"""Provider 介面：查詢 Reset 券（唯讀）與兌換能力宣告。"""
from __future__ import annotations

from typing import Protocol

from ..models import CreditsSnapshot


class ResetProvider(Protocol):
    name: str
    redemption_support: str      # supported / manual_required / unsupported

    def fetch_credits(self) -> CreditsSnapshot:
        """唯讀查詢。失敗時回傳 ok=False 的 snapshot，不得拋出以免影響 hi 觸發。"""
        ...
