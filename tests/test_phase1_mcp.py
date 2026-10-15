"""Phase 1 tests for the MCP client (holdco/mcp.py) and OAuth (holdco/oauth.py) against tests/fake_mcp_server.py
and tests/fake_oauth.py. The stdio fake runs with sys.executable, so no Node is needed.

Run with:  python -m unittest tests.test_phase1_mcp -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-mcp-"))
os.environ.setdefault("HOLDCO_VAULT_BACKEND", "file")   # never touch a developer's keyring

from holdco import config, db, mcp, oauth, server, vault  # noqa: E402
from tests.fake_mcp_server import META_CAPABILITIES, META_CLIENT, META_VERSION, FakeMCPHttp  # noqa: E402
from tests.fake_oauth import FakeOAuth, s256  # noqa: E402

db.init()

FAKE = str(ROOT / "tests" / "fake_mcp_server.py")
TOOL_NAMES = {"echo", "add", "fail", "image", "slow", "input_required"}


def stdio_client(*flags, env=None, name="fake") -> mcp.Client:
    return mcp.Client(name, "stdio", command=sys.executable, args=[FAKE, "--stdio", *flags], env=env)


def script_client(code: str, env=None, name="script") -> mcp.Client:
    """A stdio 'server' that is a few lines of Python (for crashes and odd behaviour)."""
    return mcp.Client(name, "stdio", command=sys.executable, args=["-c", code], env=env)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


_no_redirect = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FakesCase(unittest.TestCase):
    def http_fake(self, **kw) -> FakeMCPHttp:
        fake = FakeMCPHttp(**kw).start()
        self.addCleanup(fake.stop)
        return fake

    def oauth_fake(self, **kw) -> FakeOAuth:
        fake = FakeOAuth(**kw).start()
        self.addCleanup(fake.stop)
        return fake

    def connected(self, client: mcp.Client) -> mcp.Client:
        self.addCleanup(client.close)
        return client.connect()


# ---------------- the MCP client ----------------

class TestMCP(FakesCase):
    def test_stdio_current_generation(self):
        c = self.connected(stdio_client())
        self.assertEqual(c.protocol, "2026-07-28")
        self.assertEqual(c.server_info.get("name"), "fake-mcp")
        self.assertEqual(c.server_versions, ["2026-07-28"])
        tools = c.list_tools()
        self.assertEqual({t["name"] for t in tools}, TOOL_NAMES)
        for t in tools:
            self.assertTrue(t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")
        self.assertEqual(c.tools_info, {"ttlMs": 60000})   # passed through, not acted on
        self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "hello there"})), "hello there")
        failed = c.call_tool("fail", {"text": "card declined"})
        self.assertTrue(failed["isError"])
        self.assertEqual(mcp.text_of(failed), "ERROR: card declined")
        self.assertEqual(mcp.text_of(c.call_tool("image")), "[image image/png, 70 bytes]")
        added = c.call_tool("add", {"a": 2, "b": 3})
        self.assertEqual(added["structuredContent"], {"sum": 5})
        self.assertFalse(added["isError"])
        self.assertEqual(mcp.text_of(added), "5")
        self.assertIsNone(c.call_tool("echo", {"text": "x"})["structuredContent"])

    def test_stdio_older_generation(self):
        c = self.connected(stdio_client("--old"))
        self.assertEqual(c.protocol, "2025-11-25")
        self.assertEqual(c.server_info.get("name"), "fake-mcp")
        self.assertEqual({t["name"] for t in c.list_tools()}, TOOL_NAMES)
        self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "old but fine"})), "old but fine")
        self.assertEqual(c.call_tool("add", {"a": 1, "b": 1})["structuredContent"], {"sum": 2})

    def test_stdio_replies_are_matched_by_id(self):
        c = self.connected(stdio_client())
        done: dict = {}

        def slow():
            done["slow_text"] = mcp.text_of(c.call_tool("slow", {"seconds": 2.0}))
            done["slow_at"] = time.monotonic()

        t = threading.Thread(target=slow)
        t.start()
        time.sleep(0.2)
        self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "first"})), "first")
        echo_at = time.monotonic()
        t.join(30)
        self.assertEqual(done.get("slow_text"), "waited 2 s")
        self.assertLess(echo_at, done["slow_at"])   # the quick call did not wait behind the slow one

    def test_stdio_resolves_the_command_with_which(self):
        # On Windows "npx" is npx.cmd; shutil.which finds it. Here "npx" resolves to Python running the fake.
        real_which = mcp.shutil.which

        def which(cmd, *a, **kw):
            return sys.executable if cmd == "npx" else real_which(cmd, *a, **kw)
        with mock.patch.object(mcp.shutil, "which", side_effect=which):
            c = self.connected(mcp.Client("npx-like", "stdio", command="npx", args=[FAKE, "--stdio"]))
        self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "found"})), "found")

    def test_stdio_command_not_found(self):
        with self.assertRaises(mcp.MCPError) as cm:
            mcp.Client("ghost", "stdio", command="holdco-no-such-command-4711").connect()
        self.assertIn("not found", str(cm.exception))

    def test_stdio_env_is_merged_over_the_environment(self):
        code = ("import os, sys; sys.stderr.write('vars: %s %s' % (os.environ.get('HOLDCO_T_PARENT'), "
                "os.environ.get('HOLDCO_T_CLIENT'))); sys.exit(2)")
        with mock.patch.dict(os.environ, {"HOLDCO_T_PARENT": "from-parent"}):
            with self.assertRaises(mcp.MCPError) as cm:
                script_client(code, env={"HOLDCO_T_CLIENT": "from-client"}).connect()
        self.assertIn("vars: from-parent from-client", str(cm.exception))

    def test_stdio_stderr_tail_when_the_process_exits_at_start(self):
        code = "import sys; sys.stderr.write('boom: the database is locked\\n'); sys.exit(3)"
        started = time.monotonic()
        with self.assertRaises(mcp.MCPError) as cm:
            script_client(code).connect()
        self.assertLess(time.monotonic() - started, 15)   # noticed at once, not after the 30 s connect timeout
        self.assertIn("boom: the database is locked", str(cm.exception))
        self.assertIn("code 3", str(cm.exception))

    def test_stdio_stderr_tail_when_the_process_crashes_mid_call(self):
        c = self.connected(stdio_client(env={"FAKE_MCP_CRASH_ON": "echo"}))
        self.assertEqual(mcp.text_of(c.call_tool("add", {"a": 1, "b": 2})), "3")
        started = time.monotonic()
        with self.assertRaises(mcp.MCPError) as cm:
            c.call_tool("echo", {"text": "bye"})
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("simulated crash", str(cm.exception))
        self.assertIn("fake-mcp: ready", str(cm.exception))   # earlier stderr lines are kept too
        self.assertFalse(c.alive)
        with self.assertRaises(mcp.MCPError) as cm:
            c.call_tool("add", {"a": 1, "b": 2})
        self.assertIn("simulated crash", str(cm.exception))

    def test_stdio_stderr_ring_buffer_keeps_the_last_4000_characters(self):
        code = "import sys; sys.stderr.write('START' + 'x' * 9000 + 'THE-END'); sys.stderr.flush(); sys.exit(1)"
        with self.assertRaises(mcp.MCPError) as cm:
            script_client(code).connect()
        text = str(cm.exception)
        self.assertIn("THE-END", text)
        self.assertNotIn("START", text)
        self.assertLess(text.count("x"), 4001)

    def test_stdio_close_ends_the_process_and_joins_the_readers(self):
        c = stdio_client().connect()
        proc = c._t.proc
        c.close()
        self.assertIsNotNone(proc.poll())
        self.assertFalse(any(t.is_alive() for t in c._t._threads))
        self.assertFalse(c.alive)
        c.close()   # twice is fine
        with self.assertRaises(mcp.MCPError):
            c.call_tool("echo", {"text": "after close"})

    def test_stdio_close_kills_a_server_that_ignores_terminate(self):
        code = ("import signal, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "sys.stdin.read()\n"
                "time.sleep(60)\n")
        t = mcp._StdioTransport("stubborn", sys.executable, ["-c", code])
        t.start()
        time.sleep(0.3)
        started = time.monotonic()
        with mock.patch.object(mcp, "KILL_AFTER", 0.5):
            t.close()
        self.assertIsNotNone(t.proc.poll())
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(any(th.is_alive() for th in t._threads))

    def test_http_json_current_generation_headers_and_meta(self):
        fake = self.http_fake()
        c = self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url))
        self.assertEqual(c.protocol, "2026-07-28")
        self.assertEqual({t["name"] for t in c.list_tools()}, TOOL_NAMES)
        self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "over http"})), "over http")
        self.assertEqual(c.call_tool("add", {"a": 4, "b": 5})["structuredContent"], {"sum": 9})
        self.assertEqual([call.method for call in fake.calls],
                         ["server/discover", "tools/list", "tools/call", "tools/call"])
        for call in fake.calls:   # stateless: no initialize, no ping, no notifications, the same keys every time
            meta = call.params["_meta"]
            self.assertEqual(meta[META_VERSION], "2026-07-28")
            self.assertEqual(meta[META_CAPABILITIES], {})
            self.assertEqual(meta[META_CLIENT]["name"], "holdco")
            self.assertEqual(call.headers.get("Mcp-Method"), call.method)
            self.assertNotIn("Mcp-Session-Id", call.headers)
            self.assertNotIn("MCP-Protocol-Version", call.headers)
            self.assertEqual(call.headers.get("Content-Type"), "application/json")
            self.assertIn("text/event-stream", call.headers.get("Accept"))
        names = [call.headers.get("Mcp-Name") for call in fake.calls if call.method == "tools/call"]
        self.assertEqual(names, ["echo", "add"])

    def test_http_sse_both_generations(self):
        for old in (False, True):
            with self.subTest(old=old):
                fake = self.http_fake(sse=True, old=old)
                c = self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url))
                self.assertEqual(c.protocol, "2025-11-25" if old else "2026-07-28")
                self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "streamed"})), "streamed")
                self.assertEqual(len(c.list_tools()), len(TOOL_NAMES))
                self.assertTrue(any(r.get("sse") for r in fake.requests))

    def test_http_sse_broken_stream_is_reissued_with_a_new_id(self):
        fake = self.http_fake(sse=True)
        c = self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url))
        fake.break_streams = 1
        self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "again"})), "again")
        ids = [call.id for call in fake.calls if call.method == "tools/call"]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        fake.break_streams = 2   # broken twice: give up
        with self.assertRaises(mcp.MCPError) as cm:
            c.call_tool("echo", {"text": "never"})
        self.assertIn("broke", str(cm.exception))

    def test_http_older_generation_session_and_version_headers(self):
        fake = self.http_fake(old=True)
        c = self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url))
        self.assertEqual(c.protocol, "2025-11-25")
        self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "session"})), "session")
        methods = [call.method for call in fake.calls]
        self.assertEqual(methods[:3], ["server/discover", "initialize", "notifications/initialized"])
        self.assertEqual(len(fake.sessions), 1)
        sid = next(iter(fake.sessions))
        init = fake.calls[1]
        self.assertNotIn("Mcp-Session-Id", init.headers)
        self.assertNotIn("MCP-Protocol-Version", init.headers)
        self.assertEqual(init.params["protocolVersion"], "2025-11-25")
        self.assertEqual(init.params["clientInfo"]["name"], "holdco")
        self.assertNotIn("_meta", init.params)
        for call in fake.calls[2:]:
            self.assertEqual(call.headers.get("Mcp-Session-Id"), sid)
            self.assertEqual(call.headers.get("MCP-Protocol-Version"), "2025-11-25")
            self.assertNotIn("Mcp-Method", call.headers)   # those belong to the current generation
            self.assertNotIn(META_VERSION, call.params.get("_meta") or {})
        c.close()
        self.assertEqual(fake.sessions, set())   # the session was ended with DELETE

    def test_http_older_generation_starts_a_new_session_after_404(self):
        fake = self.http_fake(old=True)
        c = self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url))
        first = next(iter(fake.sessions))
        fake.expire_sessions()
        self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "renewed"})), "renewed")
        self.assertEqual([call.method for call in fake.calls].count("initialize"), 2)
        self.assertNotEqual(next(iter(fake.sessions)), first)

    def test_http_falls_back_on_an_http_error_status(self):
        for status in (404, 500):
            with self.subTest(status=status):
                fake = self.http_fake(old=True)
                fake.discover_status = status
                c = self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url))
                self.assertEqual(c.protocol, "2025-11-25")
                self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "ok"})), "ok")

    def test_http_static_headers_on_every_request(self):
        fake = self.http_fake()
        c = self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url,
                                      headers={"Authorization": "Bearer static-key-123", "X-Extra": "yes"}))
        c.call_tool("echo", {"text": "x"})
        for call in fake.calls:
            self.assertEqual(call.headers.get("Authorization"), "Bearer static-key-123")
            self.assertEqual(call.headers.get("X-Extra"), "yes")

    def test_input_required_becomes_an_error_text(self):
        fake = self.http_fake()
        for c in (self.connected(stdio_client()), self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url))):
            with self.subTest(transport=c.transport):
                with self.assertRaises(mcp.MCPError) as cm:
                    c.call_tool("input_required")
                self.assertEqual(str(cm.exception), "the server needs input Holdco cannot provide")
                self.assertEqual(f"ERROR: {cm.exception}", "ERROR: the server needs input Holdco cannot provide")
                self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "still here"})), "still here")
        self.assertEqual(mcp.text_of({"resultType": "input_required"}),
                         "ERROR: the server needs input Holdco cannot provide")

    def test_unsupported_protocol_version(self):
        fake = self.http_fake()
        fake.supported = ["2099-01-01"]
        with self.assertRaises(mcp.MCPError) as cm:
            mcp.Client("fakemcp", "http", url=fake.mcp_url).connect()
        self.assertEqual(str(cm.exception), "unsupported protocol version")
        self.assertNotIn("initialize", [call.method for call in fake.calls])   # -32022 is not a fallback signal

        fake2 = self.http_fake()
        c = self.connected(mcp.Client("fakemcp", "http", url=fake2.mcp_url))
        fake2.supported = ["2099-01-01"]   # the server moved on mid-session
        with self.assertRaises(mcp.MCPError) as cm:
            c.call_tool("echo", {"text": "x"})
        self.assertEqual(str(cm.exception), "unsupported protocol version")

        old = self.http_fake(old=True)
        old.old_version = "2024-11-05"
        with self.assertRaises(mcp.MCPError) as cm:
            mcp.Client("fakemcp", "http", url=old.mcp_url).connect()
        self.assertEqual(str(cm.exception), "unsupported protocol version")
        for v in ("2025-06-18", "2025-03-26"):
            old.old_version = v
            c = self.connected(mcp.Client("fakemcp", "http", url=old.mcp_url))
            self.assertEqual(c.protocol, v)

    def test_401_raises_auth_required_with_the_metadata_url(self):
        auth = self.oauth_fake()
        fake = self.http_fake(auth_base=auth.url)
        with self.assertRaises(mcp.AuthRequired) as cm:
            mcp.Client("fakemcp", "http", url=fake.mcp_url).connect()
        self.assertIsInstance(cm.exception, mcp.MCPError)
        self.assertEqual(cm.exception.resource_metadata_url, fake.url + "/.well-known/oauth-protected-resource")
        self.assertEqual(fake.calls, [])   # nothing got past the 401, and no fallback was tried
        self.assertEqual([r["status"] for r in fake.requests], [401])

        fake.challenge = 'Bearer realm="fake", resource_metadata="/custom/prm", scope="tools:read tools:call"'
        with self.assertRaises(mcp.AuthRequired) as cm:
            mcp.Client("fakemcp", "http", url=fake.mcp_url).connect()
        self.assertEqual(cm.exception.resource_metadata_url, fake.url + "/custom/prm")
        self.assertEqual(cm.exception.scope, "tools:read tools:call")

        fake.challenge = 'Bearer error="invalid_token"'
        with self.assertRaises(mcp.AuthRequired) as cm:
            mcp.Client("fakemcp", "http", url=fake.mcp_url).connect()
        self.assertEqual(cm.exception.resource_metadata_url, fake.url + "/.well-known/oauth-protected-resource")

    def test_slow_call_times_out(self):
        fake = self.http_fake()
        for c in (self.connected(stdio_client()), self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url))):
            with self.subTest(transport=c.transport):
                started = time.monotonic()
                with mock.patch.object(mcp, "CALL_TIMEOUT", 1.0):
                    with self.assertRaises(mcp.MCPError) as cm:
                        c.call_tool("slow", {"seconds": 200})
                self.assertLess(time.monotonic() - started, 10)
                self.assertIn("within 1 s", str(cm.exception))
                self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "after"})), "after")

    def test_constructor_checks(self):
        with self.assertRaises(mcp.MCPError):
            mcp.Client("x", "websocket", url="ws://127.0.0.1:1")
        with self.assertRaises(mcp.MCPError):
            mcp.Client("x", "http")
        with self.assertRaises(mcp.MCPError):
            mcp.Client("x", "stdio")

    def test_unreachable_http_server(self):
        fake = self.http_fake()
        url = fake.mcp_url
        fake.stop()
        with self.assertRaises(mcp.MCPError) as cm:
            mcp.Client("gone", "http", url=url).connect()
        self.assertIn("cannot reach", str(cm.exception))


class TestMCPHelpers(unittest.TestCase):
    def test_text_of(self):
        self.assertEqual(mcp.text_of({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}),
                         "a\nb")
        self.assertEqual(mcp.text_of({"content": [], "structuredContent": {"sum": 3}}), '{"sum": 3}')
        self.assertEqual(mcp.text_of({"content": [{"type": "text", "text": "given"}], "structuredContent": {"x": 1}}),
                         "given")
        self.assertEqual(mcp.text_of({"content": [{"type": "text", "text": "nope"}], "isError": True}), "ERROR: nope")
        block = {"type": "resource", "resource": {"uri": "file:///a.txt", "mimeType": "text/plain", "text": "héllo"}}
        self.assertEqual(mcp.text_of({"content": [block]}), "[resource file:///a.txt, text/plain, 6 bytes]")
        blob = {"type": "resource", "resource": {"uri": "file:///b.bin", "mimeType": "application/octet-stream",
                                                 "blob": "AAECAw=="}}
        self.assertEqual(mcp.text_of({"content": [blob]}),
                         "[resource file:///b.bin, application/octet-stream, 4 bytes]")
        self.assertEqual(mcp.text_of({"content": [{"type": "image", "data": "AAEC", "mimeType": "image/gif"}]}),
                         "[image image/gif, 3 bytes]")
        self.assertEqual(mcp.text_of({"content": []}), "")

    def test_parse_sse(self):
        lines = [": keep-alive\n", "\n",
                 "event: message\r\n", 'data: {"jsonrpc": "2.0", "method": "notifications/progress"}\r\n', "\r\n",
                 "id: 7\n", "event: message\n", 'data: {"jsonrpc": "2.0",\n', 'data:  "id": 3, "result": {}}\n', "\n",
                 "data: not json\n", "\n",
                 'data: {"jsonrpc": "2.0", "id": 4, "result": {"tail": true}}\n']   # no closing blank line
        got = list(mcp._parse_sse(iter(lines)))
        self.assertEqual(got, [{"jsonrpc": "2.0", "method": "notifications/progress"},
                               {"jsonrpc": "2.0", "id": 3, "result": {}},
                               {"jsonrpc": "2.0", "id": 4, "result": {"tail": True}}])

    def test_meta_for_and_check_result(self):
        params = mcp._meta_for("tools/call", {"name": "echo", "_meta": {"progressToken": 5}})
        self.assertEqual(params["name"], "echo")
        self.assertEqual(params["_meta"]["progressToken"], 5)
        self.assertEqual(params["_meta"][mcp.META_VERSION], "2026-07-28")
        self.assertEqual(params["_meta"][mcp.META_CAPABILITIES], {})
        self.assertEqual(params["_meta"][mcp.META_CLIENT]["name"], "holdco")
        self.assertEqual(mcp._check_result({"id": 1, "result": {"resultType": "complete", "x": 1}})["x"], 1)
        self.assertEqual(mcp._check_result({"id": 1, "result": {"x": 2}})["x"], 2)
        with self.assertRaises(mcp.MCPError) as cm:
            mcp._check_result({"id": 1, "result": {"resultType": "input_required"}})
        self.assertEqual(str(cm.exception), mcp.INPUT_REQUIRED)
        with self.assertRaises(mcp.RPCError) as cm:
            mcp._check_result({"id": 1, "error": {"code": -32022, "message": "Unsupported protocol version"}})
        self.assertEqual((cm.exception.code, str(cm.exception)), (-32022, "unsupported protocol version"))
        with self.assertRaises(mcp.RPCError) as cm:
            mcp._check_result({"id": 1, "error": {"code": -32602, "message": "Unknown tool: nope"}})
        self.assertEqual(cm.exception.code, -32602)
        self.assertIn("Unknown tool: nope", str(cm.exception))

    def test_www_authenticate_parsing(self):
        e = mcp._auth_required("https://mcp.example.com/mcp",
                               'Bearer error="invalid_token", resource_metadata="https://mcp.example.com/.well-known/'
                               'oauth-protected-resource/mcp"')
        self.assertEqual(e.resource_metadata_url, "https://mcp.example.com/.well-known/oauth-protected-resource/mcp")
        e = mcp._auth_required("https://mcp.example.com/v1/mcp", "Bearer")
        self.assertEqual(e.resource_metadata_url, "https://mcp.example.com/.well-known/oauth-protected-resource")


# ---------------- OAuth ----------------

class TestOAuth(FakesCase):
    def setUp(self):
        self.store: dict[str, str] = {}
        for name, fn in (("put", lambda n, v: self.store.__setitem__(n, v)),
                         ("get", lambda n: self.store.get(n)),
                         ("delete", lambda n: self.store.pop(n, None))):
            p = mock.patch.object(vault, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        for cache in (oauth._meta, oauth._registered, oauth._pending):
            cache.clear()
        self.addCleanup(oauth._pending.clear)

    def setup_servers(self, **oauth_kw):
        self.auth = self.oauth_fake(**oauth_kw)
        self.mcpfake = self.http_fake(auth_base=self.auth.url)
        return self.auth, self.mcpfake

    def make_connector(self, url: str, key: str = "fakemcp", **cfg) -> int:
        cid = db.run("INSERT INTO connectors(company_id, key, enabled, config, status, detail, created_at)"
                     " VALUES(NULL, ?, 1, ?, 'bad', 'authorization required', ?)",
                     (key, json.dumps({"url": url, **cfg}), db.now()))
        self.addCleanup(db.run, "DELETE FROM connectors WHERE id=?", (cid,))
        return cid

    @staticmethod
    def query(url: str) -> dict:
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}

    def follow(self, url: str) -> tuple[str, dict]:
        """Open the authorization URL without following the redirect: the callback URL and its query."""
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _no_redirect.open(url, timeout=10)
        try:
            self.assertEqual(cm.exception.code, 302)
            location = cm.exception.headers["Location"]
        finally:
            cm.exception.close()
        return location, self.query(location)

    def sign_in(self, cid: int) -> dict:
        _, callback = self.follow(oauth.start(cid))
        status, page = oauth.callback_page(callback)
        self.assertEqual(status, 200)
        self.assertIn("Connected", page)
        return json.loads(self.store[f"oauth.{cid}"])

    def test_full_flow_then_an_mcp_call_with_the_bearer_token(self):
        auth, fake = self.setup_servers()
        cid = self.make_connector(fake.mcp_url)
        url = oauth.start(cid)
        q = self.query(url)
        self.assertTrue(url.startswith(auth.url + "/authorize?"))
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(len(q["code_challenge"]), 43)
        self.assertTrue(q["state"])
        self.assertEqual(q["resource"], fake.mcp_url)
        self.assertEqual(q["client_id"], "fake-client")
        redirect = f"http://127.0.0.1:{config.PORT}/oauth/callback"
        self.assertEqual(q["redirect_uri"], redirect)
        self.assertEqual(len(auth.registrations), 1)
        self.assertEqual(auth.registrations[0]["redirect_uris"], [redirect])
        self.assertEqual(auth.registrations[0]["token_endpoint_auth_method"], "none")

        location, callback = self.follow(url)
        self.assertTrue(location.startswith(redirect + "?"))
        self.assertEqual(callback["state"], q["state"])
        self.assertEqual(callback["iss"], auth.url)
        status, page = oauth.callback_page(callback)
        self.assertEqual(status, 200)
        self.assertIn("Connected fakemcp. You can close this tab.", page)

        stored = json.loads(self.store[f"oauth.{cid}"])
        self.assertEqual(set(stored), {"access_token", "refresh_token", "expires_at", "issuer", "resource",
                                       "client_id", "token_endpoint"})
        self.assertEqual(stored["access_token"], auth.tokens[-1]["access_token"])
        self.assertEqual(stored["refresh_token"], auth.tokens[-1]["refresh_token"])
        self.assertEqual((stored["issuer"], stored["resource"], stored["client_id"], stored["token_endpoint"]),
                         (auth.url, fake.mcp_url, "fake-client", auth.url + "/token"))
        self.assertAlmostEqual(stored["expires_at"], time.time() + 3600, delta=60)
        verifier = auth.token_requests[0]["code_verifier"]
        self.assertEqual(s256(verifier), q["code_challenge"])
        self.assertEqual(auth.token_requests[0]["resource"], fake.mcp_url)
        row = db.one("SELECT * FROM connectors WHERE id=?", (cid,))
        self.assertEqual((row["status"], row["detail"]), ("", ""))   # the framework re-checks its health

        c = self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url, auth=oauth.TokenSource(cid)))
        self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "signed in"})), "signed in")
        self.assertTrue(fake.calls)
        for call in fake.calls:
            self.assertEqual(call.headers.get("Authorization"), "Bearer " + stored["access_token"])

        with self.assertRaises(oauth.OAuthError):   # a callback works once
            oauth.finish(callback)

    def test_refresh_when_the_token_is_about_to_expire(self):
        auth, fake = self.setup_servers(expires_in=1)
        cid = self.make_connector(fake.mcp_url)
        first = self.sign_in(cid)
        self.assertEqual(len(auth.tokens), 1)
        c = self.connected(mcp.Client("fakemcp", "http", url=fake.mcp_url, auth=oauth.TokenSource(cid)))
        self.assertEqual(mcp.text_of(c.call_tool("echo", {"text": "fresh"})), "fresh")
        self.assertGreaterEqual(len(auth.tokens), 2)
        self.assertEqual({t["grant_type"] for t in auth.tokens[1:]}, {"refresh_token"})
        latest = json.loads(self.store[f"oauth.{cid}"])
        self.assertNotEqual(latest["access_token"], first["access_token"])
        self.assertEqual(latest["access_token"], auth.tokens[-1]["access_token"])
        self.assertEqual(fake.calls[-1].headers.get("Authorization"), "Bearer " + latest["access_token"])
        refresh = [r for r in auth.token_requests if r["grant_type"] == "refresh_token"][0]
        self.assertEqual((refresh["client_id"], refresh["resource"]), ("fake-client", fake.mcp_url))

    def test_token_source_refuses_another_origin(self):
        auth, fake = self.setup_servers()
        cid = self.make_connector(fake.mcp_url)
        stored = self.sign_in(cid)
        source = oauth.TokenSource(cid)
        self.assertEqual(source.token(fake.mcp_url), stored["access_token"])
        for other in ("http://127.0.0.1:1/mcp", "https://evil.example/mcp", fake.mcp_url.replace("http:", "https:")):
            with self.subTest(url=other):
                with self.assertRaises(oauth.OAuthError):
                    source.token(other)
        elsewhere = self.http_fake()   # a server at another origin never sees the token: nothing is sent
        with self.assertRaises(mcp.MCPError) as cm:
            mcp.Client("elsewhere", "http", url=elsewhere.mcp_url, auth=source).connect()
        self.assertIn("Refusing", str(cm.exception))
        self.assertEqual(elsewhere.requests, [])

    def test_not_signed_in_means_auth_required(self):
        auth, fake = self.setup_servers()
        cid = self.make_connector(fake.mcp_url)
        with self.assertRaises(oauth.OAuthError) as cm:
            oauth.TokenSource(cid).token(fake.mcp_url)
        self.assertTrue(cm.exception.needs_sign_in)
        with self.assertRaises(mcp.AuthRequired):
            mcp.Client("fakemcp", "http", url=fake.mcp_url, auth=oauth.TokenSource(cid)).connect()

    def test_refresh_failure_uses_what_is_left_then_asks_for_sign_in(self):
        auth, fake = self.setup_servers()
        cid = self.make_connector(fake.mcp_url)
        stored = self.sign_in(cid)
        stored.update(refresh_token="revoked", expires_at=time.time() + 30)
        self.store[f"oauth.{cid}"] = json.dumps(stored)
        self.assertEqual(oauth.TokenSource(cid).token(), stored["access_token"])   # still valid for 30 s
        stored["expires_at"] = time.time() - 5
        self.store[f"oauth.{cid}"] = json.dumps(stored)
        with self.assertRaises(oauth.OAuthError) as cm:
            oauth.TokenSource(cid).token()
        self.assertTrue(cm.exception.needs_sign_in)

    def test_openid_configuration_fallback(self):
        auth, fake = self.setup_servers(oauth_metadata=False)
        cid = self.make_connector(fake.mcp_url)
        self.sign_in(cid)
        gets = [p for m, p in auth.requests if m == "GET" and p.startswith("/.well-known/")]
        self.assertEqual(gets[:2], ["/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"])

    def test_client_id_from_config_when_registration_is_absent(self):
        auth, fake = self.setup_servers(registration=False)
        auth.client_ids.add("holdco-config-client")
        cid = self.make_connector(fake.mcp_url, oauth_client_id="holdco-config-client")
        url = oauth.start(cid)
        self.assertEqual(self.query(url)["client_id"], "holdco-config-client")
        _, callback = self.follow(url)
        self.assertIn("Connected", oauth.callback_page(callback)[1])
        self.assertEqual(json.loads(self.store[f"oauth.{cid}"])["client_id"], "holdco-config-client")
        self.assertEqual(auth.registrations, [])
        self.assertNotIn(("POST", "/register"), auth.requests)

        bare = self.make_connector(fake.mcp_url)   # no registration and no client ID: say what to do
        with self.assertRaises(oauth.OAuthError) as cm:
            oauth.start(bare)
        self.assertIn("client ID", str(cm.exception))

    def test_wrong_iss_is_refused_and_nothing_is_stored(self):
        auth, fake = self.setup_servers(wrong_iss=True)
        cid = self.make_connector(fake.mcp_url)
        _, callback = self.follow(oauth.start(cid))
        self.assertNotEqual(callback["iss"], auth.url)
        with self.assertRaises(oauth.OAuthError) as cm:
            oauth.finish(callback)
        self.assertIn("saved nothing", str(cm.exception))
        self.assertNotIn(f"oauth.{cid}", self.store)
        self.assertEqual(auth.token_requests, [])   # the code was never exchanged
        self.assertEqual(db.one("SELECT status FROM connectors WHERE id=?", (cid,))["status"], "bad")

        _, callback = self.follow(oauth.start(cid))
        status, page = oauth.callback_page(callback)
        self.assertEqual(status, 200)
        self.assertNotIn("Connected", page)
        self.assertIn("refused", page)
        self.assertEqual(self.store, {})

    def test_a_pending_sign_in_expires_after_10_minutes(self):
        auth, fake = self.setup_servers()
        cid = self.make_connector(fake.mcp_url)
        url = oauth.start(cid)
        state = self.query(url)["state"]
        oauth._pending[state]["created_at"] -= oauth.PENDING_TTL + 1
        _, callback = self.follow(url)
        with self.assertRaises(oauth.OAuthError) as cm:
            oauth.finish(callback)
        self.assertIn("expired", str(cm.exception))
        self.assertNotIn(f"oauth.{cid}", self.store)
        stale = self.query(oauth.start(cid))["state"]
        oauth._pending[stale]["created_at"] -= oauth.PENDING_TTL + 1
        oauth.start(cid)   # starting again sweeps the stale entry
        self.assertNotIn(stale, oauth._pending)

    def test_callback_page_escapes_everything(self):
        status, page = oauth.callback_page({"error": "<script>alert(1)</script>",
                                            "error_description": "<b>bad</b>", "state": "x"})
        self.assertEqual(status, 200)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<b>bad</b>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

        auth, fake = self.setup_servers()
        cid = self.make_connector(fake.mcp_url, key="<i>evil</i>")
        _, callback = self.follow(oauth.start(cid))
        status, page = oauth.callback_page(callback)
        self.assertIn("Connected &lt;i&gt;evil&lt;/i&gt;. You can close this tab.", page)
        self.assertNotIn("<i>evil</i>", page)

    def test_callback_through_the_dashboard_route(self):
        auth, fake = self.setup_servers()
        cid = self.make_connector(fake.mcp_url)
        httpd = server.make_server("127.0.0.1", 0)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        def stop():
            httpd.shutdown()
            httpd.server_close()
            t.join(5)
        self.addCleanup(stop)
        with mock.patch.object(config, "PORT", httpd.server_address[1]):
            url = oauth.start(cid)
            with _direct.open(url, timeout=30) as r:   # follows the 302 to the real /oauth/callback
                status, page = r.status, r.read().decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("Connected fakemcp. You can close this tab.", page)
        self.assertIn(f"oauth.{cid}", self.store)

    def test_https_is_required_except_on_this_computer(self):
        oauth._require_https("https://auth.example.com/token", "x")
        oauth._require_https("http://127.0.0.1:9/token", "x")
        with self.assertRaises(oauth.OAuthError):
            oauth._require_https("http://auth.example.com/token", "The token endpoint")
        self.assertEqual(oauth._origin("HTTPS://Example.com:443/a"), oauth._origin("https://example.com/b"))
        self.assertNotEqual(oauth._origin("http://127.0.0.1:80/"), oauth._origin("http://127.0.0.1:8080/"))


if __name__ == "__main__":
    unittest.main()
