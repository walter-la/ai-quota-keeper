"""排程計算（純函式，可注入時間）。所有計算以 Asia/Taipei 的日曆為準。"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(timezone.utc)


def _at(day: datetime, t: time, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, t.hour, t.minute, tzinfo=tz)


def trigger_times_for_day(day: datetime, triggers: Sequence[time], tz: ZoneInfo) -> list[datetime]:
    return [_at(day, t, tz) for t in sorted(triggers)]


def next_trigger(after: datetime, triggers: Sequence[time], tz: ZoneInfo) -> datetime:
    """嚴格晚於 `after` 的下一個排程時間（跨日/跨月/跨年皆正確）。"""
    local = after.astimezone(tz)
    for offset in (0, 1):
        day = local + timedelta(days=offset)
        for t in trigger_times_for_day(day, triggers, tz):
            if t > local:
                return t
    raise RuntimeError("unreachable")


def previous_trigger(at: datetime, triggers: Sequence[time], tz: ZoneInfo) -> datetime:
    """不晚於 `at` 的最近一個排程時間。"""
    local = at.astimezone(tz)
    for offset in (0, -1):
        day = local + timedelta(days=offset)
        for t in reversed(trigger_times_for_day(day, triggers, tz)):
            if t <= local:
                return t
    raise RuntimeError("unreachable")


def current_slot(now: datetime, triggers: Sequence[time], tz: ZoneInfo,
                 tolerance_s: int) -> tuple[datetime, bool]:
    """回傳 (目前時段, 是否在容許延遲內)。超過容許值代表 missed，不應補跑。"""
    slot = previous_trigger(now, triggers, tz)
    late = (now - slot).total_seconds()
    return slot, late <= tolerance_s


def slot_id(slot: datetime) -> str:
    return slot.isoformat(timespec="minutes")
