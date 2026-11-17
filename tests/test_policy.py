from datetime import timedelta

import pytest

from ai_quota_keeper.redemption.policy import evaluate, next_redemption_point
from ai_quota_keeper.scheduler import next_trigger
from conftest import TZ, credit, snap, tpe
from ai_quota_keeper.config import Config

NOW1230 = tpe(2026, 11, 17, 12, 30)
NEXT1800 = tpe(2026, 11, 17, 18, 0)


def dec(credits, now=NOW1230, nxt=NEXT1800, **kw):
    return evaluate(snap(credits, **kw), now, nxt)


def test_expires_before_next_redeem():
    assert dec([credit(exp=tpe(2026, 11, 17, 17, 0))])[0].redeem


def test_expires_after_next_not_redeem():
    assert not dec([credit(exp=tpe(2026, 11, 17, 19, 0))])[0].redeem


def test_expires_exactly_at_next_redeem():
    assert dec([credit(exp=NEXT1800)])[0].redeem


def test_1800_to_tomorrow_0700_cases():
    now, nxt = tpe(2026, 11, 17, 18, 0), tpe(2026, 11, 18, 7, 0)
    assert dec([credit(exp=tpe(2026, 11, 17, 19, 0))], now, nxt)[0].redeem
    assert not dec([credit(exp=tpe(2026, 11, 18, 9, 0))], now, nxt)[0].redeem
    now2, nxt2 = tpe(2026, 11, 18, 7, 0), tpe(2026, 11, 18, 12, 30)
    assert dec([credit(exp=tpe(2026, 11, 18, 9, 0))], now2, nxt2)[0].redeem


def test_expired_not_redeem():
    assert not dec([credit(exp=NOW1230 - timedelta(minutes=1))])[0].redeem
    assert not dec([credit(exp=NOW1230)])[0].redeem        # 嚴格 now < expires


def test_not_started():
    assert not dec([credit(exp=tpe(2026, 11, 17, 17, 0), start=tpe(2026, 11, 17, 13, 0))])[0].redeem


@pytest.mark.parametrize("status", ["redeemed", "redeeming", "unknown"])
def test_used_or_non_available(status):
    assert not dec([credit(exp=tpe(2026, 11, 17, 17, 0), status=status)])[0].redeem


def test_no_expiry_conservative():
    d = dec([credit(exp=None)])[0]
    assert not d.redeem and "無到期" in d.reason


def test_no_credits():
    assert dec([]) == []
    assert dec([], count=0) == []


def test_multiple_earliest_first():
    ds = dec([credit("b", exp=tpe(2026, 11, 17, 17, 30)), credit("a", exp=tpe(2026, 11, 17, 14, 0)),
              credit("z", exp=tpe(2026, 11, 20, 14, 0))])
    assert [d.credit_id for d in ds] == ["a", "b", "z"]
    assert [d.redeem for d in ds] == [True, True, False]


def test_same_expiry_stable_order():
    e = tpe(2026, 11, 17, 15, 0)
    assert [d.credit_id for d in dec([credit("y", exp=e), credit("x", exp=e)])] == ["x", "y"]


def test_details_null_with_count():
    d = dec(None, count=2)
    assert len(d) == 1 and not d[0].redeem and "沒有券明細" in d[0].reason


def test_different_reset_type_not_redeemed():
    assert not dec([credit(exp=tpe(2026, 11, 17, 17, 0), rtype="unknown")])[0].redeem


def test_failed_snapshot_no_decisions():
    assert evaluate(snap([], ok=False), NOW1230, NEXT1800) == []


def test_next_redemption_point():
    c = Config()
    step = lambda t: next_trigger(t, c.triggers, TZ)
    s = snap([credit(exp=tpe(2026, 11, 30, 20, 0))])
    # 11/30 20:00 到期 → 最後可兌換排程點為 11/30 18:00（其下一次 12/01 07:00 已晚於到期）
    assert next_redemption_point(s, tpe(2026, 11, 17, 3, 0), step) == tpe(2026, 11, 30, 18, 0)
    s2 = snap([credit(exp=tpe(2026, 12, 1, 4, 0))])
    assert next_redemption_point(s2, tpe(2026, 11, 17, 3, 0), step) == tpe(2026, 11, 30, 18, 0)
    assert next_redemption_point(snap([credit(exp=None)]), tpe(2026, 11, 17, 3, 0), step) is None
