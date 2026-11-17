"""安全測試：任何情況都不會真實兌換、token 不外洩。"""
import json

import pytest

from ai_quota_keeper import appserver, cli
from ai_quota_keeper.logging_setup import redact, setup_logging, write_jsonl
from ai_quota_keeper.models import RedeemOutcome as O
from ai_quota_keeper.redemption.client import LiveConsumeForbidden, LiveRedemptionClient
from ai_quota_keeper.redemption.mock import MockRedemptionClient
from ai_quota_keeper.orchestrator import run_scheduled
from conftest import FakeProvider, credit, snap, tpe

DUE = tpe(2026, 11, 17, 17, 0)


@pytest.fixture
def consume_spy(monkeypatch):
    sent = []
    orig = appserver.AppServerClient._call

    def spy(self, method, params):
        sent.append(method)
        if method == appserver.CONSUME_METHOD:
            raise AssertionError("consume 被呼叫！")
        return orig(self, method, params)
    monkeypatch.setattr(appserver.AppServerClient, "_call", spy)
    return sent


def test_live_client_refuses_in_tests_and_non_production():
    t = lambda: (_ for _ in ()).throw(AssertionError("不得建立 transport"))
    with pytest.raises(LiveConsumeForbidden):
        LiveRedemptionClient(t, mode="production", allow_live=True).redeem("c", "k")   # pytest/ENV 防護
    with pytest.raises(LiveConsumeForbidden):
        LiveRedemptionClient(t, mode="dry_run", allow_live=True).redeem("c", "k")
    with pytest.raises(LiveConsumeForbidden):
        LiveRedemptionClient(t, mode="production", allow_live=False).redeem("c", "k")


def _args(*a):
    return cli.build_parser().parse_args(list(a))


def test_dry_run_status_credits_never_consume(make_deps, consume_spy, capsys):
    d = make_deps(tpe(2026, 11, 17, 12, 30), codex=FakeProvider(snap([credit("a", exp=DUE)])))
    for argv in (["dry-run"], ["status"], ["credits"], ["health"], ["history"], ["logs"]):
        if argv == ["health"]:
            continue    # health 會呼叫真實 CLI 版本檢查，另測
        cli.HANDLERS[argv[0]](_args(*argv), d)
    assert appserver.CONSUME_METHOD not in consume_spy
    assert "未發送任何 consume" in capsys.readouterr().out


def test_test_hi_never_redeems(make_deps):
    from conftest import FakeRunner
    m = MockRedemptionClient()
    prov = FakeProvider(snap([credit("a", exp=DUE)]))
    d = make_deps(tpe(2026, 11, 17, 12, 30), runner=FakeRunner(), codex=prov, client=m)
    assert cli.cmd_test_hi(_args("test-hi"), d) == 0
    assert not m.calls and prov.n == 0            # 連查券都沒有


def test_scheduled_without_allow_live_never_live(make_deps, consume_spy):
    prov = FakeProvider(snap([credit("a", exp=DUE)]))
    d = make_deps(tpe(2026, 11, 17, 12, 30), codex=prov)      # 無 client 注入、allow_live=False
    rec = run_scheduled(d, mode="production", allow_live=False)
    assert rec["credits"]["codex"]["needs_redeem_now"] is True
    assert rec["credits"]["codex"]["attempts"][0]["outcome"] == O.DRY_RUN.value
    assert not consume_spy


def test_scheduled_allow_live_still_blocked_under_pytest(make_deps):
    """即使走 production + allow_live，pytest 環境的守衛會讓 consume 失敗而非送出。"""
    prov = FakeProvider(snap([credit("a", exp=DUE)]))
    d = make_deps(tpe(2026, 11, 17, 12, 30), codex=prov)
    rec = run_scheduled(d, mode="production", allow_live=True)
    assert "error" in rec["credits"]["codex"] or rec["credits"]["codex"]["attempts"] == [] \
        or rec["credits"]["codex"]["attempts"][0]["outcome"] != "reset"


def test_redact_tokens():
    s = 'Authorization: Bearer abcdef1234567890XYZ accessToken":"sk-ant-oat01-AAAAAAAAAAAA" eyJhbGciOiJI.eyJzdWIiOiIx.sig'
    r = redact(s)
    assert "abcdef1234567890XYZ" not in r and "sk-ant-oat01" not in r and "eyJhbGci" not in r


def test_logs_never_contain_token(cfg, tmp_path):
    import logging
    log = setup_logging(cfg.logs_dir)
    log.error("fail Bearer SECRETTOKEN123456789")
    write_jsonl(cfg.logs_dir, {"e": "Bearer SECRETTOKEN123456789"})
    for f in cfg.logs_dir.iterdir():
        assert "SECRETTOKEN" not in f.read_text()
    logging.getLogger("ai_quota_keeper").handlers.clear()
    logging.getLogger("ai_quota_keeper")._configured = False


def test_install_scripts_never_run_scheduled_or_consume():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for f in (root / "scripts").glob("*.sh"):
        t = f.read_text()
        assert "consume" not in t and "run-scheduled" not in t, f.name
    assert "run-scheduled" in (root / "systemd/ai-quota-keeper.service").read_text()
