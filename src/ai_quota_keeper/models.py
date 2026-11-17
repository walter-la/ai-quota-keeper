"""內部統一資料模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class CliStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    NOT_FOUND = "NOT_FOUND"
    AUTH_ERROR = "AUTH_ERROR"
    MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"
    SKIPPED = "SKIPPED"


class RedeemOutcome(str, Enum):
    RESET = "reset"
    ALREADY_REDEEMED = "alreadyRedeemed"
    NOTHING_TO_RESET = "nothingToReset"
    NO_CREDIT = "noCredit"
    TIMEOUT = "timeout"          # 結果不確定：必須重用同一 idempotency key
    AUTH_ERROR = "authError"
    UNEXPECTED = "unexpected"
    DRY_RUN = "dryRun"
    UNSUPPORTED = "unsupported"


# 這些結果代表「平台已確定處理」，同一張券不再需要沿用舊 key
DEFINITIVE_CONSUMED = {RedeemOutcome.RESET, RedeemOutcome.ALREADY_REDEEMED}
DEFINITIVE_NOT_CONSUMED = {RedeemOutcome.NOTHING_TO_RESET, RedeemOutcome.NO_CREDIT}


@dataclass(frozen=True)
class ResetCredit:
    """可主動兌換的 Reset 券（不同於 usage window reset）。"""
    id: str
    provider: str                      # codex / claude
    status: str = "available"         # available / redeeming / redeemed / unknown
    reset_type: str = "unknown"
    granted_at: datetime | None = None
    starts_at: datetime | None = None
    expires_at: datetime | None = None  # None = 未知或不過期；絕不推測
    title: str | None = None
    paused: bool = False
    count: int = 1                     # claude grant 可能有多次 resets_left


@dataclass(frozen=True)
class UsageWindow:
    name: str
    used_percent: float | None
    window_minutes: int | None
    resets_at: datetime | None


@dataclass
class CreditsSnapshot:
    provider: str
    ok: bool
    available_count: int | None = None
    credits: list[ResetCredit] | None = None     # None = 只有總數沒有明細
    windows: list[UsageWindow] = field(default_factory=list)
    account_id_hash: str | None = None            # 帳號識別只保存雜湊
    error: str | None = None
    redemption_support: str = "unknown"           # supported / manual_required / unsupported

    def nearest_expiry(self) -> datetime | None:
        exps = [c.expires_at for c in (self.credits or []) if c.expires_at]
        return min(exps) if exps else None


@dataclass
class CliResult:
    cli: str
    status: CliStatus
    model: str
    effort: str
    started_at: datetime
    finished_at: datetime
    exit_code: int | None
    command: list[str]
    error: str | None = None
    effective_model: str | None = None
    effective_effort: str | None = None

    @property
    def duration_s(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


@dataclass
class Decision:
    credit_id: str
    provider: str
    redeem: bool
    reason: str
    expires_at: datetime | None = None
