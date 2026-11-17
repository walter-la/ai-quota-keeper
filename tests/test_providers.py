import json
from datetime import datetime, timezone

import pytest

from ai_quota_keeper.appserver import AppServerAuthError, AppServerClient, AppServerTimeout
from ai_quota_keeper.providers import claude as cp, codex as xp
from conftest import tpe

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)

SAMPLE = {"accountId": "acct-123", "rateLimits": {"primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 1794861107}},
          "rateLimitResetCredits": {"availableCount": 3, "credits": [
              {"id": "A", "resetType": "codexRateLimits", "status": "available", "grantedAt": 1793476905, "expiresAt": 1796068905},
              {"id": "B", "resetType": "codexRateLimits", "status": "available", "grantedAt": 1793476905, "expiresAt": None}]}}


def test_parse_codex_credits():
    s = xp.parse_rate_limits(SAMPLE)
    assert s.ok and s.available_count == 3 and len(s.credits) == 2   # availableCount 與明細數可不同
    assert s.credits[0].expires_at == datetime.fromtimestamp(1796068905, timezone.utc)
    assert s.credits[1].expires_at is None                           # 不推測
    assert "acct-123" not in (s.account_id_hash or "")
    assert s.windows[0].name == "primary"


def test_parse_codex_credits_null_and_missing():
    s = xp.parse_rate_limits({"rateLimitResetCredits": {"availableCount": 2, "credits": None}})
    assert s.credits is None and s.available_count == 2
    s = xp.parse_rate_limits({})
    assert s.available_count is None and s.credits is None


class Scripted:
    def __init__(self, handler): self.sent = []; self.h = handler; self.closed = False
    def send(self, m): self.sent.append(m)
    def recv(self, timeout):
        m = self.sent[-1]
        if "id" not in m: return {"method": "n"}
        return self.h(m)
    def close(self): self.closed = True


def test_handshake_and_read_only_methods():
    t = Scripted(lambda m: {"id": m["id"], "result": SAMPLE if m["method"] == "account/rateLimits/read" else {}})
    p = xp.CodexResetProvider({}, __import__("pathlib").Path("/tmp"), transport_factory=lambda: t)
    s = p.fetch_credits()
    assert s.ok
    assert [m["method"] for m in t.sent] == ["initialize", "initialized", "account/rateLimits/read"]
    assert t.closed


def test_provider_error_and_auth_mapping():
    t = Scripted(lambda m: {"id": m["id"], "error": {"code": 401, "message": "Unauthorized token"}})
    s = xp.CodexResetProvider({}, __import__("pathlib").Path("/tmp"), transport_factory=lambda: t).fetch_credits()
    assert not s.ok and "認證" in s.error


def test_appserver_timeout():
    def h(m): raise AppServerTimeout("t")
    c = AppServerClient(lambda: Scripted(h), timeout=0.1)
    with pytest.raises(AppServerTimeout):
        c.__enter__()


def test_appserver_skips_notifications():
    class T(Scripted):
        def recv(self, timeout):
            if not hasattr(self, "n"):
                self.n = 1
                return {"method": "notif", "params": {}}
            return {"id": self.sent[-1]["id"], "result": {"ok": 1}}
    with AppServerClient(lambda: T(None)) as c:
        assert c.request("x") == {"ok": 1}


CLAUDE = {"five_hour": {"utilization": 27.0, "resets_at": "2026-11-17T04:30:00+00:00"},
          "cedar_ember": {"eligible": True, "grants": [
              {"id": "g1", "resets_left": 1, "paused": False, "starts_at": "2026-11-01T00:00:00Z", "ends_at": "2026-11-20T00:00:00Z"},
              {"id": "g2", "resets_left": 1, "paused": True, "starts_at": "2026-11-01T00:00:00Z", "ends_at": "2026-11-20T00:00:00Z"},
              {"id": "g3", "resets_left": 1, "paused": False, "starts_at": "2026-11-01T00:00:00Z", "ends_at": "2026-11-10T00:00:00Z"},
              {"id": "g4", "resets_left": "x", "paused": False, "starts_at": None, "ends_at": None}]}}


def test_parse_claude_filters_invalid_grants():
    s = cp.parse_usage(CLAUDE, NOW)
    assert s.available_count == 1 and [c.id for c in s.credits] == ["g1"]
    assert s.redemption_support == "supported"


def test_parse_claude_not_eligible_or_absent():
    assert cp.parse_usage({"cedar_ember": {"eligible": False}}, NOW).available_count == 0
    s = cp.parse_usage({}, NOW)
    assert s.available_count == 0 and "cedar_ember" in s.error


def test_claude_provider_no_token_leak(tmp_path):
    (tmp_path / ".claude").mkdir()
    cred = {"claudeAiOauth": {"accessToken": "SECRET-ACCESS-TOKEN", "expiresAt": 4102444800000, "scopes": ["user:profile"]}}
    (tmp_path / ".claude/.credentials.json").write_text(json.dumps(cred))
    seen = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return json.dumps(CLAUDE).encode()

    def opener(req, timeout):
        seen["host"] = req.host; seen["auth"] = req.get_header("Authorization")
        return Resp()
    s = cp.ClaudeResetProvider(tmp_path, opener=opener, clock=lambda: NOW).fetch_credits()
    assert s.ok and seen["host"] == "api.anthropic.com"
    assert "SECRET" not in repr(s)


def test_claude_provider_missing_scope_or_expired(tmp_path):
    (tmp_path / ".claude").mkdir()
    p = tmp_path / ".claude/.credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1, "scopes": ["user:profile"]}}))
    assert not cp.ClaudeResetProvider(tmp_path, clock=lambda: NOW).fetch_credits().ok
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 4102444800000, "scopes": ["user:inference"]}}))
    assert not cp.ClaudeResetProvider(tmp_path, clock=lambda: NOW).fetch_credits().ok


def test_detect_version_parses_cli_output(monkeypatch):
    class P:
        stdout = "2.1.322 (Claude Code)\n"
    monkeypatch.setattr(cp, "find_executable", lambda name, env: "/x/claude")
    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: P())
    assert cp.detect_version({"PATH": ""}) == "2.1.322"


def test_detect_version_fallback(monkeypatch):
    monkeypatch.setattr(cp, "find_executable", lambda name, env: None)
    assert cp.detect_version({"PATH": ""}) == cp.FALLBACK_VERSION
    monkeypatch.setattr(cp, "find_executable", lambda name, env: "/x/claude")
    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert cp.detect_version({"PATH": ""}) == cp.FALLBACK_VERSION


def test_claude_user_agent_uses_given_version(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/.credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "t", "expiresAt": 4102444800000, "scopes": ["user:profile"]}}))
    seen = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return json.dumps(CLAUDE).encode()

    def opener(req, timeout):
        seen["ua"] = req.get_header("User-agent")
        return Resp()
    cp.ClaudeResetProvider(tmp_path, version="9.8.7", opener=opener, clock=lambda: NOW).fetch_credits()
    assert seen["ua"] == "claude-cli/9.8.7 (external, cli)"
