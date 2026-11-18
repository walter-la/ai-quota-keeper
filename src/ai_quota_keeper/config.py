"""設定載入：config.toml（可選）+ 程式內預設值。"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CliSpec:
    model: str
    effort: str
    prompt: str = "hi"


@dataclass(frozen=True)
class Config:
    root: Path = PROJECT_ROOT
    timezone: str = "Asia/Taipei"
    triggers: tuple[time, ...] = (time(7, 0), time(12, 30), time(18, 0), time(23, 30))
    tolerance_seconds: int = 120
    cli_timeout_seconds: int = 60
    codex: CliSpec = CliSpec("gpt-6-luna", "none")
    claude: CliSpec = CliSpec("claude-haiku-5-5", "low")
    redemption_enabled: bool = True
    workdir_codex: Path = field(default=PROJECT_ROOT / "data" / "workdir-codex")
    workdir_claude: Path = field(default=PROJECT_ROOT / "data" / "workdir-claude")
    timer_unit: str = "ai-quota-keeper.timer"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "run.lock"


def _parse_time(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def load_config(root: Path | None = None, path: Path | None = None) -> Config:
    root = root or PROJECT_ROOT
    path = path or root / "config.toml"
    base = Config(root=root, workdir_codex=root / "data" / "workdir-codex",
                  workdir_claude=root / "data" / "workdir-claude")
    if not path.exists():
        return base
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    kw: dict = {}
    for k in ("timezone", "tolerance_seconds", "cli_timeout_seconds"):
        if k in raw:
            kw[k] = raw[k]
    if "triggers" in raw:
        kw["triggers"] = tuple(sorted(_parse_time(t) for t in raw["triggers"]))
    for name in ("codex", "claude"):
        if name in raw:
            d = getattr(base, name)
            kw[name] = CliSpec(raw[name].get("model", d.model), raw[name].get("effort", d.effort),
                               raw[name].get("prompt", d.prompt))
    if "redemption" in raw:
        kw["redemption_enabled"] = bool(raw["redemption"].get("enabled", True))
    from dataclasses import replace
    return replace(base, **kw)
