"""日誌：application.log / error.log（輪替）+ trigger.jsonl（結構化）。含 token 遮罩。"""
from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)((?:access|refresh)_?token\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}(?:\.[A-Za-z0-9_\-]*)?"),
]


def redact(text: str) -> str:
    for p in _PATTERNS:
        if p.groups:
            text = p.sub(lambda m: m.group(1) + "***REDACTED***", text)
        else:
            text = p.sub("***REDACTED***", text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(logs_dir: Path, verbose: bool = False) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ai_quota_keeper")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if getattr(logger, "_configured", False):
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    app = RotatingFileHandler(logs_dir / "application.log", maxBytes=1_000_000, backupCount=5,
                              encoding="utf-8")
    err = RotatingFileHandler(logs_dir / "error.log", maxBytes=1_000_000, backupCount=5,
                              encoding="utf-8")
    err.setLevel(logging.WARNING)
    for h in (app, err):
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
        logger.addHandler(h)
    logger._configured = True  # type: ignore[attr-defined]
    return logger


def write_jsonl(logs_dir: Path, record: dict, max_bytes: int = 5_000_000, keep: int = 5) -> None:
    """附加一行 JSON；超過大小就輪替。內容一律遮罩。"""
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / "trigger.jsonl"
    if path.exists() and path.stat().st_size > max_bytes:
        for i in range(keep - 1, 0, -1):
            src = path.with_suffix(f".jsonl.{i}")
            if src.exists():
                src.replace(path.with_suffix(f".jsonl.{i + 1}"))
        path.replace(path.with_suffix(".jsonl.1"))
    line = redact(json.dumps(record, ensure_ascii=False, default=str))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    path.chmod(0o600)
