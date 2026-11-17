"""SQLite 持久化：時段去重、執行紀錄、兌換操作（含 idempotency key）。"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_runs(
  slot TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, detail TEXT,
  PRIMARY KEY(slot, kind));
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, slot TEXT, cli TEXT, status TEXT, model TEXT, effort TEXT,
  started_at TEXT, finished_at TEXT, duration_s REAL, exit_code INTEGER, error TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS redemptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, credit_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, slot TEXT, state TEXT NOT NULL, outcome TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_red_credit ON redemptions(provider, credit_id);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS snapshots(
  provider TEXT PRIMARY KEY, taken_at TEXT, ok INTEGER, available_count INTEGER,
  nearest_expiry TEXT, error TEXT);
"""

STALE_AFTER_S = 600  # 'started' 超過此秒數視為行程異常結束，可接手


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._c()) as c:
            c.executescript(SCHEMA)
        path.chmod(0o600)

    def _c(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    # ---- 時段去重 ----
    def claim_slot(self, slot: str, kind: str, now: datetime | None = None) -> bool:
        """原子宣告 (slot, kind)。已完成/進行中 → False；行程異常結束的舊 started → 接手。"""
        now = now or datetime.now(timezone.utc)
        with closing(self._c()) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status, started_at FROM slot_runs WHERE slot=? AND kind=?",
                            (slot, kind)).fetchone()
            if row is None:
                c.execute("INSERT INTO slot_runs VALUES(?,?,?,?,NULL,NULL)",
                          (slot, kind, "started", now.isoformat()))
                c.execute("COMMIT")
                return True
            if row["status"] == "started":
                age = (now - datetime.fromisoformat(row["started_at"])).total_seconds()
                if age > STALE_AFTER_S:
                    c.execute("UPDATE slot_runs SET started_at=?, detail='recovered' "
                              "WHERE slot=? AND kind=?", (now.isoformat(), slot, kind))
                    c.execute("COMMIT")
                    return True
            c.execute("ROLLBACK")
            return False

    def finish_slot(self, slot: str, kind: str, status: str, detail: str | None = None) -> None:
        with closing(self._c()) as c:
            c.execute("UPDATE slot_runs SET status=?, finished_at=?, detail=COALESCE(?,detail) "
                      "WHERE slot=? AND kind=?", (status, _now(), detail, slot, kind))

    def slot_status(self, slot: str, kind: str) -> str | None:
        with closing(self._c()) as c:
            r = c.execute("SELECT status FROM slot_runs WHERE slot=? AND kind=?",
                          (slot, kind)).fetchone()
            return r["status"] if r else None

    # ---- 執行紀錄 ----
    def add_run(self, slot: str | None, cli: str, status: str, model: str, effort: str,
                started: datetime, finished: datetime, exit_code: int | None,
                error: str | None, source: str) -> None:
        with closing(self._c()) as c:
            c.execute("INSERT INTO runs(slot,cli,status,model,effort,started_at,finished_at,"
                      "duration_s,exit_code,error,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (slot, cli, status, model, effort, started.isoformat(), finished.isoformat(),
                       (finished - started).total_seconds(), exit_code, error, source))

    def recent_runs(self, limit: int = 20, cli: str | None = None) -> list[sqlite3.Row]:
        with closing(self._c()) as c:
            q = "SELECT * FROM runs " + ("WHERE cli=? " if cli else "") + "ORDER BY id DESC LIMIT ?"
            return c.execute(q, ((cli, limit) if cli else (limit,))).fetchall()

    def last_run(self, cli: str, source: str | None = None) -> sqlite3.Row | None:
        with closing(self._c()) as c:
            if source:
                return c.execute("SELECT * FROM runs WHERE cli=? AND source=? ORDER BY id DESC "
                                 "LIMIT 1", (cli, source)).fetchone()
            return c.execute("SELECT * FROM runs WHERE cli=? ORDER BY id DESC LIMIT 1",
                             (cli,)).fetchone()

    # ---- 兌換操作 ----
    def key_for_credit(self, provider: str, credit_id: str) -> tuple[str, bool]:
        """取得此券應使用的 idempotency key。

        回傳 (key, reused)。若上次操作結果不確定（pending/timeout/unexpected），
        必須重用同一把 key；平台已明確回應後才可使用新 key。
        """
        with closing(self._c()) as c:
            r = c.execute("SELECT * FROM redemptions WHERE provider=? AND credit_id=? "
                          "ORDER BY id DESC LIMIT 1", (provider, credit_id)).fetchone()
            if r and r["state"] == "pending":
                return r["idempotency_key"], True
        return str(uuid.uuid4()), False

    def credit_done(self, provider: str, credit_id: str) -> bool:
        with closing(self._c()) as c:
            r = c.execute("SELECT 1 FROM redemptions WHERE provider=? AND credit_id=? "
                          "AND state='done'", (provider, credit_id)).fetchone()
            return r is not None

    def begin_redemption(self, provider: str, credit_id: str, key: str, slot: str) -> int:
        with closing(self._c()) as c:
            r = c.execute("SELECT id FROM redemptions WHERE provider=? AND credit_id=? AND "
                          "idempotency_key=?", (provider, credit_id, key)).fetchone()
            if r:
                c.execute("UPDATE redemptions SET slot=?, updated_at=? WHERE id=?",
                          (slot, _now(), r["id"]))
                return r["id"]
            cur = c.execute("INSERT INTO redemptions(provider,credit_id,idempotency_key,slot,"
                            "state,created_at,updated_at) VALUES(?,?,?,?, 'pending',?,?)",
                            (provider, credit_id, key, slot, _now(), _now()))
            return int(cur.lastrowid)

    def finish_redemption(self, rid: int, state: str, outcome: str) -> None:
        with closing(self._c()) as c:
            c.execute("UPDATE redemptions SET state=?, outcome=?, updated_at=? WHERE id=?",
                      (state, outcome, _now(), rid))

    def redemptions(self, limit: int = 20) -> list[sqlite3.Row]:
        with closing(self._c()) as c:
            return c.execute("SELECT * FROM redemptions ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()

    # ---- 券快照（供 status 顯示最近一次已知狀態）----
    def save_snapshot(self, provider: str, ok: bool, count: int | None,
                      nearest: datetime | None, error: str | None) -> None:
        with closing(self._c()) as c:
            c.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?)",
                      (provider, _now(), int(ok), count,
                       nearest.isoformat() if nearest else None, error))

    def get_snapshot(self, provider: str) -> sqlite3.Row | None:
        with closing(self._c()) as c:
            return c.execute("SELECT * FROM snapshots WHERE provider=?", (provider,)).fetchone()

    # ---- meta（帳號綁定等）----
    def get_meta(self, k: str) -> str | None:
        with closing(self._c()) as c:
            r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return r["v"] if r else None

    def set_meta(self, k: str, v: str) -> None:
        with closing(self._c()) as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
