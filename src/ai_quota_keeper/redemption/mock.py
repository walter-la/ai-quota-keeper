"""測試/驗證用 Mock：可腳本化結果，記錄所有呼叫。"""
from __future__ import annotations

from ..models import RedeemOutcome


class MockRedemptionClient:
    def __init__(self, outcomes: list[RedeemOutcome] | None = None,
                 default: RedeemOutcome = RedeemOutcome.RESET) -> None:
        self.outcomes = list(outcomes or [])
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def redeem(self, credit_id: str, idempotency_key: str) -> RedeemOutcome:
        self.calls.append((credit_id, idempotency_key))
        return self.outcomes.pop(0) if self.outcomes else self.default
