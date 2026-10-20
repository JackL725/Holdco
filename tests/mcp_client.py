"""A minimal MCP client for the Phase 4 tests, built on the Phase 1 client (holdco.mcp.Client): it can pin the
protocol generation (the Phase 1 client always tries 2026-07-28 first) and the clientInfo name, and it sends the
dashboard's Basic auth. It drives Holdco's own /mcp endpoint and the stdio proxy (python -m holdco.mcp_server).

    c = http_client(app.port, password="pw", generation="old", client_name="desktop").connect()
    c = stdio_client(app.port, password="pw").connect()
    status, headers, reply = post(app.port, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from holdco import mcp  # noqa: E402

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def basic_auth(password: str) -> str:
    return "Basic " + base64.b64encode(f"owner:{password}".encode("utf-8")).decode("ascii")


class McpClient(mcp.Client):
    """holdco.mcp.Client with a chosen generation ("new": server/discover, "old": initialize) and clientInfo."""

    def __init__(self, transport: str, *, generation: str = "new", client_name: str = "mcp-test",
                 password: str | None = None, **kw):
        headers = dict(kw.pop("headers", None) or {})
        if password:
            headers["Authorization"] = basic_auth(password)
        super().__init__("holdco", transport, headers=headers, **kw)
        self.generation = generation
        self.client_info = {"name": client_name, "version": "1.0"}

    def _negotiate(self) -> None:
        if self.generation == "old":
            return self._initialize()
        return super()._negotiate()

    def _send(self, method: str, params: dict | None, timeout: float, generation: str) -> dict:
        # mcp.Client._send with this client's clientInfo in place of Holdco's own
        if self._t is None:
            raise mcp.MCPError("holdco: not connected")
        params = dict(params or {})
        if method == "initialize":
            params["clientInfo"] = dict(self.client_info)
        if generation == "new":
            params = mcp._meta_for(method, params)
            params["_meta"][mcp.META_CLIENT] = dict(self.client_info)
        msg = {"jsonrpc": "2.0", "id": self._new_id(), "method": method, "params": params}
        result = mcp._check_result(self._t.request(msg, timeout, generation, self._new_id))
        info = (result.get("_meta") or {}).get(mcp.META_SERVER) if isinstance(result.get("_meta"), dict) else None
        if isinstance(info, dict):
            self.server_info = info
        return result

    def request(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        """Any request in the agreed generation; returns the result (raises mcp.RPCError on an error reply)."""
        return self._request(method, params, timeout)


def http_client(port: int, **kw) -> McpClient:
    return McpClient("http", url=f"http://127.0.0.1:{port}/mcp", **kw)


def stdio_client(port: int, password: str = "", **kw) -> McpClient:
    """python -m holdco.mcp_server pointed at the dashboard on this port (the env a client config would set)."""
    env = {"PYTHONPATH": str(ROOT), "HOLDCO_PORT": str(port), "HOLDCO_PASSWORD": password or "",
           "PYTHONIOENCODING": "utf-8"}
    return McpClient("stdio", command=sys.executable, args=["-m", "holdco.mcp_server"], env=env, **kw)


def post(port: int, body, headers: dict | None = None, method: str = "POST", raw: bytes | None = None,
         path: str = "/mcp"):
    """One HTTP request to the dashboard: (status, headers, the JSON reply or None)."""
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _direct.open(req, timeout=60) as r:
            status, out, hdrs = r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        status, out, hdrs = e.code, e.read(), dict(e.headers)
        e.close()
    try:
        reply = json.loads(out.decode("utf-8")) if out.strip() else None
    except ValueError:
        reply = None
    return status, hdrs, reply
