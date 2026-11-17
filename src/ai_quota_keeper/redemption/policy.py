"""Reset 券兌換決策（純函式）。

規則：僅當 now < expires_at <= next_trigger 才兌換（到期時間剛好等於下一排程 → 現在兌換）。
資料不完整一律保守不兌換。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..models import CreditsSnapshot, Decision, ResetCredit

EXPECTED_RESET_TYPES = {"codex": {"codexRateLimits"}, "claude": {"claudeLimitReset"}}


def decide_one(c: ResetCredit, now: datetime, next_trigger: datetime) -> Decision:
    def d(redeem: bool, reason: str) -> Decision:
        return Decision(c.id, c.provider, redeem, reason, c.expires_at)

    allowed = EXPECTED_RESET_TYPES.get(c.provider)
    if allowed is not None and c.reset_type not in allowed:
        return d(False, f"券種類不符（{c.reset_type}）")
    if c.status != "available":
        return d(False, f"券狀態為 {c.status}，非 available")
    if c.paused:
        return d(False, "券已暫停")
    if c.starts_at is not None and c.starts_at > now:
        return d(False, "券尚未生效")
    if c.expires_at is None:
        return d(False, "無到期時間，保守不自動兌換")
    if c.expires_at <= now:
        return d(False, "券已過期")
    if c.expires_at <= next_trigger:
        return d(True, "將在下一個排程時間前到期（含剛好等於），此為最後兌換機會")
    return d(False, "到期時間晚於下一個排程時間，保留給之後的排程點")


def evaluate(snap: CreditsSnapshot, now: datetime, next_trigger: datetime) -> list[Decision]:
    """依 expires_at 由早到晚（同時到期以 id 排序保持穩定）評估每張券。"""
    if not snap.ok:
        return []
    if snap.credits is None:
        if snap.available_count:
            return [Decision("", snap.provider, False,
                             f"僅有總數（{snap.available_count}）沒有券明細，保守不兌換")]
        return []
    far = datetime.max.replace(tzinfo=now.tzinfo)
    ordered = sorted(snap.credits, key=lambda c: (c.expires_at or far, c.id))
    return [decide_one(c, now, next_trigger) for c in ordered]


def next_redemption_point(snap: CreditsSnapshot, now: datetime,
                          step: Callable[[datetime], datetime]) -> datetime | None:
    """預計兌換排程點：第一個滿足 expires_at <= next(t) 的未來排程時間 t。

    `step(t)` 回傳嚴格晚於 t 的下一個排程時間。券已過期/無到期時間 → None。
    若到期早於下一個排程（現在這個時段就是最後機會）→ 回傳 None。
    """
    exp = snap.nearest_expiry() if snap.ok else None
    if exp is None or exp <= now:
        return None
    t = step(now)
    if exp <= t:
        return None
    for _ in range(5000):
        nxt = step(t)
        if exp <= nxt:
            return t
        t = nxt
    return None
