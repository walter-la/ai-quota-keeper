"""Codex：`hi` 觸發 + 透過 App Server 查詢 Reset 券。"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..appserver import (READ_METHOD, AppServerAuthError, AppServerClient, AppServerError,
                         Transport, codex_transport_factory)
from ..config import CliSpec
from ..models import CliResult, CliStatus, CreditsSnapshot, ResetCredit, UsageWindow
from ..runner import CommandRunner, ProcResult, find_executable, with_node_path
from .cli_common import NowFn, execute

_EFFORT_ERR = ("reasoning", "effort", "unsupported value", "invalid value")


def build_command(exe: str, spec: CliSpec, effort: str) -> list[str]:
    return [exe, "exec", "--model", spec.model, "--config", f"model_reasoning_effort={effort}",
            "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", spec.prompt]


def _parse(res: ProcResult) -> tuple[str | None, str | None]:
    text = res.stdout + "\n" + res.stderr
    m = re.search(r"^model:\s*(\S+)", text, re.M)
    e = re.search(r"^reasoning effort:\s*(\S+)", text, re.M)
    return (m.group(1) if m else None, e.group(1) if e else None)


def run_hi(spec: CliSpec, *, cwd: Path, timeout: float, env: dict[str, str],
           runner: CommandRunner, now: NowFn) -> CliResult:
    def attempt(effort: str) -> CliResult:
        return execute("codex", "codex", lambda exe: build_command(exe, spec, effort),
                       model=spec.model, effort=effort, cwd=cwd, timeout=timeout, env=env,
                       runner=runner, now=now, parse=_parse)

    r = attempt(spec.effort)
    # 僅當 CLI 明確不支援 `none` 時才降級為 low（能力降級，非失敗重試）
    if (r.status == CliStatus.FAILED and spec.effort == "none"
            and r.error and any(k in r.error.lower() for k in _EFFORT_ERR)):
        r = attempt("low")
    return r


# ---------------- Reset 券查詢 ----------------
def _ts(v: int | None) -> datetime | None:
    return datetime.fromtimestamp(v, timezone.utc) if v is not None else None


def parse_rate_limits(result: dict) -> CreditsSnapshot:
    """將 account/rateLimits/read 回應轉為內部模型。不推測未知的到期時間。"""
    summary = result.get("rateLimitResetCredits")
    count: int | None = None
    credits: list[ResetCredit] | None = None
    if isinstance(summary, dict):
        count = summary.get("availableCount")
        raw = summary.get("credits")
        if isinstance(raw, list):
            credits = []
            for c in raw:
                if not isinstance(c, dict) or "id" not in c:
                    continue
                credits.append(ResetCredit(
                    id=str(c["id"]), provider="codex", status=str(c.get("status", "unknown")),
                    reset_type=str(c.get("resetType", "unknown")),
                    granted_at=_ts(c.get("grantedAt")), starts_at=_ts(c.get("grantedAt")),
                    expires_at=_ts(c.get("expiresAt")), title=c.get("title")))
    windows: list[UsageWindow] = []
    rl = result.get("rateLimits") or {}
    for name in ("primary", "secondary"):
        w = rl.get(name)
        if isinstance(w, dict):
            windows.append(UsageWindow(name, w.get("usedPercent"), w.get("windowDurationMins"),
                                       _ts(w.get("resetsAt"))))
    acct = result.get("accountId")
    return CreditsSnapshot(
        "codex", True, count, credits, windows,
        hashlib.sha256(acct.encode()).hexdigest()[:12] if acct else None,
        redemption_support="supported")


class CodexResetProvider:
    name = "codex"
    redemption_support = "supported"

    def __init__(self, env: dict[str, str], cwd: Path, timeout: float = 20.0,
                 transport_factory: Callable[[], Transport] | None = None) -> None:
        self.env, self.cwd, self.timeout = env, cwd, timeout
        self._factory = transport_factory

    def _client(self) -> AppServerClient:
        factory = self._factory
        if factory is None:
            exe = find_executable("codex", self.env)
            if exe is None:
                raise AppServerError("找不到 codex")
            self.cwd.mkdir(parents=True, exist_ok=True)
            factory = codex_transport_factory(exe, with_node_path(self.env, exe), str(self.cwd))
        return AppServerClient(factory, timeout=self.timeout)

    def fetch_credits(self) -> CreditsSnapshot:
        try:
            with self._client() as c:           # 只呼叫唯讀的 READ_METHOD
                return parse_rate_limits(c.request(READ_METHOD))
        except AppServerAuthError as e:
            return CreditsSnapshot("codex", False, error=f"認證失敗: {e}",
                                   redemption_support=self.redemption_support)
        except Exception as e:  # noqa: BLE001 — 查詢失敗不得影響觸發
            return CreditsSnapshot("codex", False, error=f"查詢失敗: {type(e).__name__}: {e}",
                                   redemption_support=self.redemption_support)
