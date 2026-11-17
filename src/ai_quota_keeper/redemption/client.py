"""兌換客戶端。LiveRedemptionClient 有多層防護，測試/dry-run 下絕不會送出 consume。"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Callable, Protocol

from ..appserver import (CONSUME_METHOD, AppServerAuthError, AppServerClient, AppServerError,
                         AppServerTimeout, Transport)
from ..models import RedeemOutcome

FORBID_ENV = "AI_QUOTA_KEEPER_FORBID_CONSUME"


class LiveConsumeForbidden(RuntimeError):
    """在不允許真實兌換的情境下嘗試使用 Live Client。"""


class RedemptionClient(Protocol):
    def redeem(self, credit_id: str, idempotency_key: str) -> RedeemOutcome: ...


class DryRunClient:
    """永不連線、永不兌換。"""
    calls: int = 0

    def redeem(self, credit_id: str, idempotency_key: str) -> RedeemOutcome:
        self.calls += 1
        return RedeemOutcome.DRY_RUN


class LiveRedemptionClient:
    """符合官方規格：account/rateLimitResetCredit/consume {creditId, idempotencyKey}。

    防護：(1) mode 必須為 production；(2) 環境變數 AI_QUOTA_KEEPER_FORBID_CONSUME 或 pytest
    執行中一律拒絕；(3) 只有 allow_live=True（僅 run-scheduled 傳入）才可呼叫。
    """

    def __init__(self, transport_factory: Callable[[], Transport], mode: str,
                 allow_live: bool = False, timeout: float = 30.0) -> None:
        self._factory, self.mode, self.allow_live, self.timeout = transport_factory, mode, allow_live, timeout

    def _guard(self) -> None:
        if self.mode != "production" or not self.allow_live:
            raise LiveConsumeForbidden(f"mode={self.mode} allow_live={self.allow_live}：禁止真實兌換")
        if os.environ.get(FORBID_ENV) or os.environ.get("PYTEST_CURRENT_TEST"):
            raise LiveConsumeForbidden("測試環境禁止真實兌換")

    def redeem(self, credit_id: str, idempotency_key: str) -> RedeemOutcome:
        self._guard()
        try:
            with AppServerClient(self._factory, timeout=self.timeout) as c:
                res = c.request(CONSUME_METHOD, {"creditId": credit_id,
                                                 "idempotencyKey": idempotency_key})
        except AppServerTimeout:
            return RedeemOutcome.TIMEOUT
        except AppServerAuthError:
            return RedeemOutcome.AUTH_ERROR
        except AppServerError:
            return RedeemOutcome.UNEXPECTED
        try:
            return RedeemOutcome(res.get("outcome"))
        except ValueError:
            return RedeemOutcome.UNEXPECTED


class UnsupportedRedemptionClient:
    """沒有兌換方式的 provider。"""

    def redeem(self, credit_id: str, idempotency_key: str) -> RedeemOutcome:
        return RedeemOutcome.UNSUPPORTED


CLAUDE_HOST = "api.anthropic.com"
CLAUDE_PROGRAM = "cedar_ember"
_CLAUDE_RESULTS = {
    "reset": RedeemOutcome.RESET,
    "already_used": RedeemOutcome.ALREADY_REDEEMED,
    "not_limited": RedeemOutcome.NOTHING_TO_RESET,
    "ineligible": RedeemOutcome.NO_CREDIT,
    "cooldown": RedeemOutcome.NO_CREDIT,
}


class ClaudeRedemptionClient:
    """Claude Code CLI 使用的兌換呼叫：POST /api/organizations/{org}/reset_rate_limits
    body {program, grant_id, request_id}。request_id 即 idempotency key。

    與 LiveRedemptionClient 相同的防護：mode=production、allow_live=True、非測試環境。
    憑證由 provider 在記憶體中提供，不刷新、不記錄。
    """

    def __init__(self, provider, mode: str, allow_live: bool = False, timeout: float = 25.0,
                 opener: Callable[..., object] | None = None, version: str = "2.1.322") -> None:
        self._prov, self.mode, self.allow_live, self.timeout = provider, mode, allow_live, timeout
        self._opener = opener or urllib.request.urlopen
        self.version = version

    def _guard(self) -> None:
        if self.mode != "production" or not self.allow_live:
            raise LiveConsumeForbidden(f"mode={self.mode} allow_live={self.allow_live}：禁止真實兌換")
        if os.environ.get(FORBID_ENV) or os.environ.get("PYTEST_CURRENT_TEST"):
            raise LiveConsumeForbidden("測試環境禁止真實兌換")

    def redeem(self, credit_id: str, idempotency_key: str) -> RedeemOutcome:
        self._guard()
        org = self._prov.organization_uuid()
        tok = self._prov.token()
        if not org or not tok:
            return RedeemOutcome.AUTH_ERROR
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", credit_id) or \
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", idempotency_key):
            return RedeemOutcome.UNEXPECTED
        body = json.dumps({"program": CLAUDE_PROGRAM, "grant_id": credit_id,
                           "request_id": idempotency_key}).encode()
        req = urllib.request.Request(
            f"https://{CLAUDE_HOST}/api/organizations/{org}/reset_rate_limits", data=body,
            method="POST", headers={
                "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": f"claude-cli/{self.version} (external, cli)"})
        assert req.host == CLAUDE_HOST
        try:
            with self._opener(req, timeout=self.timeout) as r:      # type: ignore[operator]
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return RedeemOutcome.AUTH_ERROR if e.code in (401, 403) else RedeemOutcome.UNEXPECTED
        except TimeoutError:
            return RedeemOutcome.TIMEOUT
        except urllib.error.URLError as e:
            return RedeemOutcome.TIMEOUT if isinstance(e.reason, TimeoutError) else RedeemOutcome.UNEXPECTED
        except Exception:  # noqa: BLE001
            return RedeemOutcome.UNEXPECTED
        res = data.get("result") if isinstance(data, dict) else None
        return _CLAUDE_RESULTS.get(res, RedeemOutcome.UNEXPECTED)
