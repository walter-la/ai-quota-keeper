"""安全的子行程執行（無 shell）與 CLI 觸發器。"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .models import CliResult, CliStatus


@dataclass
class ProcResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    not_found: bool = False


class CommandRunner(Protocol):
    def __call__(self, cmd: list[str], *, cwd: Path, timeout: float,
                 env: dict[str, str] | None = None) -> ProcResult: ...


def subprocess_runner(cmd: list[str], *, cwd: Path, timeout: float,
                      env: dict[str, str] | None = None) -> ProcResult:
    """以獨立 process group 執行，逾時時整組終止（含孫行程）。"""
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
    except FileNotFoundError:
        return ProcResult(None, "", f"command not found: {cmd[0]}", not_found=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return ProcResult(p.returncode, out, err)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                out, err = p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                out, err = p.communicate()
        except ProcessLookupError:
            out, err = "", ""
        return ProcResult(None, out or "", err or "", timed_out=True)


_AUTH_HINTS = ("not logged in", "login", "unauthorized", "401", "authentication", "sign in",
               "please run /login", "invalid api key", "oauth")
_MODEL_HINTS = ("model", "not supported", "not found", "does not exist", "unknown model")


def classify(res: ProcResult) -> tuple[CliStatus, str | None]:
    if res.not_found:
        return CliStatus.NOT_FOUND, res.stderr
    if res.timed_out:
        return CliStatus.TIMEOUT, "執行逾時，已終止子行程"
    if res.returncode == 0:
        return CliStatus.SUCCESS, None
    text = (res.stderr + "\n" + res.stdout).lower()
    summary = _summarize(res.stderr or res.stdout)
    if any(h in text for h in _AUTH_HINTS):
        return CliStatus.AUTH_ERROR, summary
    if "model" in text and any(h in text for h in _MODEL_HINTS):
        return CliStatus.MODEL_UNSUPPORTED, summary
    return CliStatus.FAILED, summary


def _summarize(text: str, limit: int = 300) -> str:
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    return " | ".join(lines[-3:])[:limit] if lines else "(無輸出)"


def resolve_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """沿用目前環境（保留 HOME/PATH/CODEX_HOME），並確保有 HOME。"""
    env = dict(base if base is not None else os.environ)
    env.setdefault("HOME", str(Path.home()))
    return env


def find_executable(name: str, env: dict[str, str]) -> str | None:
    """解析 CLI 路徑。先找 nvm 安裝的最新版（避免排程環境 PATH 精簡時抓到舊版），
    再依 PATH，最後 ~/.local/bin。"""
    home = Path(env.get("HOME", ""))
    nvm = sorted((home / ".nvm/versions/node").glob("*/bin"), reverse=True) if env.get("HOME") else []
    for d in nvm:
        if (d / name).exists():
            return str(d / name)
    found = shutil.which(name, path=env.get("PATH"))
    if found:
        return found
    for d in (home / ".local/bin", Path("/usr/local/bin")):
        if (d / name).exists():
            return str(d / name)
    return None


def with_node_path(env: dict[str, str], exe: str) -> dict[str, str]:
    """CLI 為 node script（#!/usr/bin/env node），確保其所在目錄在 PATH 中。"""
    env = dict(env)
    d = str(Path(exe).parent)
    if d not in env.get("PATH", "").split(":"):
        env["PATH"] = d + ":" + env.get("PATH", "")
    return env
