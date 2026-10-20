"""Phase 4: Holdco as an MCP server (docs/phases/phase-4-scale.md section 1): both protocol generations in
mcp_server.handle, the eight tools as the owner (secrets masked, writes audited, the kill switch obeyed), POST /mcp
on a real Holdco (Basic auth, the Origin check, GET 405), the Phase 1 client talking to Holdco's own server in
both generations, and the stdio proxy (python -m holdco.mcp_server). No network: the real servers run on
127.0.0.1 with a model server address nothing listens on.

Run with:  python -m unittest tests.test_phase4_mcp -v
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-phase4-mcp-"))
os.environ.setdefault("HOLDCO_VAULT_BACKEND", "file")   # never touch a developer's keyring

import holdco  # noqa: E402
from holdco import (actions, brain, db, doctor, mcp, mcp_server, notify, roles, server, trust, vault,  # noqa: E402
                    workspace)
from tests.helpers import Client as ApiClient, HoldcoProcess, free_port  # noqa: E402
from tests.mcp_client import McpClient, basic_auth, http_client, post, stdio_client  # noqa: E402

db.init()

META = {mcp.META_VERSION: mcp.CURRENT, mcp.META_CAPABILITIES: {}}
TOOL_NAMES = {"list_companies", "get_company_brain", "list_inbox", "decide", "create_task", "get_metrics",
              "latest_brief", "doctor"}
KILL_TEXT = "The kill switch is on. Turn it off to run approvals."
_created: list[int] = []   # archived after each test, so other modules' scheduler checks never see them


def utc(days: float = 0) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_company(name="Mcp Paws", members=("ceo", "marketing"), status="active") -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, mission, status, stage, settings, created_at) "
                 "VALUES(?,?,?,?,?,?,?,?)", (name, slug, "Meal plans for dogs", "Feed every dog well", status,
                                             "building", json.dumps({"truth_scan_done": True}), db.now()))
    _created.append(cid)
    workspace.create(db.get_company(cid))
    for m in members:
        roles.add_agent(cid, preset=m)
    return db.get_company(cid)


def agent_of(company, role="ceo") -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def queue(company, action: str, payload: dict, role="marketing") -> int:
    """A pending approvals row, as actions.gate leaves it."""
    return db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, created_at)"
                  " VALUES(?,?,?,?,?,?,?,?)", (company["id"], agent_of(company, role)["id"], None, action,
                                               actions._summary(action, payload), json.dumps(payload), "pending",
                                               db.now()))


def new_msg(method: str, params: dict | None = None, rid=1, version: str = mcp.CURRENT, name: str = "unit-test"):
    meta = {mcp.META_VERSION: version, mcp.META_CAPABILITIES: {}, mcp.META_CLIENT: {"name": name, "version": "1"}}
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": {**(params or {}), "_meta": meta}}


def old_msg(method: str, params: dict | None = None, rid=1) -> dict:
    msg = {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}
    if rid is not None:
        msg["id"] = rid
    return msg


def call(tool: str, client: dict | None = None, **args) -> dict:
    """tools/call through handle() in the older generation; returns the tool result."""
    reply = mcp_server.handle(old_msg("tools/call", {"name": tool, "arguments": args}), client if client is not None
                              else {"name": "unit-test"})
    return reply["result"]


def quiet():
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    return stack


# ---------------------------------------------------------------- real Holdco servers

class AuthApi(ApiClient):
    """tests.helpers.Client with the dashboard password on every request."""

    def __init__(self, base: str, password: str):
        super().__init__(base)
        self.password = password

    def raw(self, method, path, body=None, headers=None):
        return super().raw(method, path, body, {"Authorization": basic_auth(self.password), **(headers or {})})


def start_app(password: str = "") -> HoldcoProcess:
    """A real Holdco on a free port with a seeded database: one paused company ("Proxy Paws") with its CEO and two
    purchase requests waiting, autopilot off and a model server address where nothing listens (so neither the
    Doctor nor anything else reaches a developer's Ollama or the internet)."""
    data = tempfile.mkdtemp(prefix="holdco-phase4-mcp-app-")
    con = sqlite3.connect(os.path.join(data, "holdco.db"))
    try:
        con.executescript(db.SCHEMA)
        for k, v in {"llm_base_url": f"http://127.0.0.1:{free_port()}", "scheduler_enabled": False}.items():
            con.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (k, json.dumps(v)))
        ts = db.now()
        con.execute("INSERT INTO companies(id, name, slug, idea, mission, status, stage, settings, created_at) "
                    "VALUES(1, 'Proxy Paws', 'proxy-paws', 'Meal plans for dogs', 'Feed every dog well', 'paused', "
                    "'building', '{}', ?)", (ts,))
        con.execute("INSERT INTO agents(id, company_id, role, name, title, enabled, status, created_at) "
                    "VALUES(1, 1, 'ceo', 'Ada', 'CEO', 1, 'idle', ?)", (ts,))
        for aid, item in ((1, "domain pawplan.example"), (2, "logo pack"), (3, "stock photos")):
            p = {"item": item, "amount_usd": 12, "vendor": "Example"}
            con.execute("INSERT INTO approvals(id, company_id, agent_id, action, summary, payload, status, created_at)"
                        " VALUES(?, 1, 1, 'spend_money', ?, ?, 'pending', ?)",
                        (aid, actions._summary("spend_money", p), json.dumps(p), ts))
        con.commit()
    finally:
        con.close()
    app = HoldcoProcess(data_dir=data, env={"HOLDCO_PASSWORD": password, "HOLDCO_VAULT_BACKEND": "file",
                                            "HOLDCO_SEARCH_PROVIDERS": "searxng"})
    if password:
        app.client = AuthApi(app.client.base, password)
    return app.start()


def stop_app(app: HoldcoProcess) -> None:
    app.stop()
    shutil.rmtree(app.data_dir, ignore_errors=True)


def tracebacks(app: HoldcoProcess) -> list[str]:
    return [line for line in app.log().splitlines() if "Traceback" in line]


# ---------------------------------------------------------------- tests

class TestMcpServer(unittest.TestCase):
    _apps: dict = {}

    @classmethod
    def app(cls, password: str = "") -> HoldcoProcess:
        """One real Holdco per password, shared by the HTTP tests and stopped at the end of the class."""
        if password not in cls._apps:
            cls._apps[password] = start_app(password)
        return cls._apps[password]

    @classmethod
    def tearDownClass(cls):
        for app in cls._apps.values():
            stop_app(app)
        cls._apps = {}

    def setUp(self):
        self._kill = bool(db.get_settings().get("kill_switch"))
        db.set_settings({"kill_switch": False})
        for p in (mock.patch.object(notify, "send_later", lambda *a, **kw: None),
                  mock.patch.object(notify, "send", lambda *a, **kw: True)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        db.set_settings({"kill_switch": self._kill})
        while _created:
            db.update("companies", _created.pop(), status="archived")

    def connected(self, client: McpClient) -> McpClient:
        self.addCleanup(client.close)
        return client.connect()

    # -------------------------------------------------- the protocol

    def test_both_generations(self):
        handle = mcp_server.handle
        # the current generation: server/discover, the version check, resultType and serverInfo on every result
        conn: dict = {}
        found = handle(new_msg("server/discover"), conn)["result"]
        self.assertEqual(found["protocolVersions"], ["2026-07-28"])
        self.assertEqual(found["capabilities"], {"tools": {}})
        server_info = {"name": "holdco", "version": holdco.__version__}
        self.assertEqual(found["serverInfo"], server_info)
        self.assertEqual(conn["name"], "unit-test")   # clientInfo from _meta, for the audit log
        bad = handle(new_msg("tools/list", version="2025-01-01", rid=2), {})
        self.assertEqual(bad["id"], 2)
        self.assertEqual(bad["error"]["code"], -32022)
        self.assertEqual(bad["error"]["data"]["supported"], ["2026-07-28"])
        for msg in (new_msg("server/discover"), new_msg("tools/list"),
                    new_msg("tools/call", {"name": "doctor", "arguments": {}})):
            result = handle(msg, {})["result"]
            self.assertEqual(result["resultType"], "complete", msg["method"])
            self.assertEqual(result["_meta"][mcp.META_SERVER], server_info, msg["method"])
        self.assertEqual(handle(new_msg("ping"), {})["error"]["code"], -32601)        # removed in 2026-07-28
        self.assertEqual(handle(new_msg("initialize"), {})["error"]["code"], -32601)  # no handshake there
        # the older generation: initialize, notifications/initialized, ping only after initialize
        conn = {}
        self.assertEqual(handle(old_msg("ping", rid=3), conn)["error"]["code"], -32600)
        init = handle(old_msg("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "desktop", "version": "1"}}), conn)["result"]
        self.assertEqual(init, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                "serverInfo": server_info})
        self.assertEqual(conn["name"], "desktop")
        self.assertIsNone(handle(old_msg("notifications/initialized", rid=None), conn))
        self.assertEqual(handle(old_msg("ping", rid=4), conn), {"jsonrpc": "2.0", "id": 4, "result": {}})
        for asked, agreed in (("2025-11-25", "2025-11-25"), ("2025-03-26", "2025-03-26"), ("2024-11-05", "2025-11-25"),
                              (None, "2025-11-25")):
            got = handle(old_msg("initialize", {"protocolVersion": asked}), {})["result"]["protocolVersion"]
            self.assertEqual(got, agreed, asked)
        # tools/list: eight tools with schemas, a 60 s ttl per client; old-generation results stay plain
        listed = handle(old_msg("tools/list", rid=5), conn)["result"]
        self.assertEqual({t["name"] for t in listed["tools"]}, TOOL_NAMES)
        self.assertEqual(len(listed["tools"]), 8)
        self.assertEqual((listed["ttlMs"], listed["cacheScope"]), (60000, "client"))
        self.assertNotIn("resultType", listed)
        for t in listed["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])
            self.assertTrue(t["description"], t["name"])
        schemas = {t["name"]: t["inputSchema"] for t in listed["tools"]}
        self.assertEqual(schemas["decide"]["required"], ["approval_id", "decision"])
        self.assertEqual(schemas["decide"]["properties"]["decision"]["enum"], ["approve", "reject", "answer"])
        self.assertEqual(set(schemas["create_task"]["properties"]),
                         {"company_id", "title", "description", "assign_to", "deliverable", "acceptance", "goal_id",
                          "priority", "due_days", "checks"})
        self.assertEqual(set(schemas["get_metrics"]["properties"]), {"company_id", "name", "days"})
        self.assertEqual(set(schemas["latest_brief"]["properties"]), {"company_id", "kind"})
        # errors: unknown method, no method, an unknown tool; a tool error is a result, never a JSON-RPC error
        self.assertEqual(handle(old_msg("resources/list", rid=6), conn)["error"]["code"], -32601)
        self.assertEqual(handle({"jsonrpc": "2.0", "id": 7}, conn)["error"]["code"], -32600)
        self.assertEqual(handle(["not", "an", "object"], conn)["error"]["code"], -32600)
        self.assertEqual(handle(old_msg("tools/call", {"name": "nope"}, rid=8), conn)["error"]["code"], -32602)
        failed = handle(old_msg("tools/call", {"name": "get_metrics", "arguments": {}}, rid=9), conn)
        self.assertTrue(failed["result"]["isError"])
        self.assertIn("company_id is required", failed["result"]["content"][0]["text"])
        self.assertIsNone(handle({"jsonrpc": "2.0", "id": 10, "result": {}}, conn))   # a reply, not a request
        # the HTTP layer: bad JSON is -32700, the optional Mcp-Method / Mcp-Name headers must match the body
        json_headers = {"Content-Type": "application/json"}
        status, reply, _ = mcp_server.http_reply("POST", json_headers, b"{not json")
        self.assertEqual((status, reply["error"]["code"], reply["id"]), (400, -32700, None))
        body = json.dumps(new_msg("tools/call", {"name": "doctor", "arguments": {}}, rid=11)).encode()
        status, reply, _ = mcp_server.http_reply("POST", {**json_headers, "Mcp-Method": "tools/list"}, body)
        self.assertEqual((status, reply["error"]["code"], reply["id"]), (400, -32600, 11))
        status, reply, _ = mcp_server.http_reply("POST", {**json_headers, "Mcp-Method": "tools/call",
                                                          "Mcp-Name": "list_inbox"}, body)
        self.assertEqual((status, reply["error"]["code"]), (400, -32600))
        status, reply, _ = mcp_server.http_reply("POST", {**json_headers, "Mcp-Method": "tools/call",
                                                          "Mcp-Name": "doctor"}, body)
        self.assertEqual(status, 200)
        self.assertFalse(reply["result"]["isError"])
        # a batch: one reply per request, none for a notification; only notifications: 202 and no body
        batch = [new_msg("server/discover", rid=12), old_msg("notifications/initialized", rid=None),
                 old_msg("tools/list", rid=13)]
        status, reply, _ = mcp_server.http_reply("POST", json_headers, json.dumps(batch).encode())
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in reply], [12, 13])
        status, reply, _ = mcp_server.http_reply("POST", json_headers,
                                                 json.dumps(old_msg("notifications/initialized", rid=None)).encode())
        self.assertEqual((status, reply), (202, b""))
        # over HTTP an older client is "initialized" when it names its version in MCP-Protocol-Version
        ping = json.dumps(old_msg("ping", rid=14)).encode()
        self.assertEqual(mcp_server.http_reply("POST", json_headers, ping)[1]["error"]["code"], -32600)
        self.assertEqual(mcp_server.http_reply("POST", {**json_headers, "MCP-Protocol-Version": "2025-11-25"},
                                               ping)[1], {"jsonrpc": "2.0", "id": 14, "result": {}})
        self.assertEqual(mcp_server.http_reply("GET", {}, b"")[0], 405)
        self.assertEqual(mcp_server.http_reply("DELETE", {}, b"")[:2], (200, {}))
        self.assertEqual(mcp_server.http_reply("POST", {"Content-Type": "text/plain"}, body)[0], 415)

    # -------------------------------------------------- the tools

    def test_tools_read(self):
        c = make_company("Brainy Paws")
        cid = c["id"]
        secret = "sk_live_" + secrets.token_hex(12)
        db.update_company_settings(cid, smtp_password=secret)    # the vault keeps it; the settings keep a marker
        self.addCleanup(vault.delete, f"company.{cid}.smtp_password")
        db.run("INSERT INTO strategy(company_id, one_liner, price, updated_at) VALUES(?,?,?,?)",
               (cid, "Weekly meal plans for dog owners", "$9/month", db.now()))
        gid = db.run("INSERT INTO goals(company_id, title, metric, target, current, deadline, owner_role, status, "
                     "created_at) VALUES(?,?,?,?,?,?,?,?,?)", (cid, "10 paying customers", "stripe.customers", 10, 0,
                                                               utc(40)[:10], "sales", "active", db.now()))
        db.run("INSERT INTO lessons(company_id, text, created_at) VALUES(?,?,?)",
               (cid, "Short subject lines get more replies", db.now()))
        db.run("INSERT INTO contacts(company_id, name, email, stage, source, created_at) VALUES(?,?,?,?,?,?)",
               (cid, "Dana", "dana@example.com", "lead", "forms", db.now()))
        for name, value, source, when in (("stripe.customers", 1, "stripe", utc(-45)),   # outside the 30 days
                                          ("site.visitors", 40, "manual", utc(-2)),
                                          ("stripe.customers", 2, "stripe", utc(-1))):
            db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, source, created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (cid, None, name, value, "", source, when))
        db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
               (cid, "daily", "Old brief", "{}", utc(-1)))
        bid = db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
                     (cid, "daily", "Revenue: 2 customers.", json.dumps({"customers": 2}), db.now()))
        pid = db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
                     (None, "daily", "Portfolio: all quiet.", "{}", db.now()))
        self.addCleanup(db.run, "DELETE FROM briefs WHERE id=?", (pid,))
        post_id = queue(c, "social_post", {"platform": "x", "content": f"Our SMTP key is {secret}",
                                           "_why_asked": trust.TAINTED + " (https://example.com/forum)",
                                           "_taint": ["https://example.com/forum"]})
        dec_id = queue(c, "decision", {"question": "Which price should we launch with?",
                                       "options": ["$9/month", "$15/month"], "recommended": "$9/month",
                                       "deadline_at": utc(2), "reversible": True}, role="ceo")
        bench = make_company("Bench Paws", status="bench")
        bench_id = queue(bench, "social_post", {"platform": "x", "content": "bench run"})

        # list_companies: the /api/state numbers, active goals, pending count; no bench company, no secret
        out = call("list_companies")
        self.assertFalse(out["isError"])
        self.assertNotIn(secret, json.dumps(out))
        rows = {r["id"]: r for r in out["structuredContent"]["companies"]}
        self.assertNotIn(bench["id"], rows)
        mine = rows[cid]
        self.assertEqual((mine["name"], mine["status"], mine["stage"], mine["mission"]),
                         ("Brainy Paws", "active", "building", "Feed every dog well"))
        self.assertEqual(mine["stats"], server._stats(cid))
        self.assertEqual(mine["pending_approvals"], 2)
        self.assertEqual([(g["id"], g["current"], g["target"]) for g in mine["goals"]], [(gid, 2.0, 10.0)])
        self.assertEqual(json.loads(out["content"][0]["text"]), out["structuredContent"])
        # get_company_brain: the digest as text, every section as structured content
        out = call("get_company_brain", company_id=cid)
        self.assertFalse(out["isError"])
        self.assertEqual(out["content"][0]["text"], brain.digest(db.get_company(cid)))
        self.assertIn("Weekly meal plans for dog owners", out["content"][0]["text"])
        data = out["structuredContent"]
        for section in brain.SECTIONS:
            self.assertIn(section, data)
        self.assertEqual(data["strategy"]["price"], "$9/month")
        self.assertEqual(data["contacts"][0]["email"], "dana@example.com")
        self.assertTrue(call("get_company_brain", company_id=bench["id"])["isError"])
        self.assertTrue(call("get_company_brain", company_id=10**9)["isError"])
        # get_metrics: newest first, with the source, 30 days by default
        out = call("get_metrics", company_id=cid)["structuredContent"]
        self.assertEqual([(m["name"], m["value"], m["source"]) for m in out["metrics"]],
                         [("stripe.customers", 2.0, "stripe"), ("site.visitors", 40.0, "manual")])
        out = call("get_metrics", company_id=cid, name="stripe.customers", days=60)["structuredContent"]
        self.assertEqual([m["value"] for m in out["metrics"]], [2.0, 1.0])
        self.assertTrue(call("get_metrics", company_id=cid, days="lots")["isError"])
        # latest_brief: the company's newest, the portfolio's when no company is named
        out = call("latest_brief", company_id=cid)
        self.assertEqual(out["structuredContent"]["brief"]["id"], bid)
        self.assertEqual(out["structuredContent"]["brief"]["data"], {"customers": 2})
        self.assertIn("Revenue: 2 customers.", out["content"][0]["text"])
        self.assertIsNone(call("latest_brief", company_id=cid, kind="weekly")["structuredContent"]["brief"])
        out = call("latest_brief", kind="daily")
        self.assertEqual(out["structuredContent"]["brief"]["id"], pid)
        self.assertTrue(call("latest_brief", kind="hourly")["isError"])
        # doctor: the cached rows and their summary
        rows = [{"key": "model_server", "label": "Model server", "status": "ok", "detail": "Ollama answers",
                 "checked_at": db.now(), "run": 3},
                {"key": "disk", "label": "Disk space", "status": "warn", "detail": "4 GB free",
                 "checked_at": db.now(), "run": 3}]
        with mock.patch.object(doctor, "LAST", rows):
            out = call("doctor")
        self.assertEqual(out["structuredContent"]["summary"]["ok"], 1)
        self.assertEqual(out["structuredContent"]["summary"]["warn"], 1)
        self.assertEqual([r["key"] for r in out["structuredContent"]["checks"]], ["model_server", "disk"])
        self.assertIn("[warn] Disk space: 4 GB free", out["content"][0]["text"])
        # list_inbox: requests, questions and decisions with what the owner needs; secrets masked; no bench rows
        out = call("list_inbox")
        self.assertNotIn(secret, json.dumps(out))
        items = {i["id"]: i for i in out["structuredContent"]["inbox"]}
        self.assertNotIn(bench_id, items)
        req = items[post_id]
        self.assertEqual((req["kind"], req["action"], req["label"], req["tag"]),
                         ("request", "social_post", "Social post", "publish"))
        self.assertIn("[redacted]", req["payload"]["content"])
        self.assertTrue(req["why_asked"].startswith(trust.TAINTED))
        self.assertEqual(req["taint"], ["https://example.com/forum"])
        dec = items[dec_id]
        self.assertEqual((dec["kind"], dec["options"], dec["recommended"]),
                         ("decision", ["$9/month", "$15/month"], "$9/month"))
        self.assertTrue(dec["deadline"])
        self.assertTrue(dec["reversible"])
        self.assertEqual(dec["question"], "Which price should we launch with?")
        only = call("list_inbox", company_id=cid)["structuredContent"]["inbox"]
        self.assertEqual(sorted(i["id"] for i in only), sorted([post_id, dec_id]))
        self.assertTrue(call("list_inbox", company_id=bench["id"])["isError"])

    def test_tools_write_audited(self):
        c = make_company("Writer Paws")
        cid = c["id"]
        gid = db.run("INSERT INTO goals(company_id, title, metric, target, current, deadline, owner_role, status, "
                     "created_at) VALUES(?,?,?,?,?,?,?,?,?)", (cid, "First 100 visitors", "site.visitors", 100, 0,
                                                               utc(30)[:10], "marketing", "active", db.now()))
        # create_task: the task form's checks, created_by "owner (mcp)"
        out = call("create_task", company_id=cid, title="Write the launch post", assign_to="marketing",
                   description="One post for X", deliverable="docs/launch-post.md", acceptance="Under 280 characters",
                   goal_id=gid, priority=2, due_days=3, checks="browser_check site/index.html")
        self.assertFalse(out["isError"], out)
        tid = out["structuredContent"]["task"]["id"]
        t = db.get_task(tid)
        self.assertEqual((t["created_by"], t["role"], t["agent_id"], t["status"], t["priority"], t["goal_id"]),
                         ("owner (mcp)", "marketing", agent_of(c, "marketing")["id"], "todo", 2, gid))
        self.assertEqual(t["checks"], "browser_check site/index.html")
        self.assertTrue(t["due_at"])
        self.assertIn(f"#{tid}", out["content"][0]["text"])
        ceo_task = call("create_task", company_id=cid, title="Plan next week")["structuredContent"]["task"]
        self.assertEqual((ceo_task["role"], ceo_task["priority"]), ("ceo", 1))   # the API's defaults
        for args, words in (({"title": ""}, "title"),
                            ({"title": "Tidy up", "assign_to": "legal"}, "Team roles: ceo, marketing"),
                            ({"title": "Tidy up", "goal_id": 10**9}, "not an active goal"),
                            ({"title": "Tidy up", "due_days": 500}, "due_days must be"),
                            ({"title": "Tidy up", "checks": "dance wildly"}, "must start with one of")):
            out = call("create_task", company_id=cid, **args)
            self.assertTrue(out["isError"], args)
            self.assertIn(words, out["content"][0]["text"])
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND title='Tidy up'", (cid,)), 0)

        # decide: a queued post approved by an MCP client runs and is audited with the client's name
        res = actions.gate(c, agent_of(c, "marketing"), None, "social_post", {"platform": "x", "content": "We launch"},
                           tainted=[])
        self.assertIn("Queued", res)
        post_id = db.scalar("SELECT MAX(id) FROM approvals WHERE company_id=? AND action='social_post'", (cid,))
        msg = new_msg("tools/call", {"name": "decide", "arguments": {"approval_id": post_id, "decision": "approve"}},
                      name="claude-test")
        with quiet():
            out = mcp_server.handle(msg, {})["result"]
        self.assertFalse(out["isError"], out)
        self.assertEqual(out["structuredContent"]["approval"]["status"], "done")
        row = db.one("SELECT * FROM approvals WHERE id=?", (post_id,))
        self.assertEqual((row["status"], row["decided_by"]), ("done", "mcp"))
        audit = db.one("SELECT * FROM audit WHERE approval_id=?", (post_id,))
        self.assertEqual(audit["approved_by"], "mcp:claude-test")
        self.assertEqual(audit["proposed_by"], agent_of(c, "marketing")["name"])
        self.assertEqual(audit["action"], "social_post")
        out = call("decide", approval_id=post_id, decision="approve")
        self.assertTrue(out["isError"])
        self.assertIn("already done", out["content"][0]["text"])
        # a question is answered (and needs an answer); a request cannot be "answered"
        qid = queue(c, "question", {"question": "Launch on Monday or Tuesday?", "options": ["Monday", "Tuesday"],
                                    "recommended": "Monday"}, role="ceo")
        out = call("decide", approval_id=qid, decision="answer")
        self.assertTrue(out["isError"])
        self.assertIn("Options: Monday; Tuesday", out["content"][0]["text"])
        with quiet():
            out = call("decide", {"name": "desktop"}, approval_id=qid, decision="answer", answer="Tuesday")
        self.assertFalse(out["isError"], out)
        row = db.one("SELECT * FROM approvals WHERE id=?", (qid,))
        self.assertEqual((row["status"], row["result"], row["decided_by"]), ("answered", "Tuesday", "mcp"))
        spend = queue(c, "spend_money", {"item": "domain", "amount_usd": 12, "vendor": "Example"})
        out = call("decide", approval_id=spend, decision="answer", answer="yes")
        self.assertTrue(out["isError"])
        self.assertIn("approve or reject", out["content"][0]["text"])
        self.assertTrue(call("decide", approval_id=spend, decision="maybe")["isError"])
        self.assertTrue(call("decide", approval_id=10**9, decision="approve")["isError"])
        # the kill switch: the Phase 0 refusal comes back as an isError result and nothing runs
        db.set_settings({"kill_switch": True})
        out = call("decide", approval_id=spend, decision="approve")
        self.assertTrue(out["isError"])
        self.assertEqual(out["content"][0]["text"], KILL_TEXT)
        self.assertEqual(db.one("SELECT status FROM approvals WHERE id=?", (spend,))["status"], "pending")
        self.assertIsNone(db.one("SELECT id FROM audit WHERE approval_id=?", (spend,)))
        db.set_settings({"kill_switch": False})
        with quiet():
            out = call("decide", approval_id=spend, decision="reject", feedback="Not yet")
        self.assertFalse(out["isError"], out)
        self.assertEqual(db.one("SELECT status FROM approvals WHERE id=?", (spend,))["status"], "rejected")

    # -------------------------------------------------- over HTTP and stdio

    def test_http_auth_and_origin(self):
        discover = new_msg("server/discover")
        # without a password: /mcp works from localhost, with no X-Holdco header
        app = self.app("")
        status, _, reply = post(app.port, discover)
        self.assertEqual(status, 200)
        self.assertEqual(reply["result"]["serverInfo"]["name"], "holdco")
        status, _, reply = post(app.port, new_msg("tools/call", {"name": "list_companies", "arguments": {}}),
                                {"Mcp-Method": "tools/call", "Mcp-Name": "list_companies"})
        self.assertEqual(status, 200)
        self.assertEqual([c["name"] for c in reply["result"]["structuredContent"]["companies"]], ["Proxy Paws"])
        # the Origin check: a browser page elsewhere (or a sandboxed preview, "null") is refused
        for origin in ("http://evil.example", "null", f"http://localhost:{free_port()}",
                       f"http://evil.example:{app.port}"):
            status, _, reply = post(app.port, discover, {"Origin": origin})
            self.assertEqual(status, 403, origin)
        for origin in (f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}"):
            self.assertEqual(post(app.port, discover, {"Origin": origin})[0], 200, origin)
        status, headers, _ = post(app.port, None, method="GET")
        self.assertEqual(status, 405)
        self.assertIn("POST", headers.get("Allow", ""))
        self.assertEqual(post(app.port, None, method="DELETE")[0], 200)
        self.assertEqual(post(app.port, None, raw=b"[1, 2", headers={})[0], 400)
        self.assertEqual(post(app.port, discover, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(post(app.port, {"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        # with HOLDCO_PASSWORD: Basic auth exactly as the dashboard
        pw = "pw-" + secrets.token_hex(6)
        app = self.app(pw)
        status, headers, _ = post(app.port, discover)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))
        self.assertEqual(post(app.port, discover, {"Authorization": basic_auth("wrong")})[0], 401)
        status, _, reply = post(app.port, discover, {"Authorization": basic_auth(pw)})
        self.assertEqual(status, 200)
        self.assertEqual(reply["result"]["protocolVersions"], ["2026-07-28"])
        self.assertEqual(post(app.port, discover, {"Authorization": basic_auth(pw),
                                                   "Origin": "http://evil.example"})[0], 403)
        with self.assertRaises(mcp.AuthRequired):
            http_client(app.port).connect()
        self.assertEqual(post(app.port, None, {"Authorization": basic_auth(pw)}, method="GET")[0], 405)
        for a in self._apps.values():
            self.assertFalse(tracebacks(a), a.log()[-3000:])

    def test_phase1_client_both_generations(self):
        """holdco.mcp, the client the connectors use, talks to Holdco's own server in both generations."""
        pw = "pw-" + secrets.token_hex(6)
        app = self.app(pw)
        new = self.connected(http_client(app.port, password=pw, client_name="desktop-new"))
        self.assertEqual(new.protocol, "2026-07-28")
        self.assertEqual(new.server_info, {"name": "holdco", "version": holdco.__version__})
        self.assertEqual({t["name"] for t in new.list_tools()}, TOOL_NAMES)
        self.assertEqual(new.tools_info, {"ttlMs": 60000, "cacheScope": "client"})
        inbox = new.call_tool("list_inbox", {"company_id": 1})
        self.assertFalse(inbox["isError"])
        self.assertIn(1, [i["id"] for i in inbox["structuredContent"]["inbox"]])
        done = new.call_tool("decide", {"approval_id": 1, "decision": "approve"})
        self.assertFalse(done["isError"], done)
        self.assertIn("done", mcp.text_of(done))
        old = self.connected(http_client(app.port, password=pw, generation="old", client_name="desktop-old"))
        self.assertEqual(old.protocol, "2025-11-25")
        self.assertEqual(old.request("ping"), {})
        self.assertEqual(len(old.list_tools()), 8)
        listed = old.call_tool("list_companies")
        self.assertEqual(listed["structuredContent"]["companies"][0]["name"], "Proxy Paws")
        missing = old.call_tool("get_company_brain", {"company_id": 99})
        self.assertTrue(missing["isError"])
        self.assertTrue(mcp.text_of(missing).startswith("ERROR: Company #99 not found"))
        audit = {a["approval_id"]: a for a in app.client.get("/api/audit")["audit"]}
        self.assertEqual(audit[1]["approved_by"], "mcp:desktop-new")
        decided = {a["id"]: a for a in app.client.get("/api/approvals?status=done")["approvals"]}
        self.assertEqual(decided[1]["decided_by"], "mcp")
        self.assertFalse(tracebacks(app), app.log()[-3000:])

    def test_stdio_proxy(self):
        pw = "pw-" + secrets.token_hex(6)
        app = start_app(pw)
        stopped = False
        try:
            # the current generation: server/discover answered by the proxy, tools forwarded to the dashboard
            new = self.connected(stdio_client(app.port, pw, client_name="cli-new"))
            self.assertEqual(new.protocol, "2026-07-28")
            self.assertEqual(new.server_info["name"], "holdco")
            listed = new.call_tool("list_companies")
            self.assertFalse(listed["isError"], listed)
            self.assertEqual(listed["structuredContent"]["companies"][0]["name"], "Proxy Paws")
            # the older generation: initialize and ping answered locally; a decision is audited with the client's
            # name although the older generation sends clientInfo only once
            old = self.connected(stdio_client(app.port, pw, generation="old", client_name="desktop-stdio"))
            self.assertEqual(old.protocol, "2025-11-25")
            self.assertEqual(old.request("ping"), {})
            self.assertEqual({t["name"] for t in old.list_tools()}, TOOL_NAMES)
            done = old.call_tool("decide", {"approval_id": 2, "decision": "approve"})
            self.assertFalse(done["isError"], done)
            audit = {a["approval_id"]: a for a in app.client.get("/api/audit")["audit"]}
            self.assertEqual(audit[2]["approved_by"], "mcp:desktop-stdio")
            # a wrong password says so
            wrong = self.connected(stdio_client(app.port, "wrong-password"))
            out = wrong.call_tool("doctor")
            self.assertTrue(out["isError"])
            self.assertEqual(out["content"][0]["text"], mcp_server.WRONG_PASSWORD)
            self.assertFalse(tracebacks(app), app.log()[-3000:])
            # the dashboard stops: tool calls say Holdco is not running; the tool list still shows
            stop_app(app)
            stopped = True
            for client in (new, old):
                out = client.call_tool("list_inbox")
                self.assertTrue(out["isError"])
                self.assertEqual(out["content"][0]["text"], mcp_server.NOT_RUNNING)
            self.assertEqual(len(old.list_tools()), 8)
            late = self.connected(stdio_client(app.port, pw))   # server/discover needs no dashboard
            self.assertEqual(late.protocol, "2026-07-28")
            self.assertEqual(mcp.text_of(late.call_tool("doctor")), "ERROR: " + mcp_server.NOT_RUNNING)
            # the raw pipe: Windows line endings, a line that is not JSON, a batch
            lines = ["{not json", json.dumps(old_msg("initialize", {"protocolVersion": "2025-11-25"}, rid=1)),
                     json.dumps([old_msg("tools/call", {"name": "doctor"}, rid=2), new_msg("server/discover", rid=3)])]
            env = mcp.child_env({"PYTHONPATH": str(ROOT), "HOLDCO_PORT": str(app.port), "HOLDCO_PASSWORD": pw})
            run = subprocess.run([sys.executable, "-m", "holdco.mcp_server"], input="\r\n".join(lines) + "\r\n",
                                 capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
            replies = [json.loads(line) for line in run.stdout.replace("\r\n", "\n").splitlines() if line.strip()]
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(replies[0]["error"]["code"], -32700)
            self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-11-25")
            self.assertEqual([r["id"] for r in replies[2]], [2, 3])
            self.assertEqual(replies[2][0]["result"]["content"][0]["text"], mcp_server.NOT_RUNNING)
            self.assertNotIn("resultType", replies[2][0]["result"])          # the older generation stays plain
            self.assertEqual(replies[2][1]["result"]["resultType"], "complete")
        finally:
            if not stopped:
                stop_app(app)


if __name__ == "__main__":
    unittest.main()
