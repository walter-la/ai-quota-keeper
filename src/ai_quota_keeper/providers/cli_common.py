"""兩個 CLI 觸發器共用邏輯。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from ..models import CliResult, CliStatus
from ..runner import CommandRunner, ProcResult, classify, find_executable, with_node_path

NowFn = Callable[[], datetime]


def execute(cli: str, exe_name: str, build: Callable[[str], list[str]], *, model: str, effort: str,
            cwd: Path, timeout: float, env: dict[str, str], runner: CommandRunner, now: NowFn,
            parse: Callable[[ProcResult], tuple[str | None, str | None]] | None = None) -> CliResult:
    start = now()
    exe = find_executable(exe_name, env)
    if exe is None:
        return CliResult(cli, CliStatus.NOT_FOUND, model, effort, start, now(), None, [exe_name],
                         error=f"找不到 {exe_name}（PATH={env.get('PATH','')[:120]}）")
    cmd = build(exe)
    res = runner(cmd, cwd=cwd, timeout=timeout, env=with_node_path(env, exe))
    status, err = classify(res)
    em, ee = (parse(res) if parse else (None, None))
    return CliResult(cli, status, model, effort, start, now(), res.returncode, cmd, err, em, ee)
