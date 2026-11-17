"""兌換流程：決策 → 持久化 → 呼叫 client → 重新查詢。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..models import (DEFINITIVE_CONSUMED, DEFINITIVE_NOT_CONSUMED, CreditsSnapshot, Decision,
                      RedeemOutcome)
from ..state_store import StateStore
from .client import RedemptionClient
from .policy import evaluate

log = logging.getLogger("ai_quota_keeper")


@dataclass
class RedemptionReport:
    provider: str
    snapshot: CreditsSnapshot
    decisions: list[Decision] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    note: str | None = None


def _any_usage(snap: CreditsSnapshot) -> bool:
    return any((w.used_percent or 0) > 0 for w in snap.windows) or not snap.windows


def run_redemption(provider: str, fetch: Callable[[], CreditsSnapshot], client: RedemptionClient,
                   store: StateStore, now_fn: Callable[[], datetime], slot: str,
                   next_trigger: datetime, account_ok: bool = True,
                   max_attempts: int = 3) -> RedemptionReport:
    snap = fetch()
    rep = RedemptionReport(provider, snap)
    if not snap.ok:
        rep.note = f"無法讀取券資訊：{snap.error}"
        return rep
    if not account_ok:
        rep.note = "帳號與已綁定帳號不符，保守不兌換（用 repin-account 重新綁定）"
        rep.decisions = evaluate(snap, now_fn(), next_trigger)
        return rep
    for _ in range(max_attempts):
        now = now_fn()
        decisions = evaluate(snap, now, next_trigger)
        rep.decisions = decisions
        target = next((d for d in decisions if d.redeem
                       and not store.credit_done(provider, d.credit_id)
                       and not any(a["credit_id"] == d.credit_id for a in rep.attempts)), None)
        if target is None:
            break
        key, reused = store.key_for_credit(provider, target.credit_id)
        rid = store.begin_redemption(provider, target.credit_id, key, slot)
        outcome = client.redeem(target.credit_id, key)
        if outcome in DEFINITIVE_CONSUMED:
            store.finish_redemption(rid, "done", outcome.value)
        elif outcome in DEFINITIVE_NOT_CONSUMED or outcome in (RedeemOutcome.DRY_RUN,
                                                              RedeemOutcome.UNSUPPORTED):
            store.finish_redemption(rid, "closed", outcome.value)
        else:   # timeout / authError / unexpected：結果不確定 → 保持 pending，下次重用同一把 key
            store.finish_redemption(rid, "pending", outcome.value)
        rep.attempts.append({"credit_id": target.credit_id, "outcome": outcome.value,
                             "key_reused": reused, "expires_at": target.expires_at})
        log.info("[%s] 兌換嘗試 outcome=%s credit=%s", provider, outcome.value, target.credit_id[-6:])
        if outcome != RedeemOutcome.RESET:
            break                      # 其他結果不連續消耗下一張
        snap = fetch()                 # 成功後重新查詢最新狀態
        rep.snapshot = snap
        if not snap.ok or not _any_usage(snap):
            break                      # 沒有可重置的額度，不再消耗
    if not rep.attempts and rep.note is None:
        rep.note = "無符合條件的券" if not any(d.redeem for d in rep.decisions) else "券已處理過"
    return rep
