from datetime import datetime, timezone

from ai_quota_keeper.config import Config
from ai_quota_keeper.scheduler import current_slot, next_trigger, previous_trigger, slot_id
from conftest import TZ, tpe

C = Config()
T = (C.triggers, TZ)


def test_triggers_are_taipei_0700_1230_1800():
    assert [t.strftime("%H:%M") for t in C.triggers] == ["07:00", "12:30", "18:00"]


def test_next_0700_to_1230():
    assert next_trigger(tpe(2026, 11, 17, 7, 0), *T) == tpe(2026, 11, 17, 12, 30)


def test_next_1230_to_1800():
    assert next_trigger(tpe(2026, 11, 17, 12, 30), *T) == tpe(2026, 11, 17, 18, 0)


def test_next_1800_to_next_day_0700():
    assert next_trigger(tpe(2026, 11, 17, 18, 0), *T) == tpe(2026, 11, 18, 7, 0)


def test_month_end():
    assert next_trigger(tpe(2026, 11, 30, 18, 0), *T) == tpe(2026, 12, 1, 7, 0)


def test_year_end():
    assert next_trigger(tpe(2026, 12, 31, 20, 0), *T) == tpe(2027, 1, 1, 7, 0)


def test_utc_conversion_not_12_00():
    # 12:30 台北 = 04:30 UTC；傳入 UTC 時間仍得到正確台北時段
    utc = datetime(2026, 11, 17, 4, 30, tzinfo=timezone.utc)
    assert next_trigger(utc, *T) == tpe(2026, 11, 17, 18, 0)
    assert previous_trigger(utc, *T) == tpe(2026, 11, 17, 12, 30)
    assert tpe(2026, 11, 17, 7, 0).astimezone(timezone.utc).hour == 23  # 前一日 23:00 UTC


def test_boundary_exact_and_just_before():
    assert previous_trigger(tpe(2026, 11, 17, 12, 30), *T) == tpe(2026, 11, 17, 12, 30)
    assert previous_trigger(tpe(2026, 11, 17, 12, 29, 59), *T) == tpe(2026, 11, 17, 7, 0)
    assert next_trigger(tpe(2026, 11, 17, 12, 29, 59), *T) == tpe(2026, 11, 17, 12, 30)


def test_before_first_trigger_uses_yesterday_1800():
    assert previous_trigger(tpe(2026, 11, 17, 3, 0), *T) == tpe(2026, 11, 16, 18, 0)


def test_tolerance_and_missed():
    slot, ok = current_slot(tpe(2026, 11, 17, 7, 1, 59), C.triggers, TZ, 120)
    assert ok and slot == tpe(2026, 11, 17, 7, 0)
    slot, ok = current_slot(tpe(2026, 11, 17, 7, 2, 1), C.triggers, TZ, 120)
    assert not ok       # 遲到超過容許值 → missed，不補跑


def test_slot_id_stable():
    assert slot_id(tpe(2026, 11, 17, 7, 0)) == "2026-11-17T07:00+08:00"
