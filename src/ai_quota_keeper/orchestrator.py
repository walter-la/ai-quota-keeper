"""一次排程執行的流程：去重 → 兩個 CLI 觸發（互相獨立）→ Reset 券判斷/兌換 → 紀錄。"""
from __future__ import annotations

import fcntl
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator

from .config import Config
from .logging_setup import write_jsonl
from .models import CliResult, CliStatus, CreditsSnapshot
from .providers import claude as claude_p
from .providers import codex as codex_p
from .providers.reset_protocol import ResetProvider
from .redemption.client import DryRunClient, RedemptionClient
from .redemption.service import RedemptionReport, run_redemption
from .runner import CommandRunner, resolve_env, subprocess_runner
from .scheduler import current_slot, next_trigger, slot_id, system_clock
from .state_store import StateStore

log = logging.getLogger("ai_quota_keeper")


@contextmanager
def global_lock(cfg: Config) -> Iterator[bool]:
    """全域互斥（防止 Linux timer 與 Windows 喚起同時執行）。取不到鎖 → yield False。"""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    f = open(cfg.lock_path, "a+")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


@dataclass
class Deps:
    cfg: Config
    store: StateStore
    runner: CommandRunner = subprocess_runner
    clock: Callable[[], datetime] = system_clock
    env: dict[str, str] = field(default_factory=resolve_env)
    codex_provider: ResetProvider | None = None
    claude_provider: ResetProvider | None = None
    codex_client: RedemptionClient | None = None   # None → 依 mode 決定
    claude_client: RedemptionClient | None = None

    def codex_prov(self) -> ResetProvider:
        return self.codex_provider or codex_p.CodexResetProvider(self.env, self.cfg.workdir_codex)

    def claude_prov(self) -> ResetProvider:
        from pathlib import Path
        return self.claude_provider or claude_p.ClaudeResetProvider(Path(self.env["HOME"]), version=claude_p.detect_version(self.env))


def _trigger_clis(d: Deps, slot: str | None, source: str) -> list[CliResult]:
    """兩個 CLI 各自獨立；任何例外都不影響另一個。"""
    cfg, out = d.cfg, []
    jobs = [("codex", lambda: codex_p.run_hi(cfg.codex, cwd=cfg.workdir_codex,
                                             timeout=cfg.cli_timeout_seconds, env=d.env,
                                             runner=d.runner, now=d.clock), cfg.codex),
            ("claude", lambda: claude_p.run_hi(cfg.claude, cwd=cfg.workdir_claude,
                                               timeout=cfg.cli_timeout_seconds, env=d.env,
                                               runner=d.runner, now=d.clock), cfg.claude)]
    for name, job, spec in jobs:
        try:
            r = job()
        except Exception as e:  # noqa: BLE001
            t = d.clock()
            r = CliResult(name, CliStatus.FAILED, spec.model, spec.effort, t, t, None, [],
                          error=f"內部例外 {type(e).__name__}: {e}")
        d.store.add_run(slot, name, r.status.value, r.model, r.effective_effort or r.effort,
                        r.started_at, r.finished_at, r.exit_code, r.error, source)
        lvl = logging.INFO if r.status == CliStatus.SUCCESS else logging.ERROR
        log.log(lvl, "[%s] %s model=%s effort=%s exit=%s %.1fs %s", name, r.status.value,
                r.effective_model or r.model, r.effective_effort or r.effort, r.exit_code,
                r.duration_s, r.error or "")
        out.append(r)
    return out


def _account_ok(store: StateStore, snap: CreditsSnapshot) -> bool:
    if not snap.ok or not snap.account_id_hash:
        return True
    pinned = store.get_meta(f"account:{snap.provider}")
    if pinned is None:
        store.set_meta(f"account:{snap.provider}", snap.account_id_hash)
        return True
    return pinned == snap.account_id_hash


def _fmt(dt: datetime | None, cfg: Config) -> str | None:
    return dt.astimezone(cfg.tz).isoformat(timespec="seconds") if dt else None


def run_scheduled(d: Deps, mode: str = "production", allow_live: bool = False) -> dict:
    """systemd 呼叫的唯一入口。回傳寫入 trigger.jsonl 的 record。"""
    cfg = d.cfg
    now = d.clock()
    slot_dt, in_tol = current_slot(now, cfg.triggers, cfg.tz, cfg.tolerance_seconds)
    sid = slot_id(slot_dt)
    nxt = next_trigger(slot_dt, cfg.triggers, cfg.tz)
    rec: dict = {"event": "scheduled_run", "slot": sid, "mode": mode,
                 "date_taipei": now.astimezone(cfg.tz).date().isoformat(),
                 "now_taipei": _fmt(now, cfg), "now_utc": now.astimezone(timezone.utc).isoformat(
                     timespec="seconds"),
                 "trigger_taipei": _fmt(slot_dt, cfg), "next_trigger": _fmt(nxt, cfg)}
    if not in_tol:
        rec["result"] = "missed"
        rec["reason"] = f"延遲超過容許值 {cfg.tolerance_seconds}s，不補跑"
        d.store.claim_slot(sid, "missed", now)
        log.warning("時段 %s missed：%s", sid, rec["reason"])
        write_jsonl(cfg.logs_dir, rec)
        return rec
    with global_lock(cfg) as got:
        if not got:
            rec.update(result="skipped", reason="另一個執行程序持有全域鎖")
            log.warning("時段 %s skipped：全域鎖被占用", sid)
            write_jsonl(cfg.logs_dir, rec)
            return rec
        if not d.store.claim_slot(sid, "triggers", now):
            rec.update(result="skipped", reason="此時段已執行過（去重）")
            log.warning("時段 %s skipped：重複執行", sid)
            write_jsonl(cfg.logs_dir, rec)
            return rec
        try:
            results = _trigger_clis(d, sid, "scheduled")
            rec["cli"] = {r.cli: _cli_rec(r, cfg) for r in results}
            rec["credits"] = _credits_phase(d, sid, nxt, mode, allow_live)
            ok = all(r.status == CliStatus.SUCCESS for r in results)
            rec["result"] = "success" if ok else "partial_failure"
            rec["errors"] = [f"{r.cli}: {r.error}" for r in results if r.error]
            d.store.finish_slot(sid, "triggers", "done" if ok else "failed")
        except BaseException:
            d.store.finish_slot(sid, "triggers", "crashed")
            raise
    write_jsonl(cfg.logs_dir, rec)
    return rec


def _cli_rec(r: CliResult, cfg: Config) -> dict:
    return {"status": r.status.value, "model": r.effective_model or r.model,
            "effort": r.effective_effort or r.effort, "exit_code": r.exit_code,
            "started_at": _fmt(r.started_at, cfg), "finished_at": _fmt(r.finished_at, cfg),
            "duration_s": round(r.duration_s, 2), "error": r.error}


def _credits_phase(d: Deps, sid: str, nxt: datetime, mode: str, allow_live: bool) -> dict:
    cfg, out = d.cfg, {}
    # ---- Codex ----
    try:
        if d.codex_client is not None:
            client: RedemptionClient = d.codex_client
        elif mode == "production" and allow_live and cfg.redemption_enabled:
            from .appserver import codex_transport_factory
            from .redemption.client import LiveRedemptionClient
            from .runner import find_executable, with_node_path
            exe = find_executable("codex", d.env) or "codex"
            client = LiveRedemptionClient(
                codex_transport_factory(exe, with_node_path(d.env, exe), str(cfg.workdir_codex)),
                mode="production", allow_live=True)
        else:
            client = DryRunClient()
        prov = d.codex_prov()
        snap0 = prov.fetch_credits()
        rep = run_redemption("codex", lambda: prov.fetch_credits(), client, d.store, d.clock, sid,
                             nxt, account_ok=_account_ok(d.store, snap0))
        out["codex"] = _report_rec(rep, cfg, sid)
        _save_snap(d.store, rep.snapshot)
    except Exception as e:  # noqa: BLE001
        out["codex"] = {"error": f"{type(e).__name__}: {e}"}
        log.error("Codex 券階段例外: %s", e)
    # ---- Claude：與 Codex 相同流程；兌換使用 Claude Code CLI 自己的呼叫 ----
    try:
        prov = d.claude_prov()
        if d.claude_client is not None:
            client = d.claude_client
        elif mode == "production" and allow_live and cfg.redemption_enabled:
            from .redemption.client import ClaudeRedemptionClient
            client = ClaudeRedemptionClient(prov, mode="production", allow_live=True,
                                            version=claude_p.detect_version(d.env))
        else:
            client = DryRunClient()
        snap0 = prov.fetch_credits()
        rep = run_redemption("claude", lambda: prov.fetch_credits(), client, d.store, d.clock, sid,
                             nxt, account_ok=_account_ok(d.store, snap0))
        out["claude"] = _report_rec(rep, cfg, sid)
        _save_snap(d.store, rep.snapshot)
    except Exception as e:  # noqa: BLE001
        out["claude"] = {"error": f"{type(e).__name__}: {e}"}
        log.error("Claude 券階段例外: %s", e)
    return out


def _save_snap(store: StateStore, s: CreditsSnapshot) -> None:
    store.save_snapshot(s.provider, s.ok, s.available_count, s.nearest_expiry(), s.error)


def _report_rec(rep: RedemptionReport, cfg: Config, sid: str) -> dict:
    s = rep.snapshot
    return {"ok": s.ok, "error": s.error, "available_count": s.available_count,
            "nearest_expiry": _fmt(s.nearest_expiry(), cfg),
            "needs_redeem_now": any(x.redeem for x in rep.decisions),
            "decisions": [{"credit": x.credit_id[-6:], "redeem": x.redeem, "reason": x.reason,
                           "expires_at": _fmt(x.expires_at, cfg)} for x in rep.decisions],
            "attempts": [{**a, "expires_at": _fmt(a["expires_at"], cfg)} for a in rep.attempts],
            "note": rep.note}


def run_test_hi(d: Deps) -> list[CliResult]:
    """手動 hi 測試：真實呼叫兩個 CLI；完全不接觸兌換流程。"""
    return _trigger_clis(d, None, "test-hi")
