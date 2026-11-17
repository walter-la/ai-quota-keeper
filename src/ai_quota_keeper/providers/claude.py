"""Claude：`hi` 觸發 + 唯讀 Reset 券查詢（非公開 OAuth usage 介面，獨立封裝）。

兌換：沿用 Claude Code CLI 自己的「使用 Reset 券」呼叫（見 redemption/client.py 的 ClaudeRedemptionClient）。
憑證只在記憶體中使用、只送往 api.anthropic.com，不寫入日誌、不刷新、不覆寫。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import CliSpec
from ..models import CliResult, CreditsSnapshot, ResetCredit, UsageWindow
from ..runner import CommandRunner, ProcResult, find_executable, with_node_path
from .cli_common import NowFn, execute

USAGE_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1"   # 固定主機，不可由外部設定
ALLOWED_HOST = "api.anthropic.com"


def build_command(exe: str, spec: CliSpec) -> list[str]:
    return [exe, "-p", spec.prompt, "--model", spec.model, "--effort", spec.effort,
            "--max-turns", "1", "--tools", "", "--no-session-persistence",
            "--disable-slash-commands", "--strict-mcp-config", "--setting-sources", "",
            "--output-format", "json"]


def _parse(res: ProcResult) -> tuple[str | None, str | None]:
    try:
        d = json.loads(res.stdout)
        models = list((d.get("modelUsage") or {}).keys())
        return (models[0] if models else None, None)
    except (json.JSONDecodeError, AttributeError):
        return (None, None)


def run_hi(spec: CliSpec, *, cwd: Path, timeout: float, env: dict[str, str],
           runner: CommandRunner, now: NowFn) -> CliResult:
    return execute("claude", "claude", lambda exe: build_command(exe, spec), model=spec.model,
                   effort=spec.effort, cwd=cwd, timeout=timeout, env=env, runner=runner, now=now,
                   parse=_parse)


# ---------------- Reset 券（唯讀）----------------
FALLBACK_VERSION = "2.1.322"


def detect_version(env: dict[str, str]) -> str:
    """取得本機 claude CLI 實際版本。伺服器依 User-Agent 版本判斷資格（過舊 → cli_version 不適用）。"""
    exe = find_executable("claude", env)
    if exe:
        try:
            p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10,
                               env=with_node_path(env, exe), stdin=subprocess.DEVNULL)
            m = re.search(r"\d+\.\d+\.\d+", p.stdout)
            if m:
                return m.group(0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return FALLBACK_VERSION


def _iso(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_usage(data: dict, now: datetime) -> CreditsSnapshot:
    """解析 cedar_ember。資料不完整的 grant 直接捨棄（保守）。"""
    windows = []
    for key in ("five_hour", "seven_day"):
        w = data.get(key)
        if isinstance(w, dict):
            windows.append(UsageWindow(key, w.get("utilization"), None, _iso(w.get("resets_at"))))
    ce = data.get("cedar_ember")
    if not isinstance(ce, dict) or ce.get("eligible") is not True:
        return CreditsSnapshot("claude", True, 0, [], windows, redemption_support="supported",
                               error=None if ce is not None else "回應不含 cedar_ember（帳號不適用或介面變更）")
    credits: list[ResetCredit] = []
    for g in ce.get("grants") or []:
        if not isinstance(g, dict):
            continue
        left, paused = g.get("resets_left"), g.get("paused")
        starts, ends = _iso(g.get("starts_at")), _iso(g.get("ends_at"))
        if not isinstance(left, int) or not isinstance(paused, bool) or not starts or not ends:
            continue
        if paused or left <= 0 or starts > now or ends <= now:
            continue
        credits.append(ResetCredit(id=str(g.get("id", "")), provider="claude", status="available",
                                   reset_type="claudeLimitReset", starts_at=starts, expires_at=ends,
                                   count=left, title=g.get("title")))
    total = sum(c.count for c in credits)
    return CreditsSnapshot("claude", True, total, credits, windows,
                           redemption_support="supported")


class ClaudeResetProvider:
    name = "claude"
    redemption_support = "supported"

    def __init__(self, home: Path, version: str = FALLBACK_VERSION, timeout: float = 15.0,
                 opener: Callable[..., object] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.cred_path = home / ".claude" / ".credentials.json"
        self.version, self.timeout = version, timeout
        self._opener = opener or urllib.request.urlopen
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def token(self) -> str | None:
        try:
            d = json.loads(self.cred_path.read_text(encoding="utf-8"))["claudeAiOauth"]
        except (OSError, KeyError, json.JSONDecodeError):
            return None
        exp = d.get("expiresAt")
        if isinstance(exp, (int, float)) and exp / 1000 < self._clock().timestamp():
            return None      # 過期：不自行刷新（避免覆寫憑證）；CLI 的 hi 觸發會自行更新
        if "user:profile" not in (d.get("scopes") or []):
            return None
        return d.get("accessToken")

    def _account(self) -> dict:
        try:
            a = json.loads((self.cred_path.parent.parent / ".claude.json").read_text(encoding="utf-8"))
            return a.get("oauthAccount") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            return {}

    def organization_uuid(self) -> str | None:
        v = self._account().get("organizationUuid")
        return v if isinstance(v, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", v) else None

    def fetch_credits(self) -> CreditsSnapshot:
        snap = self._fetch()
        acc = self._account().get("accountUuid")
        if snap.ok and isinstance(acc, str) and acc:
            snap.account_id_hash = hashlib.sha256(acc.encode()).hexdigest()[:16]
        return snap

    def _fetch(self) -> CreditsSnapshot:
        tok = self.token()
        if not tok:
            return CreditsSnapshot("claude", False, redemption_support=self.redemption_support,
                                   error="無可用的唯讀 OAuth 憑證（缺少 user:profile scope、已過期或未登入）")
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"claude-cli/{self.version} (external, cli)", "Accept": "application/json"})
        assert req.host == ALLOWED_HOST
        try:
            with self._opener(req, timeout=self.timeout) as r:      # type: ignore[operator]
                data = json.loads(r.read().decode("utf-8"))
            return parse_usage(data, self._clock())
        except urllib.error.HTTPError as e:
            return CreditsSnapshot("claude", False, redemption_support=self.redemption_support,
                                   error=f"HTTP {e.code}（不重試、不繞過驗證）")
        except Exception as e:  # noqa: BLE001
            return CreditsSnapshot("claude", False, redemption_support=self.redemption_support,
                                   error=f"查詢失敗: {type(e).__name__}")
