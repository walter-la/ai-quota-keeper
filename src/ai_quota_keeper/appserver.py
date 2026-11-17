"""Codex App Server（JSON-RPC over stdio）極簡客戶端：initialize 握手、request/response、逾時、行程生命週期。"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Protocol

CONSUME_METHOD = "account/rateLimitResetCredit/consume"
READ_METHOD = "account/rateLimits/read"


class AppServerError(Exception):
    """一般錯誤（含 JSON-RPC error）。"""


class AppServerTimeout(AppServerError):
    pass


class AppServerAuthError(AppServerError):
    pass


class Transport(Protocol):
    def send(self, msg: dict) -> None: ...
    def recv(self, timeout: float) -> dict | None: ...
    def close(self) -> None: ...


class StdioTransport:
    """啟動 `codex app-server`（stdio）。不載入任何使用者專案。"""

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None, cwd: str | None = None):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env=env, cwd=cwd,
                                     start_new_session=True)
        self._q: queue.Queue[dict | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self._q.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        self._q.put(None)

    def send(self, msg: dict) -> None:
        assert self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise AppServerError(f"app-server 已結束: {e}") from e

    def recv(self, timeout: float) -> dict | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            raise AppServerTimeout("等待 app-server 回應逾時") from None

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except OSError:
            pass


class AppServerClient:
    """握手後可呼叫 request()；notification 與無關訊息會被略過。"""

    def __init__(self, transport_factory: Callable[[], Transport], timeout: float = 20.0,
                 client_name: str = "ai_quota_keeper", client_version: str = "0.1.0") -> None:
        self._factory = transport_factory
        self.timeout = timeout
        self._name, self._ver = client_name, client_version
        self._t: Transport | None = None
        self._next_id = 0

    def __enter__(self) -> "AppServerClient":
        self._t = self._factory()
        self._call("initialize", {"clientInfo": {"name": self._name, "title": self._name,
                                                 "version": self._ver}})
        self._t.send({"method": "initialized"})
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._t:
            self._t.close()
            self._t = None

    def request(self, method: str, params: dict | None = None) -> dict:
        return self._call(method, params)

    def _call(self, method: str, params: dict | None) -> dict:
        assert self._t
        self._next_id += 1
        rid = self._next_id
        msg: dict[str, Any] = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._t.send(msg)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTimeout(f"{method} 逾時")
            m = self._t.recv(remaining)
            if m is None:
                raise AppServerError("app-server 提前結束")
            if m.get("id") != rid or "method" in m:   # notification / server request
                continue
            if "error" in m:
                err = m["error"] or {}
                text = f"{err.get('code')}: {err.get('message')}"
                if any(k in text.lower() for k in ("auth", "401", "unauthorized", "log in",
                                                   "sign in", "token")):
                    raise AppServerAuthError(text)
                raise AppServerError(text)
            return m.get("result") or {}


def codex_transport_factory(codex_path: str, env: dict[str, str], cwd: str) -> Callable[[], Transport]:
    return lambda: StdioTransport([codex_path, "app-server"], env=env, cwd=cwd)
