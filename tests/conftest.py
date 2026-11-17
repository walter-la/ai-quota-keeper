import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ai_quota_keeper import appserver
from ai_quota_keeper.config import Config
from ai_quota_keeper.models import CreditsSnapshot, ResetCredit
from ai_quota_keeper.orchestrator import Deps
from ai_quota_keeper.runner import ProcResult
from ai_quota_keeper.state_store import StateStore

TZ = ZoneInfo("Asia/Taipei")


def tpe(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _forbid_live(monkeypatch):
    """全域保險：測試期間禁止真實兌換、禁止啟動真實 codex app-server。"""
    monkeypatch.setenv("AI_QUOTA_KEEPER_FORBID_CONSUME", "1")

    def boom(*a, **k):
        raise AssertionError("測試中不得啟動真實 codex app-server")
    monkeypatch.setattr(appserver.StdioTransport, "__init__", boom)


@pytest.fixture
def cfg(tmp_path):
    return Config(root=tmp_path, workdir_codex=tmp_path / "w1", workdir_claude=tmp_path / "w2")


@pytest.fixture
def store(cfg):
    return StateStore(cfg.db_path)


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t.astimezone(timezone.utc)


class FakeRunner:
    """依 cmd[0] 的檔名回傳腳本化結果並記錄呼叫。"""
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def __call__(self, cmd, *, cwd, timeout, env=None):
        self.calls.append((cmd, str(cwd), timeout, env))
        name = Path(cmd[0]).name
        r = self.results.get(name, ProcResult(0, "ok", ""))
        if isinstance(r, Exception):
            raise r
        return r


class FakeProvider:
    def __init__(self, snap): self.snap = snap; self.n = 0
    redemption_support = "supported"; name = "fake"
    def fetch_credits(self):
        self.n += 1
        return self.snap(self.n) if callable(self.snap) else self.snap


def credit(id="c1", exp=None, status="available", rtype="codexRateLimits", start=None, provider="codex"):
    return ResetCredit(id=id, provider=provider, status=status, reset_type=rtype,
                       starts_at=start, expires_at=exp)


def snap(credits, count=None, ok=True, windows=(), acct="acct1", provider="codex"):
    return CreditsSnapshot(provider, ok, count if count is not None else (len(credits) if credits is not None else None),
                           credits, list(windows), acct)


@pytest.fixture
def make_deps(cfg, store, tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    for n in ("codex", "claude"):
        f = tmp_path / "bin" / n
        f.write_text("#!/bin/sh\n"); f.chmod(0o755)

    def _make(now, runner=None, codex=None, claude=None, client=None):
        env = {"HOME": str(tmp_path), "PATH": str(tmp_path / "bin")}
        return Deps(cfg=cfg, store=store, runner=runner or FakeRunner(), clock=Clock(now), env=env,
                    codex_provider=codex or FakeProvider(snap([])),
                    claude_provider=claude or FakeProvider(snap([], provider="claude")),
                    codex_client=client)
    return _make
