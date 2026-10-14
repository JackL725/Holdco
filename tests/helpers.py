"""Shared test helpers: temp data folders, a Holdco subprocess, and a tiny HTTP client."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout: float = 60, interval: float = 0.5, what: str = "condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as e:  # server busy or restarting
            last = e
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s waiting for {what} (last: {last!r})")


class Client:
    def __init__(self, base: str):
        self.base = base

    def raw(self, method: str, path: str, body=None, headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _opener.open(req, timeout=60) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def call(self, method: str, path: str, body=None, expect: int | None = 200):
        headers = {} if method == "GET" else {"X-Holdco": "1"}
        status, raw, _ = self.raw(method, path, body, headers)
        data = json.loads(raw.decode() or "{}")
        if expect is not None and status != expect:
            raise AssertionError(f"{method} {path} -> {status}: {data}")
        return data

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)

    def put(self, path, body, **kw):
        return self.call("PUT", path, body, **kw)

    def patch(self, path, body, **kw):
        return self.call("PATCH", path, body, **kw)

    def delete(self, path, body=None, **kw):
        return self.call("DELETE", path, body if body is not None else {}, **kw)


class HoldcoProcess:
    """Runs `python run.py` against a temporary data folder."""

    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir or tempfile.mkdtemp(prefix="holdco-test-")
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.client = Client(f"http://127.0.0.1:{self.port}")
        self.log_path = Path(self.data_dir) / "server.log"

    def start(self):
        env = {**os.environ, "HOLDCO_DATA": self.data_dir, "HOLDCO_PORT": str(self.port),
               "HOLDCO_TICK_SECONDS": "1", "HOLDCO_ROUTINE_CHECK_SECONDS": "1", "PYTHONUNBUFFERED": "1"}
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "a", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--no-browser", "--port", str(self.port)],
                                     cwd=str(ROOT), env=env, stdout=self._log, stderr=subprocess.STDOUT)
        wait_for(lambda: self.client.get("/api/state"), 30, 0.3, "Holdco to start")
        return self

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        if getattr(self, "_log", None):
            self._log.close()

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""
