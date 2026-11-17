import json
import uuid

import pytest

from ai_quota_keeper.appserver import AppServerClient
from ai_quota_keeper.models import RedeemOutcome as O, UsageWindow
from ai_quota_keeper.redemption.client import (ClaudeRedemptionClient, DryRunClient, LiveConsumeForbidden,
                                                LiveRedemptionClient)
from ai_quota_keeper.redemption.mock import MockRedemptionClient
from ai_quota_keeper.redemption.service import run_redemption
from conftest import Clock, FakeProvider, credit, snap, tpe

NOW = tpe(2026, 11, 17, 12, 30)
NXT = tpe(2026, 11, 17, 18, 0)
EXP = tpe(2026, 11, 17, 17, 0)


def go(store, prov, client, **kw):
    return run_redemption("codex", prov.fetch_credits, client, store, Clock(NOW), "s1", NXT, **kw)


def test_full_mock_flow_redeems_earliest_only_once_then_stops(store):
    w = [UsageWindow("primary", 0.0, 300, None)]   # 兌換後用量為 0 → 不再消耗下一張
    calls = {"n": 0}

    def s(n):
        calls["n"] = n
        return snap([credit("b", exp=tpe(2026, 11, 17, 17, 30)), credit("a", exp=EXP)],
                    windows=[UsageWindow("primary", 50.0, 300, None)] if n == 1 else w)
    m = MockRedemptionClient()
    rep = go(store, FakeProvider(s), m)
    assert [c[0] for c in m.calls] == ["a"]
    assert rep.attempts[0]["outcome"] == "reset"
    assert calls["n"] == 2      # 成功後重新查詢


def test_continues_to_next_credit_only_if_usage_remains(store):
    s = snap([credit("a", exp=EXP), credit("b", exp=EXP)], windows=[UsageWindow("primary", 80.0, 300, None)])
    m = MockRedemptionClient()
    go(store, FakeProvider(s), m)
    assert [c[0] for c in m.calls] == ["a", "b"]


@pytest.mark.parametrize("outcome", [O.NOTHING_TO_RESET, O.NO_CREDIT, O.ALREADY_REDEEMED])
def test_non_reset_outcomes_stop_chain(store, outcome):
    m = MockRedemptionClient([outcome])
    rep = go(store, FakeProvider(snap([credit("a", exp=EXP), credit("b", exp=EXP)])), m)
    assert len(m.calls) == 1 and rep.attempts[0]["outcome"] == outcome.value


def test_nothing_to_reset_allows_new_key_later(store):
    m = MockRedemptionClient([O.NOTHING_TO_RESET, O.RESET])
    p = FakeProvider(snap([credit("a", exp=EXP)], windows=[UsageWindow("primary", 0.0, 300, None)]))
    go(store, p, m); go(store, p, m)
    assert m.calls[0][1] != m.calls[1][1]


def test_timeout_reuses_same_idempotency_key(store):
    m = MockRedemptionClient([O.TIMEOUT, O.RESET])
    p = FakeProvider(snap([credit("a", exp=EXP)], windows=[UsageWindow("primary", 0.0, 300, None)]))
    go(store, p, m)
    rep2 = go(store, p, m)
    assert m.calls[0][1] == m.calls[1][1]            # 不確定的操作重用同 key
    uuid.UUID(m.calls[0][1])                          # 為合法 UUID
    assert rep2.attempts[0]["key_reused"]


def test_repeat_schedule_no_double_redeem_after_success(store):
    m = MockRedemptionClient([O.RESET])
    p = FakeProvider(snap([credit("a", exp=EXP)], windows=[UsageWindow("primary", 0.0, 300, None)]))
    go(store, p, m); go(store, p, m)
    assert len(m.calls) == 1                           # 已 done 的券不會再送


def test_platform_reject_and_auth_error_recorded(store):
    for o in (O.AUTH_ERROR, O.UNEXPECTED):
        m = MockRedemptionClient([o])
        rep = go(store, FakeProvider(snap([credit(f"x{o.value}", exp=EXP)])), m)
        assert rep.attempts[0]["outcome"] == o.value and len(m.calls) == 1


def test_unreadable_snapshot_no_call(store):
    m = MockRedemptionClient()
    rep = go(store, FakeProvider(snap([], ok=False)), m)
    assert not m.calls and "無法讀取" in rep.note


def test_account_mismatch_blocks(store):
    m = MockRedemptionClient()
    rep = go(store, FakeProvider(snap([credit("a", exp=EXP)])), m, account_ok=False)
    assert not m.calls and "不符" in rep.note


def test_no_detail_only_count_no_redeem(store):
    m = MockRedemptionClient()
    go(store, FakeProvider(snap(None, count=2)), m)
    assert not m.calls


def test_not_due_no_call(store):
    m = MockRedemptionClient()
    go(store, FakeProvider(snap([credit("a", exp=tpe(2026, 11, 20, 1, 0))])), m)
    assert not m.calls


# ---- Live client 規格與防護（以 fake transport，不啟動真實行程）----
class FakeTransport:
    def __init__(self, reply): self.sent = []; self.reply = reply
    def send(self, m):
        self.sent.append(m)
    def recv(self, timeout):
        m = self.sent[-1]
        if "id" not in m:
            return {"method": "noise"}
        return {"id": m["id"], "result": self.reply(m)}
    def close(self): pass


def test_live_client_spec_payload(monkeypatch):
    monkeypatch.delenv("AI_QUOTA_KEEPER_FORBID_CONSUME")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)   # 僅模擬守衛放行，transport 為 fake
    t = FakeTransport(lambda m: {"outcome": "reset"} if m["method"].endswith("consume") else {})
    c = LiveRedemptionClient(lambda: t, mode="production", allow_live=True)
    assert c.redeem("cid", "key-1") == O.RESET
    methods = [m.get("method") for m in t.sent]
    assert methods == ["initialize", "initialized", "account/rateLimitResetCredit/consume"]
    assert t.sent[-1]["params"] == {"creditId": "cid", "idempotencyKey": "key-1"}


def test_live_client_unknown_outcome_is_unexpected(monkeypatch):
    monkeypatch.delenv("AI_QUOTA_KEEPER_FORBID_CONSUME")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    t = FakeTransport(lambda m: {"outcome": "weird"} if m["method"].endswith("consume") else {})
    assert LiveRedemptionClient(lambda: t, "production", True).redeem("c", "k") == O.UNEXPECTED


def test_live_client_timeout_maps_to_timeout(monkeypatch):
    from ai_quota_keeper.appserver import AppServerTimeout
    monkeypatch.delenv("AI_QUOTA_KEEPER_FORBID_CONSUME")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    class T(FakeTransport):
        def recv(self, timeout):
            if self.sent[-1].get("method", "").endswith("consume"):
                raise AppServerTimeout("x")
            return super().recv(timeout)
    t = T(lambda m: {})
    assert LiveRedemptionClient(lambda: t, "production", True).redeem("c", "k") == O.TIMEOUT


def test_dry_run_client_never_connects():
    d = DryRunClient()
    assert d.redeem("c", "k") == O.DRY_RUN


# ---- Claude client（fake opener，不連網）----
class _Resp:
    def __init__(self, body): self._b = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self._b).encode()


class _ClaudeProv:
    def organization_uuid(self): return "11111111-2222-3333-4444-555555555555"
    def token(self): return "tok"


def _claude(monkeypatch, reply, **kw):
    monkeypatch.delenv("AI_QUOTA_KEEPER_FORBID_CONSUME", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    seen = []

    def opener(req, timeout):
        seen.append(req)
        if isinstance(reply, Exception):
            raise reply
        return _Resp(reply)
    return ClaudeRedemptionClient(_ClaudeProv(), "production", True, opener=opener, **kw), seen


@pytest.mark.parametrize("res,out", [("reset", O.RESET), ("already_used", O.ALREADY_REDEEMED),
                                     ("not_limited", O.NOTHING_TO_RESET), ("cooldown", O.NO_CREDIT),
                                     ("weird", O.UNEXPECTED)])
def test_claude_client_maps_results(monkeypatch, res, out):
    c, seen = _claude(monkeypatch, {"result": res})
    assert c.redeem("full-reset-2611", "0b3c1f52-aaaa-bbbb-cccc-1234567890ab") == out
    r = seen[0]
    assert r.full_url.endswith("/api/organizations/11111111-2222-3333-4444-555555555555/reset_rate_limits")
    assert json.loads(r.data) == {"program": "cedar_ember", "grant_id": "full-reset-2611",
                                  "request_id": "0b3c1f52-aaaa-bbbb-cccc-1234567890ab"}


def test_claude_client_timeout_and_http(monkeypatch):
    import urllib.error
    c, _ = _claude(monkeypatch, TimeoutError())
    assert c.redeem("g", "k") == O.TIMEOUT
    c, _ = _claude(monkeypatch, urllib.error.HTTPError("u", 403, "x", {}, None))
    assert c.redeem("g", "k") == O.AUTH_ERROR


def test_claude_client_refused_in_tests():
    with pytest.raises(LiveConsumeForbidden):
        ClaudeRedemptionClient(_ClaudeProv(), "production", True).redeem("g", "k")
