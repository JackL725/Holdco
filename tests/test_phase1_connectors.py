"""Phase 1 connector framework (docs/phases/phase-1-connect.md sections 4 to 6): risk tags and policies, connector
instances and setup cards, the dashboard API, health checks, Doctor rows, the daily phone alert, the prompt
section, metric pipes with a source, the facts block, find_tool and the routing of loaded tools through the risk
gate (including a scripted agent run).

Nothing here touches the network or a real keyring: the vault is an in-memory dict, MCP clients are stubs, and the
connectors are test-only definitions inserted into connectors.DEFINITIONS for the length of each test.

Run with:  python -m unittest tests.test_phase1_connectors -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-connectors-")

from holdco import (actions, agents, config, connectors, db, doctor, llm, mcp, notify, oauth, roles,  # noqa: E402
                    routing, server, tools, truth, vault, workspace)

db.init()
assert server   # imported for its routes (/api/companies/..., /api/approvals)
REAL_CLIENT = connectors.client   # most tests replace it with a stub; one test checks the real caching

MASK = "••••••••"
TEST_KEY = "wk_test_0123456789abcdef"
BAD_KEY = "wk_bad_0123456789abcdef"


def make_company(name="ConnCo", members=("ceo", "engineering", "marketing")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "An idea", "active", "building", "{}", db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def agent_of(company, role) -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def ctx_for(company, role, task=None, kind="task") -> tools.Ctx:
    agent = agent_of(company, role)
    c = tools.Ctx(company=company, agent=agent, kind=kind, run_id=0, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(agent, kind)}
    return c


def set_policies(company, **policies) -> dict:
    cs = json.loads(db.get_company(company["id"])["settings"] or "{}")
    cs["policies"] = {**cs.get("policies", {}), **policies}
    db.update("companies", company["id"], settings=json.dumps(cs))
    return db.get_company(company["id"])


class Handler:
    def __init__(self, query=None):
        self.query = {k: [str(v)] for k, v in (query or {}).items()}


def call(method: str, path: str, body=None, query: dict | None = None):
    """Dispatch like server.Handler does, so the route patterns are tested too."""
    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(Handler(query), m, body)
    raise AssertionError(f"no route for {method} {path}")


def quiet():
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    return stack


# ---------------------------------------------------------------- test-only connectors

CALLS: list = []   # (tool, args, company id) for every api tool that really ran


def _list_widgets(inst, company, limit=10):
    CALLS.append(("list_widgets", {"limit": limit}, company["id"] if company else None))
    return f"2 widgets for {company['name']} (limit {limit})"


def _publish_widget(inst, company, title, price_usd=0.0):
    CALLS.append(("publish_widget", {"title": title, "price_usd": price_usd}, company["id"] if company else None))
    return f"Published {title} at https://shop.example/w/1 for ${price_usd:g}"


def _leaky(inst, company):
    return "the key is " + connectors.secrets(inst)["api_key"]


def _refund_order(inst, company, order_id):
    CALLS.append(("refund_order", {"order_id": order_id}, company["id"] if company else None))
    return f"Refunded {order_id}"


def _api_tools(inst):
    return [
        {"name": "list_widgets", "description": "List the widgets in the shop",
         "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "How many"}},
                        "required": []}, "tag": "read", "fn": _list_widgets},
        {"name": "publish_widget", "description": "Publish a widget page for sale with a price",
         "parameters": {"type": "object", "properties": {"title": {"type": "string"},
                                                         "price_usd": {"type": "number"}}, "required": ["title"]},
         "tag": "publish", "fn": _publish_widget},
        {"name": "refund_order", "description": "Refund an order",
         "parameters": {"order_id": {"type": "string", "description": "The order"}},   # properties only
         "tag": "irreversible", "fn": _refund_order},
        {"name": "leaky_read", "description": "Shows a diagnostic line", "parameters": {}, "tag": "read",
         "fn": _leaky},
    ]


def _api_health(inst):
    key = connectors.secrets(inst).get("api_key", "")
    if not key:
        return "bad", "No API key is set"
    if "_bad_" in key:
        return "bad", "key rejected"
    return "ok", "test mode, all good"


def _api_metrics(inst, company):
    return [("revenue_usd_30d", 19, "1 payment"), ("payments_30d", 1, ""), ("email.sent_7d", 3, "namespaced"),
            ("broken", "not a number", "")]


TESTAPI = {
    "label": "Widget Shop", "category": "Payments", "kind": "api", "scope": "company",
    "setup": {"where_to_get_key": "Shop settings, API keys.", "permissions": ["read widgets", "write widgets"],
              "fields": [{"key": "api_key", "label": "API key", "secret": True, "hint": "a test key first",
                          "placeholder": "wk_test_..."},
                         {"key": "shop_name", "label": "Shop name", "secret": False, "hint": "", "placeholder": ""}],
              "test_mode": "Start with a test key.", "docs_url": "https://shop.example/docs",
              "agents_can": ["list widgets", "publish widget pages (asks you first)"]},
    "health": _api_health, "metrics": _api_metrics, "tools": _api_tools,
    "test_mode": lambda inst: "_test_" in connectors.secrets(inst).get("api_key", ""),
}


class StubClient:
    """Stands in for mcp.Client: a fixed tool list, recorded calls, and a close flag."""

    TOOLS = [
        {"name": "list_pages", "description": "List the open browser pages",
         "inputSchema": {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#",
                         "additionalProperties": False, "properties": {"limit": {"type": "integer"}},
                         "required": ["limit", "ghost"]}},
        {"name": "navigate", "description": "Open a web page in the browser",
         "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
        {"name": "send_message", "description": "Send a message through a contact form on a site",
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
        {"name": "mystery_tool", "description": "Does something nobody classified",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "api_write", "description": "Write through the service API with a method and a path",
         "inputSchema": {"type": "object", "properties": {"method": {"type": "string"},
                                                           "path": {"type": "string"}}}},
        {"name": "evil_eval", "description": "Run any script in the page",
         "inputSchema": {"type": "object", "properties": {"script": {"type": "string"}}}},
        {"name": "broken_tool", "description": "Always reports an error",
         "inputSchema": {"type": "object", "properties": {}}},
    ]

    def __init__(self, **kw):
        self.kw = kw
        self.calls: list = []
        self.closed = False
        self.list_count = 0
        self.connected = False

    def connect(self):
        self.connected = True
        return self

    def list_tools(self):
        self.list_count += 1
        return [dict(t) for t in self.TOOLS]

    def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "broken_tool":
            return {"content": [{"type": "text", "text": "it broke"}], "isError": True}
        return {"content": [{"type": "text", "text": f"{name} ok"}], "isError": False}

    def close(self):
        self.closed = True


def fake_text_of(result: dict) -> str:
    text = "\n".join(b.get("text", "") for b in result.get("content") or [] if b.get("type") == "text")
    return ("ERROR: " + text) if result.get("isError") else text


def _refuse(tool, args):
    if tool == "navigate" and re.search(r"127\.0\.0\.1|localhost", str(args.get("url") or "")):
        return "use browser_check for your own site"
    return None


def _tag_for_call(tool, args):
    if tool == "api_write" and re.search(r"refund|cancel|delete", f"{args.get('method')} {args.get('path')}", re.I):
        return "irreversible"
    return None


def _mcp_health(inst):
    names = [t["name"] for t in connectors.client(inst).list_tools()]
    return ("ok", f"{len(names)} tools") if "navigate" in names else ("bad", "navigate is missing")


TESTMCP = {
    "label": "Test Browser", "category": "Browser", "kind": "mcp-stdio", "scope": "global",
    "setup": {"where_to_get_key": "Nothing to get.", "permissions": [],
              "fields": [{"key": "token", "label": "Token", "secret": True, "hint": "", "placeholder": ""},
                         {"key": "profile", "label": "Profile", "secret": False, "hint": "", "placeholder": ""}],
              "test_mode": "", "docs_url": "https://browser.example/docs", "agents_can": ["open pages"]},
    "health": _mcp_health,
    "mcp": {"command": "browser-server", "args": ["--profile", "{profile}", "--token", "{token}"],
            "env": {"TB_TOKEN": "{token}"},
            "classify": [("list_*", "read"), ("navigate", "read"), ("send_*", "contact"), ("api_write", "publish"),
                         ("broken_tool", "read")],
            "allow": {"*": ["list_*", "navigate"], "engineering": ["*"]},
            "blocked": ["evil_eval"]},
    "refuse_call": _refuse, "tag_for_call": _tag_for_call,
}


class MemoryVault:
    """In-memory stand-in for the vault (the real one is built in parallel)."""

    def __init__(self):
        self.data: dict[str, str] = {}

    def put(self, name, value):
        self.data[name] = value

    def get(self, name):
        return self.data.get(name)

    def delete(self, name):
        self.data.pop(name, None)

    def names(self):
        return sorted(self.data)

    def redact(self, text):
        for v in self.data.values():
            if v and len(v) >= 8:
                text = text.replace(v, "[redacted]")
        return text


# ---------------------------------------------------------------- base

class Base(unittest.TestCase):
    def setUp(self):
        self.vault = MemoryVault()
        self.stub = StubClient()
        self.alerts: list = []
        patches = [mock.patch.object(vault, n, getattr(self.vault, n)) for n in ("put", "get", "delete", "names",
                                                                                 "redact")]
        patches += [mock.patch.object(connectors, "client", lambda inst: self.stub),
                    mock.patch.object(mcp, "text_of", fake_text_of),
                    mock.patch.object(notify, "send", side_effect=lambda text, *a, **kw: self.alerts.append(text)),
                    # the test switch: only with it may a card's hidden api_base be set through the API
                    mock.patch.dict(os.environ, {"HOLDCO_ALLOW_LOCAL": "127.0.0.1:*"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        connectors.DEFINITIONS["testapi"] = TESTAPI
        connectors.DEFINITIONS["testmcp"] = TESTMCP
        self.addCleanup(self._cleanup)
        db.set_settings({"kill_switch": False})
        CALLS.clear()
        self.c = make_company()

    def _cleanup(self):
        for r in db.q("SELECT id FROM connectors WHERE key IN ('testapi', 'testmcp', 'slowapi')"):
            with quiet():
                connectors.remove(r["id"])
        for k in ("testapi", "testmcp", "slowapi"):
            connectors.DEFINITIONS.pop(k, None)
        connectors._clients.clear()
        connectors._tool_cache.clear()
        connectors._alerted.clear()

    def connect_api(self, key=TEST_KEY, company=None, **extra) -> dict:
        out = call("POST", "/api/connectors", {"key": "testapi", "company_id": (company or self.c)["id"],
                                               "enabled": True, "config": {"api_key": key, **extra}})
        return out

    def connect_mcp(self, **config_values) -> dict:
        return call("POST", "/api/connectors", {"key": "testmcp", "company_id": None, "enabled": True,
                                                "config": config_values})


# ================================================================ risk tags and policies

class TestRiskTags(Base):
    def test_every_registered_tool_has_a_tag(self):
        for name, t in tools.REGISTRY.items():
            self.assertIn(t.risk, config.RISK_TAGS, name)
            self.assertEqual(tools.risk_of(name), t.risk)

    def test_builtin_tags_match_the_table(self):
        table = {
            "read": ["web_search", "fetch_url", "read_file", "list_files", "recall", "list_tasks", "check_inbox",
                     "audit_page", "check_contrast", "review_text", "browser_check", "find_tool"],
            "write": ["write_file", "delete_file", "create_task", "update_task", "remember", "record_metric",
                      "update_charter", "hire_agent", "note_progress", "mark_email_handled", "report_to_owner",
                      "ask_owner", "finish", "run_code"],
            "contact": ["send_email"],
            "publish": ["social_post", "deploy_site", "http_request"],
            "spend": ["request_purchase"],
        }
        for tag, names in table.items():
            for n in names:
                self.assertIn(n, tools.REGISTRY, n)
                self.assertEqual(tools.risk_of(n), tag, n)

    def test_policy_for_each_tag(self):
        self.assertIsNone(connectors.policy_for("read"))
        self.assertEqual(connectors.policy_for("write"), "connector_write")
        self.assertEqual(connectors.policy_for("publish"), "connector_publish")
        self.assertEqual(connectors.policy_for("contact"), "connector_contact")
        self.assertEqual(connectors.policy_for("spend"), "spend_money")
        self.assertEqual(connectors.policy_for("irreversible"), "irreversible")
        self.assertEqual(connectors.policy_for("unclassified"), "connector_publish")   # an unmatched MCP tool asks
        for k in ("connector_write", "connector_publish", "connector_contact", "irreversible"):
            self.assertIn(k, config.POLICY_ACTIONS)
            self.assertEqual(config.DEFAULT_POLICIES[k], "approve")

    def test_irreversible_and_spend_are_locked(self):
        self.assertTrue({"spend_money", "irreversible"} <= set(config.LOCKED_POLICIES))
        c = set_policies(self.c, irreversible="auto", spend_money="auto", connector_publish="auto")
        self.assertEqual(db.effective_policy(c, "irreversible"), "approve")
        self.assertEqual(db.effective_policy(c, "spend_money"), "approve")
        self.assertEqual(db.effective_policy(c, "connector_publish"), "auto")

    def test_gate_uses_the_policy_action(self):
        inst_id = self.connect_api()["instance"]["id"]
        payload = {"connector_id": inst_id, "connector": "testapi", "tool": "publish_widget",
                   "args": {"title": "Gated"}, "tag": "publish"}
        agent = agent_of(self.c, "marketing")
        c = set_policies(self.c, connector_publish="off")
        out = actions.gate(c, agent, None, "connector_call", payload, policy_action="connector_publish")
        self.assertIn("Not allowed", out)
        self.assertIn("Connected service call", out)
        c = set_policies(self.c, connector_publish="approve", connector_call="off")
        out = actions.gate(c, agent, None, "connector_call", payload, policy_action="connector_publish")
        self.assertIn("Queued for the owner's approval", out)
        row = db.one("SELECT * FROM approvals WHERE company_id=? AND action='connector_call'", (c["id"],))
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["summary"], 'testapi publish_widget({"title": "Gated"}) [publish]')
        self.assertEqual(CALLS, [])


# ================================================================ connectors

class TestConnectors(Base):
    def test_every_definition_has_a_setup_card_health_and_tagged_tools(self):
        self.assertIn("testapi", connectors.DEFINITIONS)
        for key, d in connectors.DEFINITIONS.items():
            with self.subTest(key=key):
                self.assertTrue(d.get("label") and d.get("category"))
                self.assertIn(d["kind"], connectors.KINDS)
                self.assertIn(d.get("scope", "company"), connectors.SCOPES)
                setup = d["setup"]
                for k in ("where_to_get_key", "permissions", "fields", "test_mode", "docs_url", "agents_can"):
                    self.assertIn(k, setup)
                for f in setup["fields"]:
                    self.assertTrue({"key", "label", "secret"} <= set(f), f)
                self.assertTrue(callable(d["health"]))
                for t in connectors._api_tools(d, connectors._placeholder(key)):
                    self.assertIn(t["tag"], config.RISK_TAGS, t["name"])
                    self.assertTrue(callable(t["fn"]))
                rules = (d.get("mcp") or {}).get("classify") or []
                for _glob, tag in (rules.items() if isinstance(rules, dict) else rules):
                    self.assertIn(tag, config.RISK_TAGS)

    def test_post_creates_an_instance_puts_the_secret_in_the_vault_and_runs_health(self):
        out = self.connect_api(shop_name="  Paws  ", api_base="http://127.0.0.1:9/fake", stray="dropped")
        inst = out["instance"]
        self.assertEqual(out["health"], {"status": "ok", "detail": "test mode, all good"})
        self.assertEqual(inst["config"], {"api_key": MASK, "shop_name": "Paws", "api_base": "http://127.0.0.1:9/fake"})
        self.assertEqual((inst["key"], inst["company_id"], inst["enabled"], inst["mode"]),
                         ("testapi", self.c["id"], True, "test"))
        self.assertEqual(self.vault.data, {f"connector.{inst['id']}.api_key": TEST_KEY})
        row = db.one("SELECT * FROM connectors WHERE id=?", (inst["id"],))
        self.assertNotIn(TEST_KEY, json.dumps(row))
        self.assertEqual((row["status"], row["detail"]), ("ok", "test mode, all good"))
        self.assertTrue(row["last_check"])
        self.assertEqual(connectors.instance("testapi", self.c)["id"], inst["id"])
        self.assertEqual(connectors.secrets(connectors.get(inst["id"])), {"api_key": TEST_KEY})

        listing = call("GET", "/api/connectors", query={"company_id": self.c["id"]})
        card = listing["definitions"]["testapi"]
        self.assertEqual((card["label"], card["kind"], card["scope"]), ("Widget Shop", "api", "company"))
        self.assertEqual(card["setup"]["agents_can"], TESTAPI["setup"]["agents_can"])
        self.assertIn({"name": "testapi__publish_widget", "tool": "publish_widget",
                       "description": "Publish a widget page for sale with a price", "tag": "publish"}, card["tools"])
        self.assertEqual(listing["definitions"]["testmcp"]["blocked"], ["evil_eval"])
        listed = [i for i in listing["instances"] if i["id"] == inst["id"]][0]
        self.assertEqual(listed["config"]["api_key"], MASK)
        self.assertEqual(listed["company_name"], self.c["name"])
        self.assertEqual(listing["statuses"][str(inst["id"])]["status"], "ok")
        self.assertNotIn(TEST_KEY, json.dumps(listing))

    def test_the_mask_keeps_a_secret_and_an_empty_value_deletes_it(self):
        iid = self.connect_api()["instance"]["id"]
        name = f"connector.{iid}.api_key"
        out = call("POST", "/api/connectors", {"id": iid, "key": "testapi", "config": {"api_key": MASK,
                                                                                       "shop_name": "New"}})
        self.assertEqual(self.vault.data[name], TEST_KEY)
        self.assertEqual(out["instance"]["config"]["shop_name"], "New")
        self.assertEqual(out["health"]["status"], "ok")
        out = call("POST", "/api/connectors", {"id": iid, "config": {"api_key": "", "shop_name": ""}})
        self.assertNotIn(name, self.vault.data)
        self.assertEqual(out["instance"]["config"], {"api_key": ""})
        self.assertEqual(out["health"], {"status": "bad", "detail": "No API key is set"})

    def test_validation(self):
        with self.assertRaises(routing.HTTPError) as e:
            call("POST", "/api/connectors", {"key": "nope", "company_id": self.c["id"]})
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(routing.HTTPError) as e:
            call("POST", "/api/connectors", {"key": "testapi", "config": {"api_key": TEST_KEY}})
        self.assertEqual(e.exception.status, 400)
        self.assertIn("per company", e.exception.message)
        with self.assertRaises(routing.HTTPError) as e:
            call("POST", "/api/connectors", {"key": "testapi", "company_id": 999999})
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(routing.HTTPError) as e:
            call("POST", "/api/connectors", {"id": 999999, "key": "testapi"})
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(routing.HTTPError) as e:
            self.connect_api(shop_name={"nested": True})
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(routing.HTTPError) as e:
            self.connect_api(api_base="file:///etc/passwd")
        self.assertEqual(e.exception.status, 400)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM connectors WHERE key='testapi'"), 0)
        iid = self.connect_api()["instance"]["id"]
        with self.assertRaises(routing.HTTPError) as e:
            call("POST", "/api/connectors", {"id": iid, "key": "testmcp"})
        self.assertEqual(e.exception.status, 400)
        # One instance per (key, company): saving again without the id updates the same row.
        again = self.connect_api(shop_name="Second")["instance"]
        self.assertEqual(again["id"], iid)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM connectors WHERE key='testapi'"), 1)
        # A global connector ignores the company it was sent with.
        g = call("POST", "/api/connectors", {"key": "testmcp", "company_id": self.c["id"],
                                             "config": {"profile": "p1"}})["instance"]
        self.assertIsNone(g["company_id"])
        self.assertEqual(g["config"], {"token": "", "profile": "p1"})

    def test_failing_health_doctor_row_daily_alert_and_prompt(self):
        with doctor._lock:
            saved = list(doctor.LAST)
            doctor.LAST[:] = [doctor._row("disk", "Disk space", "ok", "plenty")]

        def restore():
            with doctor._lock:
                doctor.LAST[:] = saved
        self.addCleanup(restore)
        out = self.connect_api(key=BAD_KEY)
        iid = out["instance"]["id"]
        self.assertEqual(out["health"], {"status": "bad", "detail": "key rejected"})
        self.assertEqual(self.alerts, [])   # saving shows the result on the card; the phone is for scheduled checks
        rows = {r["key"]: r for r in connectors.doctor_rows()}
        row = rows[f"connector:{iid}"]
        self.assertEqual((row["status"], row["detail"], row["company_id"]), ("bad", "key rejected", self.c["id"]))
        self.assertEqual(row["label"], f"Widget Shop ({self.c['name']})")
        self.assertEqual(row["fix"]["href"], f"#/c/{self.c['id']}/settings")
        # The Doctor's cached rows pick the result up at once (no wait for the hourly run).
        cached = {r["key"]: r for r in doctor.LAST}
        self.assertEqual(cached[f"connector:{iid}"]["status"], "bad")
        self.assertEqual(cached["disk"]["detail"], "plenty")

        connectors.check_all(alert=True)
        connectors.check_all(alert=True)
        mine = [a for a in self.alerts if a.startswith("Connector down:")]
        self.assertEqual(len(mine), 1)
        self.assertIn("Widget Shop", mine[0])
        self.assertIn("key rejected", mine[0])
        self.assertNotIn(BAD_KEY, mine[0])

        line = "Widget Shop: key rejected. Do not plan work that depends on it."
        self.assertEqual(connectors.down_for(self.c), [line])
        section = connectors.prompt_section(self.c)
        self.assertIn("Connectors that are down right now:\n- " + line, section)
        prompt = agents.system_prompt(self.c, agent_of(self.c, "engineering"), "task", 14)
        self.assertIn("# Connected services\n- testapi (live mode)\nUse find_tool to load a tool from them.", prompt)
        self.assertIn(line, prompt)
        self.assertNotIn(BAD_KEY, prompt)

    def test_health_that_raises_or_hangs_is_bad(self):
        def boom(inst):
            raise ValueError("exploded")

        def hang(inst):
            time.sleep(2)
            return "ok", "late"

        connectors.DEFINITIONS["slowapi"] = {**TESTAPI, "label": "Slow", "health": boom}
        out = call("POST", "/api/connectors", {"key": "slowapi", "company_id": self.c["id"], "config": {}})
        self.assertEqual(out["health"], {"status": "bad", "detail": "exploded"})
        connectors.DEFINITIONS["slowapi"] = {**TESTAPI, "label": "Slow", "health": hang, "health_timeout": 0.2}
        out = call("POST", f"/api/connectors/{out['instance']['id']}/test", {})
        self.assertEqual(out["health"]["status"], "bad")
        self.assertIn("took longer than 0.2 s", out["health"]["detail"])
        self.assertEqual(out["metrics"], [])

    def test_check_if_stale_runs_in_the_background_at_most_once(self):
        iid = self.connect_api()["instance"]["id"]
        before = db.one("SELECT last_check FROM connectors WHERE id=?", (iid,))["last_check"]
        db.update("connectors", iid, status="sentinel")
        connectors.check_if_stale()   # checked a moment ago: nothing is due
        time.sleep(0.2)
        self.assertFalse(connectors._checking)
        self.assertEqual(db.one("SELECT status FROM connectors WHERE id=?", (iid,))["status"], "sentinel")
        connectors.mark_stale(iid)
        connectors._checking = True
        try:
            connectors.check_if_stale()   # a pass is already running
            self.assertIsNone(db.one("SELECT last_check FROM connectors WHERE id=?", (iid,))["last_check"])
        finally:
            connectors._checking = False
        connectors.check_if_stale()
        deadline = time.time() + 10
        while time.time() < deadline and (connectors._checking or not db.one(
                "SELECT last_check FROM connectors WHERE id=?", (iid,))["last_check"]):
            time.sleep(0.05)
        row = db.one("SELECT * FROM connectors WHERE id=?", (iid,))
        self.assertTrue(row["last_check"])
        self.assertEqual(row["status"], "ok")
        self.assertTrue(before)

    def test_poll_metrics_writes_rows_with_a_source(self):
        iid = self.connect_api()["instance"]["id"]
        ctx = ctx_for(self.c, "marketing")
        tools.record_metric(ctx, "signups", 4, "typed by hand")
        db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, created_at) VALUES(?,?,?,?,?,?)",
               (self.c["id"], None, "old_metric", 2, "", db.now()))
        written = connectors.poll_metrics()
        mine = [w for w in written if w["company_id"] == self.c["id"]]
        self.assertEqual({w["name"]: w["value"] for w in mine},
                         {"testapi.revenue_usd_30d": 19.0, "testapi.payments_30d": 1.0, "email.sent_7d": 3.0})
        rows = {r["name"]: r for r in db.q("SELECT * FROM metrics WHERE company_id=?", (self.c["id"],))}
        self.assertEqual(rows["testapi.revenue_usd_30d"]["source"], "testapi")
        self.assertIsNone(rows["testapi.revenue_usd_30d"]["agent_id"])
        self.assertEqual(rows["testapi.revenue_usd_30d"]["note"], "1 payment")
        self.assertEqual(rows["email.sent_7d"]["source"], "testapi")
        self.assertEqual(rows["signups"]["source"], "manual")
        self.assertEqual(rows["old_metric"]["source"], "manual")

        api = call("GET", f"/api/companies/{self.c['id']}/metrics")["metrics"]
        self.assertEqual({m["name"]: m["source"] for m in api}["testapi.revenue_usd_30d"], "testapi")
        self.assertEqual({m["name"]: m["source"] for m in api}["signups"], "manual")
        overview = call("GET", f"/api/companies/{self.c['id']}")["metrics"]
        self.assertEqual({m["name"]: m["source"] for m in overview},
                         {"email.sent_7d": "testapi", "old_metric": "manual", "signups": "manual",
                          "testapi.payments_30d": "testapi", "testapi.revenue_usd_30d": "testapi"})
        text = agents._metrics_text(self.c["id"])
        self.assertIn("- testapi.revenue_usd_30d: 19 as of ", text)
        self.assertIn("(source: testapi)", text)
        self.assertRegex(text, r"- signups: 4 as of \S+ \(manual\)")
        # The card shows the latest polled numbers.
        listed = call("GET", "/api/connectors", query={"company_id": self.c["id"]})["instances"]
        card = [i for i in listed if i["id"] == iid][0]
        self.assertEqual({m["name"] for m in card["metrics"]},
                         {"testapi.revenue_usd_30d", "testapi.payments_30d", "email.sent_7d"})

    def test_poll_if_due_runs_in_the_background_on_its_interval(self):
        self.connect_api()
        connectors._last_poll = None
        with mock.patch.dict(os.environ, {"HOLDCO_METRICS_POLL_SECONDS": "3600"}):
            connectors.poll_if_due()
            deadline = time.time() + 10
            while time.time() < deadline and (connectors._polling or not db.scalar(
                    "SELECT COUNT(*) FROM metrics WHERE company_id=? AND source='testapi'", (self.c["id"],))):
                time.sleep(0.05)
            n = db.scalar("SELECT COUNT(*) FROM metrics WHERE company_id=? AND source='testapi'", (self.c["id"],))
            self.assertEqual(n, 3)
            connectors.poll_if_due()   # not due again for an hour
            time.sleep(0.2)
            self.assertFalse(connectors._polling)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM metrics WHERE company_id=? AND source='testapi'",
                                       (self.c["id"],)), 3)
        connectors._last_poll = None

    def test_test_endpoint_runs_health_and_one_poll(self):
        iid = self.connect_api()["instance"]["id"]
        out = call("POST", f"/api/connectors/{iid}/test", {})
        self.assertEqual(out["health"], {"status": "ok", "detail": "test mode, all good"})
        self.assertEqual({m["name"] for m in out["metrics"]},
                         {"testapi.revenue_usd_30d", "testapi.payments_30d", "email.sent_7d"})
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM metrics WHERE company_id=? AND source='testapi'",
                                   (self.c["id"],)), 3)
        with self.assertRaises(routing.HTTPError) as e:
            call("POST", "/api/connectors/999999/test", {})
        self.assertEqual(e.exception.status, 404)

    def test_delete_removes_the_vault_secrets_and_the_row(self):
        iid = self.connect_api()["instance"]["id"]
        self.vault.put(f"oauth.{iid}", json.dumps({"access_token": "tok_123456789"}))
        self.vault.put(f"connector.{iid}.extra", "leftover-value")
        self.vault.put("settings.brave_api_key", "unrelated-secret")
        self.assertEqual(call("DELETE", f"/api/connectors/{iid}", {}), {"ok": True})
        self.assertEqual(self.vault.data, {"settings.brave_api_key": "unrelated-secret"})
        self.assertIsNone(connectors.get(iid))
        self.assertNotIn(f"connector:{iid}", {r["key"] for r in connectors.doctor_rows()})
        with self.assertRaises(routing.HTTPError) as e:
            call("DELETE", f"/api/connectors/{iid}", {})
        self.assertEqual(e.exception.status, 404)

    def test_oauth_start_route(self):
        iid = self.connect_mcp()["instance"]["id"]
        with mock.patch.object(oauth, "start", return_value="https://auth.example/authorize?x=1") as start:
            self.assertEqual(call("POST", f"/api/connectors/{iid}/oauth/start", {}),
                             {"url": "https://auth.example/authorize?x=1"})
        start.assert_called_once_with(iid)
        with mock.patch.object(oauth, "start", side_effect=oauth.OAuthError("no authorization server found")):
            with self.assertRaises(routing.HTTPError) as e:
                call("POST", f"/api/connectors/{iid}/oauth/start", {})
        self.assertEqual((e.exception.status, e.exception.message), (400, "no authorization server found"))

    def test_global_instance_serves_every_company(self):
        other = make_company("OtherCo")
        out = self.connect_mcp(profile="main")
        iid = out["instance"]["id"]
        self.assertEqual(out["health"], {"status": "ok", "detail": "7 tools"})
        for c in (self.c, other):
            self.assertEqual([i["id"] for i in connectors.visible(c)], [iid])
            self.assertEqual(connectors.prompt_section(c),
                             "# Connected services\n- testmcp\nUse find_tool to load a tool from them.")
        row = {r["key"]: r for r in connectors.doctor_rows()}[f"connector:{iid}"]
        self.assertEqual((row["label"], row["company_id"], row["fix"]["href"]), ("Test Browser (global)", None,
                                                                                 "#/settings"))
        # Turning it off: nothing is visible, the Doctor drops it, the prompt section is empty.
        out = call("POST", "/api/connectors", {"id": iid, "enabled": False})
        self.assertEqual(out["health"]["status"], "warn")
        self.assertEqual(connectors.visible(self.c), [])
        self.assertEqual(connectors.prompt_section(self.c), "")
        self.assertNotIn(f"connector:{iid}", {r["key"] for r in connectors.doctor_rows()})

    def test_mcp_clients_are_cached_filled_and_closed(self):
        made: list = []

        def factory(name, transport, **kw):
            s = StubClient(name=name, transport=transport, **kw)
            made.append(s)
            return s

        # This test uses the real connectors.client with a fake mcp.Client.
        with mock.patch.object(connectors, "client", REAL_CLIENT), \
                mock.patch.object(mcp, "Client", side_effect=factory):
            out = self.connect_mcp(profile="work", token="tb_secret_token_1")
            iid = out["instance"]["id"]
            self.assertEqual(out["health"]["status"], "ok")
            first = made[-1]
            self.assertEqual(first.kw, {"name": "testmcp", "transport": "stdio", "command": "browser-server",
                                        "args": ["--profile", "work", "--token", "tb_secret_token_1"],
                                        "env": {"TB_TOKEN": "tb_secret_token_1"}})
            self.assertTrue(first.connected)
            inst = connectors.get(iid)
            self.assertIs(connectors.client(inst), first)   # kept open between calls
            first.closed = True                              # the process died: the next use re-creates it
            second = connectors.client(inst)
            self.assertIsNot(second, first)
            # Disabling closes the client (in the background).
            call("POST", "/api/connectors", {"id": iid, "enabled": False})
            deadline = time.time() + 5
            while time.time() < deadline and not second.closed:
                time.sleep(0.02)
            self.assertTrue(second.closed)
            self.assertNotIn(iid, connectors._clients)
            call("POST", "/api/connectors", {"id": iid, "enabled": True})
            third = connectors._clients[iid]
            call("DELETE", f"/api/connectors/{iid}", {})
            deadline = time.time() + 5
            while time.time() < deadline and not third.closed:
                time.sleep(0.02)
            self.assertTrue(third.closed)

    def test_facts_list_connected_services_and_connector_metrics_count_as_real(self):
        self.connect_api(shop_name="Paws")
        facts = truth.facts_section(self.c)
        self.assertIn("Accounts this company has: Widget Shop (payments, test mode).", facts)
        self.assertIn("Signups, revenue, replies: none recorded from a real source.", facts)
        tools.record_metric(ctx_for(self.c, "marketing"), "revenue_usd", 500)
        self.assertIn("none recorded from a real source", truth.facts_section(self.c))   # typed: not real
        for name, value in (("stripe.revenue_usd_30d", 19), ("stripe.payments_30d", 1), ("stripe.mrr_usd", 0)):
            db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, source, created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (self.c["id"], None, name, value, "", "stripe", db.now()))
        facts = truth.facts_section(self.c)
        self.assertIn("Recorded from a real source: revenue 19 (stripe.revenue_usd_30d, from stripe).", facts)
        api = call("GET", f"/api/companies/{self.c['id']}/facts")
        self.assertIn({"kind": "connector", "key": "testapi", "label": "Widget Shop (payments, test mode)"},
                      api["accounts"])
        self.assertEqual(api["signals"]["revenue"][0]["name"], "stripe.revenue_usd_30d")


# ================================================================ find_tool and routing

class TestFindTool(Base):
    def setUp(self):
        super().setUp()
        self.api_id = self.connect_api()["instance"]["id"]
        self.mcp_id = self.connect_mcp()["instance"]["id"]

    def test_catalog_from_an_api_definition_and_an_mcp_server(self):
        mk = {e["name"]: e for e in connectors.catalog(self.c, agent_of(self.c, "marketing"))}
        self.assertEqual(set(mk), {"testapi__list_widgets", "testapi__publish_widget", "testapi__refund_order",
                                   "testapi__leaky_read", "testmcp__list_pages", "testmcp__navigate",
                                   "testmcp__send_message", "testmcp__mystery_tool", "testmcp__api_write",
                                   "testmcp__broken_tool"})   # evil_eval is blocked: never listed
        self.assertEqual({n: (e["tag"], e["allowed"]) for n, e in mk.items() if n.startswith("testmcp")},
                         {"testmcp__list_pages": ("read", True), "testmcp__navigate": ("read", True),
                          "testmcp__send_message": ("contact", False), "testmcp__mystery_tool": ("unclassified", False),
                          "testmcp__api_write": ("publish", False), "testmcp__broken_tool": ("read", True)})
        self.assertTrue(all(e["allowed"] for n, e in mk.items() if n.startswith("testapi")))
        self.assertEqual(mk["testapi__publish_widget"]["instance_id"], self.api_id)
        self.assertEqual(mk["testmcp__list_pages"]["instance_id"], self.mcp_id)
        # MCP schemas keep only type, properties, required and description; required names must exist.
        self.assertEqual(mk["testmcp__list_pages"]["parameters"],
                         {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": ["limit"]})
        self.assertEqual(mk["testapi__refund_order"]["parameters"]["properties"],
                         {"order_id": {"type": "string", "description": "The order"}})
        eng = {e["name"]: e for e in connectors.catalog(self.c, agent_of(self.c, "engineering"))}
        self.assertTrue(eng["testmcp__send_message"]["allowed"] and eng["testmcp__mystery_tool"]["allowed"])
        # tools/list is cached per instance for ten minutes.
        n = self.stub.list_count
        connectors.catalog(self.c, agent_of(self.c, "marketing"))
        self.assertEqual(self.stub.list_count, n)
        stamp, listed = connectors._tool_cache[self.mcp_id]
        connectors._tool_cache[self.mcp_id] = (stamp - connectors.TOOL_CACHE_SECONDS - 1, listed)
        connectors.catalog(self.c, agent_of(self.c, "marketing"))
        self.assertEqual(self.stub.list_count, n + 1)

    def test_scoring_loads_the_top_three(self):
        ctx = ctx_for(self.c, "marketing")
        out = connectors.find_tool(ctx, "publish a widget page for sale")
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("testapi__publish_widget [publish]: Publish a widget page"), out)
        self.assertTrue(out.endswith("You can call them now."))
        self.assertEqual(len(ctx.loaded_tools), 3)
        self.assertEqual(ctx.loaded_tools[0]["name"], "testapi__publish_widget")
        self.assertIn("Loaded: testapi__publish_widget, ", out)
        self.assertEqual(set(ctx.loaded_by_name), {t["name"] for t in ctx.loaded_tools})
        self.assertEqual(set(ctx.loaded_tools[0]["parameters"]), {"type", "properties", "required"})
        self.assertIn("may wait for the owner", ctx.loaded_tools[0]["description"])
        # Ties are broken by name; a second search adds only what is new.
        out = connectors.find_tool(ctx, "widget")
        self.assertEqual(out.splitlines()[:2], ["testapi__list_widgets [read]: List the widgets in the shop",
                                                "testapi__publish_widget [publish]: Publish a widget page for sale "
                                                "with a price"])
        self.assertEqual([t["name"] for t in ctx.loaded_tools].count("testapi__publish_widget"), 1)
        self.assertEqual(len(ctx.loaded_tools), len({t["name"] for t in ctx.loaded_tools}))
        # No match names what is connected, and what it offers (Phase 3 bench gate), so the next search can hit it.
        out = connectors.find_tool(ctx, "zebra stripes")
        first, offer = out.splitlines()
        self.assertEqual(first, "No connected tool matches 'zebra stripes'. Connected services: testapi (test mode), "
                                "testmcp.")
        self.assertTrue(offer.startswith("What they offer: "), offer)
        self.assertIn("testapi__list_widgets (List the widgets in the shop)", offer)
        self.assertTrue(offer.endswith(". Search again with words from these."), offer)
        lonely = make_company("LonelyCo")
        call("POST", "/api/connectors", {"id": self.mcp_id, "enabled": False})
        self.assertEqual(connectors.find_tool(ctx_for(lonely, "marketing"), "widget"),
                         "No connected tool matches 'widget'. Connected services: none.")

    def run_script(self, company, role, script, task_title="Sell widgets"):
        """One task session where the model replays `script` [(tool, args)]. Returns the tool names the model was
        offered on each step, and the tool results."""
        tid = db.run("INSERT INTO tasks(company_id, title, role, status, created_by, created_at, updated_at) "
                     "VALUES(?,?,?,?,?,?,?)", (company["id"], task_title, role, "in_progress", "test", db.now(),
                                                db.now()))
        steps, offered = iter(script), []

        def fake_chat(messages, tools_spec=None, *a, **kw):
            offered.append([t["name"] for t in tools_spec or []])
            try:
                name, args = next(steps)
            except StopIteration:
                return llm.Reply(content="")
            return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)])

        with mock.patch.object(agents.llm, "chat", fake_chat):
            agents.run(agent_of(company, role)["id"], "task", task_id=tid)
        row = db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (agent_of(company, role)["id"],))
        results = [(m["name"], m["content"]) for m in json.loads(row["transcript"]) if m["role"] == "tool"]
        return tid, offered, results

    def test_agent_run_sees_the_loaded_tool_and_a_publish_is_queued_then_executed_and_audited(self):
        tid, offered, results = self.run_script(self.c, "marketing", [
            ("find_tool", {"query": "publish widget page"}),
            ("testapi__publish_widget", {"title": "Starter", "price_usd": "19"}),
            ("finish", {"summary": "Asked to publish the Starter widget", "status": "partial"})])
        self.assertIn("find_tool", offered[0])
        self.assertNotIn("testapi__publish_widget", offered[0])
        self.assertIn("testapi__publish_widget", offered[1])       # the loaded spec reaches the model next step
        self.assertEqual([n for n, _ in results], ["find_tool", "testapi__publish_widget", "finish"])
        self.assertIn("Queued for the owner's approval", results[1][1])
        self.assertEqual(CALLS, [])
        row = db.one("SELECT * FROM approvals WHERE company_id=? AND action='connector_call'", (self.c["id"],))
        payload = json.loads(row["payload"])
        self.assertEqual(payload, {"connector_id": self.api_id, "connector": "testapi", "tool": "publish_widget",
                                   "args": {"title": "Starter", "price_usd": 19.0}, "tag": "publish"})
        self.assertEqual(row["task_id"], tid)
        approvals = call("GET", "/api/approvals", query={"company_id": self.c["id"]})["approvals"]
        self.assertEqual([a["tag"] for a in approvals if a["id"] == row["id"]], ["publish"])

        done = actions.decide(row["id"], "approve")
        self.assertEqual(done["status"], "done")
        self.assertIn("Published Starter at https://shop.example/w/1", done["result"])
        self.assertEqual(CALLS, [("publish_widget", {"title": "Starter", "price_usd": 19.0}, self.c["id"])])
        audit = db.one("SELECT * FROM audit WHERE approval_id=?", (row["id"],))
        self.assertEqual((audit["action"], audit["approved_by"], audit["proposed_by"]),
                         ("connector_call", "owner", agent_of(self.c, "marketing")["name"]))
        self.assertEqual(json.loads(audit["payload"])["tag"], "publish")
        self.assertIn("Published Starter", audit["result"])

    def test_tools_execute_routes_a_loaded_read_tool_which_runs_free(self):
        ctx = ctx_for(self.c, "marketing")
        connectors.find_tool(ctx, "list widgets")
        out = tools.execute(ctx, "testapi__list_widgets", {"limit": "5"})
        self.assertEqual(out, f"2 widgets for {self.c['name']} (limit 5)")
        self.assertEqual(CALLS, [("list_widgets", {"limit": 5}, self.c["id"])])
        ev = db.one("SELECT * FROM events WHERE company_id=? AND kind='tool' AND message LIKE 'called %' "
                    "ORDER BY id DESC", (self.c["id"],))
        self.assertEqual(ev["message"], "called testapi list_widgets")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM approvals WHERE company_id=?", (self.c["id"],)), 0)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM audit WHERE company_id=?", (self.c["id"],)), 0)
        # A name that was never loaded is not a tool for this session.
        self.assertIn("there's no tool called 'testapi__refund_order'",
                      tools.execute(ctx, "testapi__refund_order", {"order_id": "o1"}))
        # A secret that leaks into a result never reaches the agent.
        connectors.find_tool(ctx, "diagnostic line")
        self.assertEqual(tools.execute(ctx, "testapi__leaky_read", {}), "the key is [redacted]")

    def test_publish_with_an_auto_policy_runs_and_is_audited(self):
        c = set_policies(self.c, connector_publish="auto")
        ctx = ctx_for(c, "marketing")
        connectors.find_tool(ctx, "publish widget")
        out = tools.execute(ctx, "testapi__publish_widget", {"title": "Auto"})
        self.assertIn("Published Auto", out)
        audit = db.one("SELECT * FROM audit WHERE company_id=? AND action='connector_call'", (c["id"],))
        self.assertEqual(audit["approved_by"], "policy:auto")
        self.assertIsNone(audit["approval_id"])

    def test_an_unclassified_mcp_tool_asks(self):
        ctx = ctx_for(self.c, "engineering")
        out = connectors.find_tool(ctx, "mystery")
        self.assertIn("testmcp__mystery_tool [unclassified]", out)
        out = tools.execute(ctx, "testmcp__mystery_tool", {})
        self.assertIn("Queued for the owner's approval", out)
        self.assertEqual(self.stub.calls, [])
        row = db.one("SELECT * FROM approvals WHERE company_id=? AND action='connector_call'", (self.c["id"],))
        self.assertEqual(json.loads(row["payload"])["tag"], "unclassified")
        done = actions.decide(row["id"], "approve")
        self.assertEqual((done["status"], done["result"]), ("done", "mystery_tool ok"))
        self.assertEqual(self.stub.calls, [("mystery_tool", {})])
        # The policy that applies is connector_publish: turned off, nothing is queued.
        c = set_policies(self.c, connector_publish="off")
        out = connectors.call(ctx_for(c, "engineering"), "testmcp__mystery_tool", {})
        self.assertIn("Not allowed", out)

    def test_a_role_outside_allow_is_refused(self):
        ctx = ctx_for(self.c, "marketing")
        self.assertEqual(connectors.call(ctx, "testmcp__send_message", {"text": "hi"}),
                         "ERROR: testmcp__send_message is not allowed for your role.")
        out = connectors.find_tool(ctx, "send message contact form")
        self.assertIn("testmcp__send_message [contact]: Send a message through a contact form on a site "
                      "(not allowed for your role)", out)
        self.assertNotIn("testmcp__send_message", ctx.loaded_by_name)
        self.assertEqual(self.stub.calls, [])
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM approvals WHERE company_id=?", (self.c["id"],)), 0)

    def test_blocked_tools_are_never_listed_and_always_refused(self):
        for role in ("marketing", "engineering"):
            ctx = ctx_for(self.c, role)
            self.assertNotIn("evil_eval", connectors.find_tool(ctx, "run any script in the page"))
            self.assertEqual(connectors.call(ctx, "testmcp__evil_eval", {"script": "1"}),
                             "ERROR: evil_eval is not available through Holdco")
        with self.assertRaises(RuntimeError):
            connectors.execute({"connector_id": self.mcp_id, "connector": "testmcp", "tool": "evil_eval",
                                "args": {}, "tag": "read"})
        self.assertEqual(self.stub.calls, [])

    def test_definition_hooks_refuse_and_raise_the_tag(self):
        ctx = ctx_for(self.c, "engineering")
        connectors.find_tool(ctx, "open web page navigate")
        self.assertEqual(tools.execute(ctx, "testmcp__navigate", {"url": "http://127.0.0.1:8420/preview/x/"}),
                         "ERROR: use browser_check for your own site")
        self.assertEqual(tools.execute(ctx, "testmcp__navigate", {"url": "https://example.com"}), "navigate ok")
        self.assertEqual(self.stub.calls, [("navigate", {"url": "https://example.com"})])
        c = set_policies(self.c, irreversible="auto", connector_publish="auto")
        ctx = ctx_for(c, "engineering")
        out = connectors.call(ctx, "testmcp__api_write", {"method": "POST", "path": "/v1/refunds"})
        self.assertIn("Queued for the owner's approval", out)   # irreversible is locked, whatever the override
        row = db.one("SELECT * FROM approvals WHERE company_id=? AND action='connector_call'", (c["id"],))
        self.assertEqual(json.loads(row["payload"])["tag"], "irreversible")
        out = connectors.call(ctx, "testmcp__api_write", {"method": "POST", "path": "/v1/products"})
        self.assertEqual(out, "api_write ok")   # an ordinary write keeps its publish tag (auto here)

    def test_a_hook_never_lowers_the_risk(self):
        self.assertEqual(connectors._raise_tag("publish", "irreversible"), "irreversible")
        self.assertEqual(connectors._raise_tag("spend", "unclassified"), "spend")
        self.assertEqual(connectors._raise_tag("irreversible", "read"), "irreversible")
        self.assertEqual(connectors._raise_tag("write", None), "write")
        self.assertEqual(connectors._raise_tag("read", "nonsense"), "read")

        def broken(tool, args):
            raise KeyError("method")

        with mock.patch.dict(TESTMCP, {"tag_for_call": broken}):
            out = connectors.call(ctx_for(self.c, "engineering"), "testmcp__list_pages", {"limit": 1})
        self.assertIn("Queued for the owner's approval", out)   # a broken rule asks instead of running freely
        row = db.one("SELECT * FROM approvals WHERE company_id=? AND action='connector_call'", (self.c["id"],))
        self.assertEqual(json.loads(row["payload"])["tag"], "unclassified")
        self.assertEqual(self.stub.calls, [])

    def test_mcp_error_results(self):
        ctx = ctx_for(self.c, "marketing")
        self.assertEqual(connectors.call(ctx, "testmcp__broken_tool", {}), "ERROR: it broke")
        with self.assertRaises(RuntimeError) as e:
            connectors.execute({"connector_id": self.mcp_id, "connector": "testmcp", "tool": "broken_tool",
                                "args": {}, "tag": "read"})
        self.assertEqual(str(e.exception), "it broke")
        # A failing approved call is recorded as FAILED, not as done.
        c = set_policies(self.c, connector_publish="auto")
        with mock.patch.object(self.stub, "call_tool", side_effect=mcp.MCPError("server went away")):
            out = connectors.call(ctx_for(c, "engineering"), "testmcp__mystery_tool", {})
        self.assertTrue(out.startswith("FAILED: Test Browser mystery_tool failed: server went away"), out)
        audit = db.one("SELECT * FROM audit WHERE company_id=? AND action='connector_call'", (c["id"],))
        self.assertTrue(audit["result"].startswith("FAILED:"))

    def test_a_removed_or_disabled_connector_is_not_called(self):
        ctx = ctx_for(self.c, "marketing")
        connectors.find_tool(ctx, "list widgets")
        call("POST", "/api/connectors", {"id": self.api_id, "enabled": False})
        out = tools.execute(ctx, "testapi__list_widgets", {})
        self.assertEqual(out, "ERROR: Widget Shop is no longer connected for this company.")
        self.assertEqual(CALLS, [])


if __name__ == "__main__":
    unittest.main()
