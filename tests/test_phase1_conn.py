"""Phase 1: the five connectors (Stripe, Resend, Playwright, Buffer, Brave) and browser_check.

The connector framework (holdco/connectors.py) is built separately, so these tests never depend on it: instances
are hand-built dicts, and connectors.secrets / connectors.instance / connectors.client are patched. Every outside
service is a fake on 127.0.0.1 (tests/fake_stripe.py, fake_resend.py, fake_buffer.py); nothing touches the network.

Run with:  python -m unittest tests.test_phase1_conn -v
"""
from __future__ import annotations

import base64
import email.utils
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-conn-")
os.environ.setdefault("HOLDCO_VAULT_BACKEND", "file")   # never touch a developer's keyring

from holdco import (actions, browser, config, conn_brave, conn_buffer, conn_playwright, conn_resend,  # noqa: E402
                    conn_stripe, connectors, db, emailer, mcp, roles, tools, web, workspace)
from tests.fake_buffer import FakeBuffer  # noqa: E402
from tests.fake_resend import FakeResend  # noqa: E402
from tests.fake_stripe import FakeStripe  # noqa: E402
from tests.helpers import free_port  # noqa: E402

db.init()

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJ"
                       "RU5ErkJggg==")   # a 1x1 image
STRIPE_KEY = "sk_test_holdco1234567890"
RESEND_KEY = "re_test_holdco1234567890"


def make_company(name="ConnCo", members=("ceo", "engineering", "marketing", "sales", "qa"), settings=None) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "An idea", "active", "building", json.dumps(settings or {}), db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def set_settings(company: dict, **values) -> dict:
    cs = json.loads(db.get_company(company["id"])["settings"] or "{}")
    cs.update(values)
    db.update("companies", company["id"], settings=json.dumps(cs))
    return db.get_company(company["id"])


def agent_of(company: dict, role: str) -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def ctx_for(company: dict, role: str = "qa") -> tools.Ctx:
    return tools.Ctx(company=company, agent=agent_of(company, role), kind="task", run_id=0, task=None,
                     settings=db.get_settings())


_ids = iter(range(9000, 100000))


def make_inst(key: str, company: dict | None, secrets: dict | None = None, status: str = "ok", detail: str = "",
              enabled: int = 1, **config_values) -> dict:
    """An instance row the way the framework hands it to a definition (config parsed, secrets kept aside)."""
    return {"id": next(_ids), "company_id": company["id"] if company else None, "key": key, "enabled": enabled,
            "config": dict(config_values), "status": status, "detail": detail, "_secrets": dict(secrets or {})}


def patched(instances: dict | None = None, client=None) -> ExitStack:
    """Patch the framework helpers the connectors call: secrets(inst), instance(key, company), client(inst)."""
    stack = ExitStack()
    stack.enter_context(mock.patch.object(connectors, "secrets", lambda inst: dict((inst or {}).get("_secrets") or {})))
    reg = dict(instances or {})
    stack.enter_context(mock.patch.object(connectors, "instance", lambda key, company=None: reg.get(key)))
    if client is not None:
        stack.enter_context(mock.patch.object(connectors, "client", lambda inst: client, create=True))
    return stack


def spec(definition: dict, inst: dict, name: str) -> dict:
    return next(t for t in definition["tools"](inst) if t["name"] == name)


class Recorder:
    """A local HTTP server that records POST bodies (a stand-in posting webhook)."""

    def __init__(self):
        self.bodies: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.bodies.append(json.loads(raw or b"{}"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/hook"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, context=None, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class FakeIMAP:
    messages: list[bytes] = []

    def __init__(self, host, port=993, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        return "OK", [b"logged in"]

    def select(self, box, readonly=False):
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b" ".join(str(i).encode() for i in range(1, len(self.messages) + 1))]
        raw = self.messages[int(args[0]) - 1]
        return "OK", [(b"%d (BODY[] {%d}" % (int(args[0]), len(raw)), raw), b")"]


class FakeBrowserClient:
    """Stands in for the Playwright MCP client: records calls and answers like the server would."""

    def __init__(self, fail_on: str | None = None, console: str = "", snapshot: str = "", tools_list=None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_on = fail_on
        self.console = console
        self.snapshot = snapshot
        self.tools_list = tools_list if tools_list is not None else [{"name": n} for n in
                                                                     conn_playwright.READ_TOOLS]

    def list_tools(self):
        if isinstance(self.tools_list, Exception):
            raise self.tools_list
        return self.tools_list

    def call_tool(self, name, args):
        self.calls.append((name, dict(args or {})))
        if name == self.fail_on:
            raise mcp.MCPError("browser crashed")
        if name == "browser_console_messages":
            return {"content": [{"type": "text", "text": self.console}], "isError": False}
        if name == "browser_snapshot":
            return {"content": [{"type": "text", "text": self.snapshot}], "isError": False}
        if name == "browser_take_screenshot":
            return {"content": [{"type": "text", "text": "Took the screenshot"},
                                {"type": "image", "mimeType": "image/png",
                                 "data": base64.b64encode(PNG).decode()}], "isError": False}
        return {"content": [{"type": "text", "text": f"ran {name}"}], "isError": False}


# =============================================================================================== definitions

class TestDefinitions(unittest.TestCase):
    MODULES = (conn_stripe, conn_resend, conn_playwright, conn_buffer, conn_brave)

    def test_every_definition_has_the_card_and_tagged_tools(self):
        defs = [m.DEFINITION for m in self.MODULES] + [conn_stripe.MCP_DEFINITION]
        for d in defs:
            with self.subTest(d["label"]):
                for k in ("label", "category", "kind", "scope", "setup", "health"):
                    self.assertIn(k, d)
                self.assertIn(d["kind"], ("api", "mcp-stdio", "mcp-http", "builtin"))
                self.assertIn(d["scope"], ("global", "company"))
                for k in ("where_to_get_key", "permissions", "fields", "test_mode", "docs_url", "agents_can"):
                    self.assertIn(k, d["setup"])
                for f in d["setup"]["fields"]:
                    for k in ("key", "label", "secret", "hint", "placeholder"):
                        self.assertIn(k, f)
                self.assertTrue(callable(d["health"]))
                for t in d.get("tools", lambda i: [])(None):
                    self.assertIn(t["tag"], config.RISK_TAGS)
                    self.assertNotIn("__", t["name"])
                    self.assertTrue(callable(t["fn"]))
                    self.assertEqual(t["parameters"]["type"], "object")
                text = json.dumps(d["setup"], ensure_ascii=False)
                self.assertNotIn("\u2014", text)   # no em dashes in owner-facing text
        self.assertEqual(conn_stripe.DEFINITIONS, {"stripe": conn_stripe.DEFINITION,
                                                   "stripe_mcp": conn_stripe.MCP_DEFINITION})
        self.assertEqual(conn_playwright.DEFINITION["kind"], "mcp-stdio")
        self.assertEqual(conn_playwright.DEFINITION["scope"], "global")
        self.assertEqual(conn_brave.DEFINITION["kind"], "builtin")
        self.assertEqual(conn_resend.DEFINITION["metric_prefix"], "email")
        self.assertTrue(conn_buffer.DEFINITION["beta"])
        self.assertFalse(conn_buffer.DEFINITION["enabled_by_default"])


# =============================================================================================== Stripe

class TestStripe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeStripe().start()
        cls.company = make_company("StripeCo")

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.clear()
        self.inst = make_inst("stripe", self.company, {"api_key": STRIPE_KEY}, api_base=self.fake.url)
        self.stack = patched({"stripe": self.inst})
        self.stack.__enter__()

    def tearDown(self):
        self.stack.__exit__(None, None, None)

    def call(self, _tool, **args):
        return spec(conn_stripe.DEFINITION, self.inst, _tool)["fn"](self.inst, self.company, **args)

    def test_create_payment_link_makes_product_price_and_link(self):
        out = self.call("create_payment_link", name="Starter plan", amount_usd=19, recurring_interval="month")
        self.assertEqual([r["path"] for r in self.fake.posts()], ["/v1/products", "/v1/prices", "/v1/payment_links"])
        product, price, link = (r["body"] for r in self.fake.posts())
        self.assertEqual(product, {"name": "Starter plan"})   # no empty description: Stripe refuses one
        self.assertEqual(price, {"product": "prod_1", "unit_amount": "1900", "currency": "usd",
                                 "recurring": {"interval": "month"}})
        self.assertEqual(link, {"line_items": [{"price": "price_1", "quantity": "1"}]})
        self.assertEqual([r["idempotency_key"] for r in self.fake.posts()], [None, None, None])
        self.assertEqual(out, f"Created payment link {self.fake.url}/pay/plink_1 (id plink_1, Starter plan, "
                              f"$19.00/month, test mode)")
        self.assertNotIn(STRIPE_KEY, out)
        once = self.call("create_payment_link", name="Guide", amount_usd="9.5", description="A PDF guide")
        self.assertIn("$9.50, test mode", once)
        self.assertNotIn("/", once.split("$9.50")[1].split(",")[0])   # one-time: no interval
        self.assertEqual(self.fake.prices["price_2"]["recurring"], None)
        for bad in ({"name": "", "amount_usd": 5}, {"name": "X", "amount_usd": 0},
                    {"name": "X", "amount_usd": 5, "recurring_interval": "fortnight"}):
            with self.assertRaises(ValueError):
                self.call("create_payment_link", **bad)

    def test_list_and_get_payment_links(self):
        self.call("create_payment_link", name="Starter plan", amount_usd=19, recurring_interval="month")
        self.call("create_payment_link", name="Pro plan", amount_usd=49, recurring_interval="year")
        listed = self.call("list_payment_links")
        self.assertIn("2 payment link(s)", listed)
        self.assertLess(listed.index("plink_2"), listed.index("plink_1"))   # newest first
        self.assertIn("active: true", listed)
        got = self.call("get_payment_link", id="plink_1")
        self.assertIn("id: plink_1", got)
        self.assertIn(f"url: {self.fake.url}/pay/plink_1", got)
        self.assertIn("active: true", got)
        self.assertIn("item: Starter plan, $19.00/month, quantity 1", got)
        self.assertIn("mode: test", got)
        by_url = self.call("get_payment_link", id=f"{self.fake.url}/pay/plink_2")
        self.assertIn("item: Pro plan, $49.00/year", by_url)
        with self.assertRaises(conn_stripe.StripeError) as cm:
            self.call("get_payment_link", id="plink_missing")
        self.assertEqual(cm.exception.status, 404)
        self.assertNotIn(STRIPE_KEY, str(cm.exception))

    def test_recent_payments_after_a_test_charge(self):
        self.assertIn("No successful payments", self.call("recent_payments"))
        self.fake.add_charge(amount=1900, currency="usd", paid=True, customer_email="owner@dogs.example")
        self.fake.add_charge(amount=500, paid=False)
        self.fake.add_charge(amount=7000, created=int(time.time()) - 20 * 86400)
        out = self.call("recent_payments", days=7)
        self.assertIn("1 successful payment(s) in the last 7 day(s), $19.00 USD", out)
        self.assertIn("owner@dogs.example", out)
        self.assertIn("pi_1", out)
        self.assertIn("2 successful", self.call("recent_payments", days=30))

    def test_create_refund_is_irreversible_and_always_queued(self):
        refund = spec(conn_stripe.DEFINITION, self.inst, "create_refund")
        self.assertEqual(refund["tag"], "irreversible")
        self.assertEqual(connectors.policy_for("irreversible"), "irreversible")
        company = set_settings(self.company, policies={"irreversible": "auto"})   # locked: an override is ignored
        payload = {"connector_id": self.inst["id"], "connector": "stripe", "tool": "create_refund",
                   "args": {"payment_intent": "pi_1", "amount_usd": 5}, "tag": "irreversible"}
        out = actions.gate(company, agent_of(company, "sales"), None, "connector_call", payload,
                           policy_action=connectors.policy_for(refund["tag"]))
        self.assertIn("Queued for the owner's approval", out)
        row = db.one("SELECT * FROM approvals WHERE company_id=? AND action='connector_call' ORDER BY id DESC",
                     (company["id"],))
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.fake.refunds, [])   # nothing ran
        # After the owner approves, the executor runs the tool against Stripe.
        self.fake.add_charge(amount=1900)
        done = self.call("create_refund", payment_intent="pi_1", amount_usd=5, approval_id=41)
        self.assertIn("Refunded $5.00 on pi_1", done)
        self.assertEqual(self.fake.refunds[0]["amount"], 500)
        self.assertTrue(self.fake.posts("/v1/refunds")[0]["idempotency_key"].startswith("holdco-approval-41-"))
        # the same approval again (a retry): the fake has no replay cache
        again = self.call("create_refund", payment_intent="pi_1", amount_usd=5, approval_id=41)
        self.assertNotIn(STRIPE_KEY, again)
        keys = [r["idempotency_key"] for r in self.fake.posts("/v1/refunds")]
        # A retried approval carries the same key, so real Stripe replays it. (Independent review: the key comes
        # from the approval now, so two approvals for the same amount are two refunds; tests/test_phase1_review.py.)
        self.assertEqual(keys[0], keys[1])
        set_settings(self.company, policies={})

    def test_metrics_from_charges_and_subscriptions(self):
        now = int(time.time())
        self.fake.add_charge(amount=1900, customer_email="a@dogs.example")
        self.fake.add_charge(amount=1000, amount_refunded=300)
        self.fake.add_charge(amount=500, currency="eur")
        self.fake.add_charge(amount=9900, created=now - 40 * 86400)   # outside the 30 days
        self.fake.add_charge(amount=4400, paid=False)                  # failed
        self.fake.add_customer(email="b@dogs.example")
        self.fake.add_subscription(unit_amount=12000, interval="year")              # $10.00 a month
        self.fake.add_subscription(unit_amount=1500, interval="month", quantity=2)  # $30.00
        self.fake.add_subscription(unit_amount=700, interval="week")                # $30.33
        self.fake.add_subscription(unit_amount=9999, interval="month", status="canceled")
        rows = {name: (value, note) for name, value, note in conn_stripe.metrics(self.inst, self.company)}
        self.assertEqual(set(rows), {"revenue_usd_30d", "payments_30d", "customers", "mrr_usd"})
        self.assertAlmostEqual(rows["revenue_usd_30d"][0], 26.0)   # 19 + (10 - 3); the EUR charge is in the note
        self.assertIn("5.00 EUR", rows["revenue_usd_30d"][1])
        self.assertEqual(rows["payments_30d"][0], 3)
        self.assertEqual(rows["customers"][0], 2)
        self.assertAlmostEqual(rows["mrr_usd"][0], 70.33, places=2)
        self.assertIn("test mode", rows["mrr_usd"][1])
        self.assertAlmostEqual(conn_stripe._monthly(36500, 1, "day", 1), 36500 * 365 / 12)
        self.assertAlmostEqual(conn_stripe._monthly(1000, 1, "month", 3), 1000 / 3)

    def test_mode_from_the_key(self):
        self.assertEqual(conn_stripe.mode_of_key("sk_test_abc"), "test")
        self.assertEqual(conn_stripe.mode_of_key("rk_test_abc"), "test")
        self.assertEqual(conn_stripe.mode_of_key("sk_live_abc"), "live")
        self.assertEqual(conn_stripe.mode_of_key("rk_live_abc"), "live")
        self.assertTrue(conn_stripe.test_mode(self.inst))
        live = make_inst("stripe", self.company, {"api_key": "rk_live_abc"})
        self.assertFalse(conn_stripe.DEFINITION["test_mode"](live))

    def test_health(self):
        self.assertEqual(conn_stripe.health(self.inst), ("ok", "test mode, balance available"))
        live = make_inst("stripe", self.company, {"api_key": "sk_live_0123456789"}, api_base=self.fake.url)
        self.assertEqual(conn_stripe.health(live), ("bad", "key rejected"))
        self.assertEqual(conn_stripe.health(make_inst("stripe", self.company, {}))[0], "bad")
        down = make_inst("stripe", self.company, {"api_key": STRIPE_KEY}, api_base=f"http://127.0.0.1:{free_port()}")
        status, detail = conn_stripe.health(down)
        self.assertEqual(status, "bad")
        self.assertIn("Cannot reach Stripe", detail)
        self.assertNotIn(STRIPE_KEY, detail)

    def test_bracket_encoding(self):
        q = urllib.parse.parse_qsl(conn_stripe.encode({"line_items": [{"price": "p", "quantity": 1}],
                                                       "created": {"gte": 5}, "expand": ["line_items"],
                                                       "flag": True}))
        self.assertEqual(q, [("line_items[0][price]", "p"), ("line_items[0][quantity]", "1"),
                             ("created[gte]", "5"), ("expand[]", "line_items"), ("flag", "true")])
        self.assertEqual(conn_stripe._scrub("bad key sk_test_abcDEF123 and rk_live_x9"), "bad key [key] and [key]")

    def test_mcp_classification_and_the_irreversible_write(self):
        c = conn_stripe.classify
        for name in ("stripe_api_search", "stripe_api_read", "get_stripe_account_info", "stripe_analytics",
                     "search_stripe_documentation", "stripe_implementation_planner", "stripe_api_details"):
            self.assertEqual(c(name), "read")
        # Independent review: stripe_api_write is irreversible unless its target is on the safe list, and a tool
        # the map does not name is irreversible too (it was publish and unclassified before).
        self.assertEqual(c("stripe_api_write"), "irreversible")
        self.assertEqual(c("send_money"), "spend")
        self.assertEqual(c("convert_currency"), "spend")
        self.assertEqual(c("setup_recipient"), "write")
        self.assertEqual(c("atlas_company_formation"), "irreversible")
        self.assertEqual(c("send_stripe_feedback"), "irreversible")
        self.assertEqual(c("brand_new_tool"), "irreversible")
        t = conn_stripe.tag_for_call
        self.assertEqual(t("stripe_api_write", {"method": "POST", "path": "/v1/refunds"}), "irreversible")
        self.assertEqual(t("stripe_api_write", {"method": "POST", "path": "/v1/subscriptions/sub_1/cancel"}),
                         "irreversible")
        self.assertEqual(t("stripe_api_write", {"method": "DELETE", "path": "/v1/products/prod_1"}), "irreversible")
        self.assertEqual(t("stripe_api_write", {"method": "POST", "path": "/v1/payouts"}), "spend")
        self.assertEqual(t("stripe_api_write", {"method": "POST", "path": "/v1/products"}), "publish")
        self.assertIsNone(t("stripe_api_read", {"path": "/v1/refunds"}))
        mcp_def = conn_stripe.MCP_DEFINITION
        self.assertEqual(mcp_def["kind"], "mcp-http")
        self.assertEqual(mcp_def["mcp"]["url"], "https://mcp.stripe.com")
        self.assertIs(mcp_def["tag_for_call"], conn_stripe.tag_for_call)

    def test_mcp_definition_follows_use_mcp(self):
        mcp_inst = make_inst("stripe_mcp", self.company, {})
        self.assertEqual(conn_stripe.mcp_health(mcp_inst)[0], "warn")   # use_mcp is off on the Stripe card
        self.inst["config"]["use_mcp"] = "yes"
        self.assertTrue(conn_stripe.mcp_active(mcp_inst))
        self.assertEqual(conn_stripe.mcp_headers(mcp_inst), {"Authorization": f"Bearer {STRIPE_KEY}"})
        client = FakeBrowserClient(tools_list=[{"name": "stripe_api_read"}, {"name": "stripe_api_write"}])
        with mock.patch.object(connectors, "client", lambda inst: client, create=True):
            self.assertEqual(conn_stripe.mcp_health(mcp_inst), ("ok", "2 Stripe MCP tools, test mode"))
            client.tools_list = mcp.AuthRequired("https://mcp.stripe.com/.well-known/oauth-protected-resource")
            status, detail = conn_stripe.mcp_health(mcp_inst)
        self.assertEqual(status, "bad")
        self.assertIn("Agent tag", detail)


# =============================================================================================== Resend

class TestResend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeResend(domains=("pawplan.example", ("pending.example", "pending"))).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.clear()
        FakeSMTP.sent = []
        self.company = make_company("MailCo", members=("ceo", "support", "sales"),
                                    settings={"smtp_host": "smtp.mailco.example", "smtp_user": "me@mailco.example",
                                              "smtp_password": "pw-not-real", "imap_user": "inbox@mailco.example"})
        self.inst = self.resend_inst()

    def resend_inst(self, status="ok", key=RESEND_KEY, **cfg):
        values = {"api_base": self.fake.url, "from_address": "hello@pawplan.example", "from_name": "PawPlan", **cfg}
        return make_inst("resend", self.company, {"api_key": key}, status=status, **values)

    def send(self, inst, provider=None, **kw):
        company = set_settings(self.company, email_provider=provider) if provider else db.get_company(
            self.company["id"])
        with patched({"resend": inst} if inst else {}), mock.patch.object(emailer.smtplib, "SMTP", FakeSMTP):
            return emailer.send(company, kw.pop("to", "lead@dogs.example"), kw.pop("subject", "Hello"),
                                kw.pop("body", "A short note."), **kw)

    def test_resend_provider_posts_to_resend_with_headers_and_idempotency(self):
        out = self.send(self.inst, provider="resend", in_reply_to="<orig@dogs.example>")
        self.assertEqual(out, "Email sent to lead@dogs.example (through Resend).")
        self.assertEqual(len(self.fake.sent), 1)
        sent = self.fake.sent[0]
        body = sent["body"]
        self.assertEqual(body["from"], "PawPlan <hello@pawplan.example>")
        self.assertEqual(body["to"], "lead@dogs.example")
        self.assertEqual(body["subject"], "Hello")
        self.assertEqual(body["text"], "A short note.")
        self.assertEqual(body["reply_to"], "inbox@mailco.example")   # replies come back to the IMAP mailbox
        self.assertTrue(body["headers"]["Message-ID"].endswith("@pawplan.example>"))
        self.assertEqual(body["headers"]["In-Reply-To"], "<orig@dogs.example>")
        self.assertEqual(body["headers"]["References"], "<orig@dogs.example>")
        self.assertTrue(sent["idempotency_key"].startswith("holdco-"))
        row = db.one("SELECT * FROM emails WHERE company_id=? AND direction='out'", (self.company["id"],))
        self.assertEqual(row["message_id"], body["headers"]["Message-ID"])   # Holdco's id, for reply matching
        self.assertEqual(row["uid"], sent["id"])                              # Resend's id
        self.assertEqual(row["from_addr"], "hello@pawplan.example")
        self.assertEqual(FakeSMTP.sent, [])

    def test_auto_uses_resend_only_when_healthy(self):
        self.send(self.inst)   # email_provider defaults to auto
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(FakeSMTP.sent, [])
        self.send(self.resend_inst(status="bad"), subject="Second")
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.send(None, subject="Third")   # no Resend connection at all
        self.assertEqual(len(FakeSMTP.sent), 2)
        self.send(self.inst, provider="smtp", subject="Fourth")   # SMTP chosen: Resend is ignored
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(len(FakeSMTP.sent), 3)

    def test_resend_chosen_but_not_connected_is_a_clear_error(self):
        with self.assertRaises(emailer.EmailError) as cm:
            self.send(None, provider="resend")
        self.assertIn("Resend isn't connected", str(cm.exception))
        with self.assertRaises(emailer.EmailError) as cm:
            self.send(self.resend_inst(from_address=""), provider="resend")
        self.assertIn("from address", str(cm.exception))
        with self.assertRaises(emailer.EmailError) as cm:
            self.send(self.resend_inst(key="re_x", api_base=f"http://127.0.0.1:{free_port()}"), provider="resend")
        self.assertIn("Cannot reach Resend", str(cm.exception))

    def test_an_approved_email_uses_the_approval_id_and_cannot_send_twice(self):
        company = set_settings(self.company, email_provider="resend")
        agent = agent_of(company, "sales")
        payload = {"to": "lead@dogs.example", "subject": "Quick question", "body": "Would this help?"}
        aid = db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, "
                     "created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (company["id"], agent["id"], None, "send_email", "Email", json.dumps(payload, sort_keys=True),
                      "pending", db.now()))
        with patched({"resend": self.inst}):
            row = actions.decide(aid, "approve")
            self.assertEqual(row["status"], "done")
            self.assertEqual(len(self.fake.sent), 1)
            key = self.fake.sent[0]["idempotency_key"]
            self.assertTrue(key.startswith(f"holdco-approval-{aid}-"))
            # The same approval running again (a retry after a lost reply) reuses the key: Resend sends nothing new.
            db.update("approvals", aid, status="running")
            emailer.send(db.get_company(company["id"]), payload["to"], payload["subject"], payload["body"])
        self.assertEqual(len(self.fake.sent), 1)
        ids = [r["uid"] for r in db.q("SELECT uid FROM emails WHERE company_id=? AND direction='out'",
                                      (company["id"],))]
        self.assertEqual(ids[0], ids[1])
        # Without an approval row the key is a stable hash of the message.
        db.update("approvals", aid, status="done")
        k1 = emailer._idempotency_key(company, "a@x.example", "S", "B")
        self.assertEqual(k1, emailer._idempotency_key(company, "a@x.example", "S", "B"))
        self.assertNotEqual(k1, emailer._idempotency_key(company, "a@x.example", "S", "B2"))

    def test_poll_inbox_fills_in_reply_to(self):
        company = set_settings(self.company, imap_host="imap.mailco.example", imap_password="pw-not-real")
        FakeIMAP.messages = [
            (b"From: Lead <lead@dogs.example>\r\nTo: inbox@mailco.example\r\nSubject: Re: Hello\r\n"
             b"Message-ID: <reply1@dogs.example>\r\nIn-Reply-To: <sent1@pawplan.example>\r\n\r\nYes please.\r\n"),
            (b"From: Other <other@dogs.example>\r\nTo: inbox@mailco.example\r\nSubject: Re: Hi\r\n"
             b"Message-ID: <reply2@dogs.example>\r\nReferences: <a@x.example> <sent2@pawplan.example>\r\n\r\n"
             b"Sure.\r\n"),
            (b"From: New <new@dogs.example>\r\nTo: inbox@mailco.example\r\nSubject: A question\r\n"
             b"Message-ID: <new@dogs.example>\r\n\r\nHello?\r\n"),
        ]
        with mock.patch.object(emailer.imaplib, "IMAP4_SSL", FakeIMAP):
            self.assertEqual(emailer.poll_inbox(company), 3)
        rows = db.q("SELECT message_id, in_reply_to FROM emails WHERE company_id=? AND direction='in' ORDER BY id",
                    (company["id"],))
        self.assertEqual([r["in_reply_to"] for r in rows],
                         ["<sent1@pawplan.example>", "<sent2@pawplan.example>", ""])

    def test_sent_and_replies_metrics(self):
        cid = self.company["id"]
        old = (email.utils.parsedate_to_datetime(email.utils.formatdate(time.time() - 10 * 86400, usegmt=True))
               .strftime("%Y-%m-%dT%H:%M:%SZ"))

        def add(direction, frm, to, subject, message_id="", in_reply_to="", created=None):
            db.run("INSERT INTO emails(company_id, direction, message_id, from_addr, to_addr, subject, body, status,"
                   " created_at, in_reply_to) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (cid, direction, message_id, frm, to, subject, "", "sent", created or db.now(), in_reply_to))

        add("out", "hello@pawplan.example", "a@dogs.example", "Quick question", "<m1@pawplan.example>")
        add("out", "hello@pawplan.example", "b@dogs.example", "Meal plans", "<m2@pawplan.example>")
        add("out", "hello@pawplan.example", "c@dogs.example", "Older", "<m3@pawplan.example>", created=old)
        add("in", "Someone <z@dogs.example>", "inbox@mailco.example", "Anything", in_reply_to="<m1@pawplan.example>")
        add("in", "Bee <b@dogs.example>", "inbox@mailco.example", "RE: Fwd: meal  plans")   # subject fallback
        add("in", "Cee <c@dogs.example>", "inbox@mailco.example", "Unrelated")
        add("in", "Dee <a@dogs.example>", "inbox@mailco.example", "Re: Quick question", created=old)   # too old
        rows = {n: (v, note) for n, v, note in conn_resend.metrics(self.inst, self.company)}
        self.assertEqual(rows["sent_7d"][0], 2)
        self.assertEqual(rows["replies_7d"][0], 2)
        self.assertIn("1 by In-Reply-To, 1 by subject", rows["replies_7d"][1])
        self.assertEqual(conn_resend.normal_subject("Re: RE: Fwd:  Hello  There"), "hello there")

    def test_health(self):
        with patched():
            self.assertEqual(conn_resend.health(self.inst), ("ok", "pawplan.example verified"))
            status, detail = conn_resend.health(self.resend_inst(from_address="hi@pending.example"))
            self.assertEqual(status, "warn")
            self.assertIn("pending", detail)
            self.assertEqual(conn_resend.health(self.resend_inst(from_address="hi@other.example"))[0], "warn")
            self.assertEqual(conn_resend.health(self.resend_inst(from_address=""))[0], "warn")
            self.assertEqual(conn_resend.health(self.resend_inst(key="bad-key")), ("bad", "key rejected"))
            self.assertEqual(conn_resend.health(self.resend_inst(key="re_send_only_123"))[0], "ok")
            down = self.resend_inst(api_base=f"http://127.0.0.1:{free_port()}")
            self.assertEqual(conn_resend.health(down)[0], "bad")
            self.assertEqual(conn_resend.tools(self.inst), [])


# =============================================================================================== Buffer

class TestBuffer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeBuffer().start()
        cls.hook = Recorder()
        cls.company = make_company("PostCo", members=("ceo", "social"))

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()
        cls.hook.stop()

    def setUp(self):
        self.fake.clear()
        self.hook.bodies.clear()
        self.inst = make_inst("buffer", self.company, {"api_key": self.fake.token}, api_base=self.fake.url)

    def call(self, _tool, **args):
        return spec(conn_buffer.DEFINITION, self.inst, _tool)["fn"](self.inst, self.company, **args)

    def test_list_channels_and_create_post(self):
        with patched({"buffer": self.inst}):
            listed = self.call("list_channels")
            self.assertIn("chan_x: x PawPlan on X", listed)
            self.assertIn("chan_li: linkedin PawPlan on LinkedIn", listed)
            out = self.call("create_post", text="Meal plans are live", channel_ids="chan_li",
                            link="https://pawplan.example")
            self.assertIn("added to the queue", out)
            post = self.fake.posts[-1]["input"]
            self.assertEqual(post["channelId"], "chan_li")
            self.assertEqual(post["text"], "Meal plans are live\n\nhttps://pawplan.example")
            self.assertEqual(post["mode"], "addToQueue")
            self.inst["config"]["default_channels"] = "chan_x"
            self.call("create_post", text="Scheduled", schedule="2026-10-20T15:00:00Z")
            post = self.fake.posts[-1]["input"]
            self.assertEqual((post["channelId"], post["mode"], post["dueAt"]),
                             ("chan_x", "customScheduled", "2026-10-20T15:00:00Z"))
            self.inst["config"]["default_channels"] = ""
            with self.assertRaises(ValueError):
                self.call("create_post", text="Nowhere")
            with self.assertRaises(ValueError):
                self.call("create_post", text="Bad time", channel_ids="chan_x", schedule="tomorrow")
        tags = {t["name"]: t["tag"] for t in conn_buffer.tools(self.inst)}
        self.assertEqual(tags, {"list_channels": "read", "create_post": "publish"})

    def test_social_post_goes_through_buffer_when_configured(self):
        company = set_settings(self.company, webhook_url=self.hook.url)
        # Independent review: only the card's default channels are used (none set falls back to the webhook)
        self.inst["config"]["default_channels"] = "chan_x, chan_li"
        with patched({"buffer": self.inst}):
            out = actions.execute(company, "social_post", {"platform": "linkedin", "content": "Hello dog owners",
                                                           "link": ""})
        self.assertIn("Buffer", out)
        self.assertEqual(self.fake.posts[-1]["input"]["channelId"], "chan_li")
        self.assertEqual(self.hook.bodies, [])
        with patched({"buffer": self.inst}):   # "twitter" matches the x channel
            actions.execute(company, "social_post", {"platform": "twitter", "content": "Hi", "link": ""})
        self.assertEqual(self.fake.posts[-1]["input"]["channelId"], "chan_x")

    def test_social_post_falls_back_to_the_webhook(self):
        company = set_settings(self.company, webhook_url=self.hook.url)
        with patched({"buffer": self.inst}):   # no reddit channel in Buffer
            out = actions.execute(company, "social_post", {"platform": "reddit", "content": "Hi", "link": ""})
        self.assertIn("webhook", out)
        self.assertEqual(self.hook.bodies[-1]["platform"], "reddit")
        unhealthy = dict(self.inst, status="bad")
        with patched({"buffer": unhealthy}):
            actions.execute(company, "social_post", {"platform": "linkedin", "content": "Hi", "link": ""})
        with patched({}):
            actions.execute(company, "social_post", {"platform": "linkedin", "content": "Hi", "link": ""})
        self.assertEqual(len(self.hook.bodies), 3)
        self.assertEqual(self.fake.posts, [])
        set_settings(self.company, webhook_url="")

    def test_health(self):
        with patched():
            status, detail = conn_buffer.health(self.inst)
            self.assertEqual(status, "ok")
            self.assertIn("owner@pawplan.example", detail)
            wrong = make_inst("buffer", self.company, {"api_key": "nope"}, api_base=self.fake.url)
            self.assertEqual(conn_buffer.health(wrong), ("bad", "token rejected"))
            self.assertEqual(conn_buffer.health(make_inst("buffer", self.company, {}))[0], "bad")


# =============================================================================================== Playwright

class TestPlaywright(unittest.TestCase):
    def setUp(self):
        self.inst = make_inst("playwright", None, {})

    def test_health_warns_without_npx(self):
        with mock.patch.object(conn_playwright.shutil, "which", return_value=None):
            self.assertEqual(conn_playwright.health(self.inst), ("warn", "npx not found: install Node.js"))

    def test_health_ok_or_bad_with_the_stderr_tail(self):
        with mock.patch.object(conn_playwright.shutil, "which", return_value="/usr/bin/npx"):
            client = FakeBrowserClient()
            with patched(client=client):
                self.assertEqual(conn_playwright.health(self.inst)[0], "ok")
                client.tools_list = [{"name": "something_else"}]
                self.assertEqual(conn_playwright.health(self.inst)[0], "bad")
                client.tools_list = mcp.MCPError("the server exited: npm ERR! 404 Not Found @playwright/mcp")
                status, detail = conn_playwright.health(self.inst)
        self.assertEqual(status, "bad")
        self.assertIn("npm ERR! 404", detail)

    def test_command_and_override(self):
        self.assertEqual(conn_playwright.launch(self.inst),
                         ("npx", ["-y", "@playwright/mcp@0.0.83", "--headless", "--isolated", "--browser",
                                  "chromium"]))   # pinned, not @latest (independent review)
        d = conn_playwright.DEFINITION["mcp"]
        self.assertEqual((d["command"], d["args"]), conn_playwright.launch(self.inst))
        custom = make_inst("playwright", None, {}, command="node /opt/pw/cli.js --headless --isolated")
        self.assertEqual(conn_playwright.launch(custom), ("node", ["/opt/pw/cli.js", "--headless", "--isolated"]))

    def test_classification_map(self):
        c = conn_playwright.classify
        for name in ("browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_take_screenshot",
                     "browser_console_messages", "browser_network_requests", "browser_resize", "browser_wait_for",
                     "browser_find", "browser_tabs", "browser_close"):
            self.assertEqual(c(name), "read", name)
        for name in ("browser_click", "browser_type", "browser_fill_form", "browser_press_key",
                     "browser_select_option", "browser_hover", "browser_drag", "browser_handle_dialog"):
            self.assertEqual(c(name), "contact", name)
        self.assertEqual(c("browser_file_upload"), "publish")
        self.assertIsNone(c("browser_install"))   # unclassified: asks
        d = conn_playwright.DEFINITION["mcp"]
        self.assertEqual(d["blocked"], ["browser_evaluate", "browser_run_code_unsafe"])
        self.assertEqual(d["allow"]["*"], conn_playwright.READ_TOOLS)
        for role in ("qa", "engineering", "research", "design", "marketing"):
            self.assertEqual(d["allow"][role], ["*"])
        for name in d["blocked"]:
            self.assertIsNone(c(name))

    def test_blocked_tools_are_refused_for_every_role(self):
        for name in ("browser_evaluate", "browser_run_code_unsafe"):
            self.assertEqual(conn_playwright.refuse_call(name, {"function": "() => 1"}),
                             f"{name} is not available through Holdco")
        self.assertIsNone(conn_playwright.refuse_call("browser_snapshot", {}))

    def test_file_upload_queues_under_connector_publish(self):
        company = make_company("UploadCo", members=("ceo", "engineering"))
        tag = conn_playwright.classify("browser_file_upload")
        self.assertEqual(connectors.policy_for(tag), "connector_publish")
        payload = {"connector_id": self.inst["id"], "connector": "playwright", "tool": "browser_file_upload",
                   "args": {"paths": ["/tmp/x.pdf"]}, "tag": tag}
        out = actions.gate(company, agent_of(company, "engineering"), None, "connector_call", payload,
                           policy_action=connectors.policy_for(tag))
        self.assertIn("Queued for the owner's approval", out)

    def test_navigate_to_this_pc_is_refused(self):
        refuse = conn_playwright.refuse_call
        with mock.patch.object(web, "ALLOW_LOCAL", ["127.0.0.1:*"]):   # the test exemption does not apply here
            for url in ("http://127.0.0.1:8420/preview/pawplan/index.html", "http://localhost:8420/",
                        "http://[::1]/", "http://0.0.0.0:8420/", "http://app.localhost/"):
                self.assertIn("use browser_check for your own site", refuse("browser_navigate", {"url": url}), url)
        self.assertIn("private network", refuse("browser_navigate", {"url": "http://192.168.1.10/admin"}))
        self.assertIn("Only http(s)", refuse("browser_navigate", {"url": "file:///etc/passwd"}))
        self.assertIn("Only http(s)", refuse("browser_navigate", {"url": "javascript:alert(1)"}))
        self.assertIsNone(refuse("browser_navigate", {"url": "https://93.184.216.34/"}))


# =============================================================================================== browser_check

INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PawPlan: weekly meal plans for dogs</title>
</head><body><h1>Meal plans your dog will eat</h1>
<a href="about.html">About us</a> <a href="/about.html">About again</a> <a href="missing.html">Broken page</a>
<a href="#top">Top</a> <a href="mailto:hi@pawplan.example">Email</a> <a href="https://example.com/">Elsewhere</a>
<form name="waitlist" data-netlify="true"><label>Email <input name="email" type="email"></label>
<button type="submit">Join the waitlist</button></form>
<form><label>Name <input name="name"></label><button type="submit">Send it</button></form>
</body></html>"""


class PreviewServer:
    """Serves /preview/<slug>/<path> from a company's site/ folder, like the dashboard does."""

    def __init__(self, company: dict):
        site = workspace.safe_path(company, "site")
        prefix = f"/preview/{company['slug']}/"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = urllib.parse.unquote(self.path.split("?")[0])
                target = site / (path[len(prefix):] or "index.html") if path.startswith(prefix) else None
                if target is None or not target.is_file():
                    body, code = b"not found", 404
                else:
                    body, code = target.read_bytes(), 200
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestBrowserCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stripe = FakeStripe().start()

    @classmethod
    def tearDownClass(cls):
        cls.stripe.stop()

    def setUp(self):
        self.company = make_company("CheckCo", members=("ceo", "qa", "engineering"))
        site = workspace.safe_path(self.company, "site")
        (site / "index.html").write_text(INDEX, encoding="utf-8")
        (site / "about.html").write_text("<!doctype html><title>About</title><h1>About</h1>", encoding="utf-8")
        self.ctx = ctx_for(self.company, "qa")
        self.port = free_port()   # nothing listens here: the preview is unreachable

    def check(self, target, instances=None, client=None, port=None, mobile=True):
        with patched(instances or {}, client), mock.patch.object(config, "PORT", port or self.port):
            return browser.browser_check(self.ctx, target, mobile)

    def test_static_fallback_without_a_server(self):
        out = self.check("site/index.html")
        lines = out.splitlines()
        self.assertEqual(lines[0], "Static check only: Playwright is not set up")
        self.assertIn(f"GET http://127.0.0.1:{self.port}/preview/{self.company['slug']}/index.html -> unreachable",
                      lines[1])
        self.assertIn("read site/index.html from disk", lines[1])
        self.assertIn("Title: PawPlan: weekly meal plans for dogs", out)
        self.assertIn("H1: Meal plans your dog will eat", out)
        self.assertIn("Links: 2 checked, 1 ok (1 external not checked", out)   # about.html twice counts once
        self.assertIn("- broken: missing.html -> missing file", out)
        self.assertIn("Forms: 2 found, 1 with a target (data-netlify)", out)
        self.assertIn("- form 2 has no action or data-netlify attribute", out)
        self.assertRegex(out, r"Audit: \d+ blocker\(s\), \d+ major, \d+ minor \(details below\)")
        self.assertIn("Broken local link or file: missing.html", out)   # the audit's own line
        self.assertFalse((workspace.root(self.company) / ".qa").exists())
        self.assertEqual(browser.browser_check(self.ctx, "index.html")[:17], "Static check only")   # site/ implied

    def test_static_with_the_preview_running(self):
        server = PreviewServer(self.company)
        try:
            out = self.check("site/index.html", port=server.port)
        finally:
            server.stop()
        self.assertIn(f"GET http://127.0.0.1:{server.port}/preview/{self.company['slug']}/index.html -> 200", out)
        self.assertIn("Links: 2 checked, 1 ok", out)
        self.assertIn("- broken: missing.html -> 404", out)
        self.assertEqual(browser._preview_url(self.company, "blog/a b.html"),
                         f"http://127.0.0.1:{config.PORT}/preview/{self.company['slug']}/blog/a%20b.html")

    def test_with_playwright(self):
        inst = make_inst("playwright", None, {})
        client = FakeBrowserClient(
            console="[LOG] ready\n[ERROR] Uncaught TypeError: plan is undefined @ app.js:3",
            snapshot=('- Page URL: x\n- Page Title: PawPlan rendered\n- Page Snapshot:\n```yaml\n'
                      '- heading "Meal plans your dog will eat" [level=1] [ref=e2]\n'
                      '- link "About us" [ref=e3]:\n  - /url: about.html\n'
                      '- link "Pricing" [ref=e4]:\n  - /url: pricing.html\n```'))
        out = self.check("site/index.html", {"playwright": inst}, client)
        self.assertTrue(out.startswith("Browser check (Playwright) of site/index.html"), out)
        self.assertIn("Title: PawPlan rendered", out)
        self.assertIn("H1: Meal plans your dog will eat", out)
        self.assertIn("Console errors: 1", out)
        self.assertIn("Uncaught TypeError: plan is undefined", out)
        self.assertIn("- broken: missing.html -> missing file", out)
        self.assertIn("- broken: pricing.html -> missing file", out)   # a link only the rendered page had
        self.assertIn("Forms: 2 found, 1 with a target (data-netlify)", out)
        self.assertIn("Screenshots: .qa/index.png, .qa/index-mobile.png", out)
        qa = workspace.root(self.company) / ".qa"
        self.assertEqual((qa / "index.png").read_bytes(), PNG)
        self.assertEqual((qa / "index-mobile.png").read_bytes(), PNG)
        names = [c[0] for c in client.calls]
        self.assertLess(names.index("browser_navigate"), names.index("browser_console_messages"))
        self.assertIn(("browser_resize", {"width": 390, "height": 844}), client.calls)
        self.assertEqual(client.calls[names.index("browser_navigate")][1]["url"],
                         f"http://127.0.0.1:{self.port}/preview/{self.company['slug']}/index.html")
        self.assertFalse(any(n in ("browser_click", "browser_type", "browser_evaluate") for n in names))
        no_phone = self.check("site/about.html", {"playwright": inst}, FakeBrowserClient(), mobile=False)
        self.assertIn("Screenshots: .qa/about.png", no_phone)
        self.assertNotIn("about-mobile", no_phone)

    def test_playwright_problems_fall_back_to_the_labelled_static_check(self):
        inst = make_inst("playwright", None, {})
        out = self.check("site/index.html", {"playwright": inst}, FakeBrowserClient(fail_on="browser_snapshot"))
        self.assertEqual(out.splitlines()[0], "Static check only: Playwright failed: browser crashed")
        self.assertIn("Audit:", out)
        down = make_inst("playwright", None, {}, status="warn", detail="npx not found: install Node.js")
        out = self.check("site/index.html", {"playwright": down}, FakeBrowserClient())
        self.assertEqual(out.splitlines()[0], "Static check only: Playwright failed: npx not found: install Node.js")
        # A server without browser_resize still renders: only the phone-sized screenshot is missing.
        partial = self.check("site/index.html", {"playwright": inst}, FakeBrowserClient(fail_on="browser_resize"))
        self.assertTrue(partial.startswith("Browser check (Playwright)"), partial)
        self.assertIn("Screenshots: .qa/index.png, no phone-sized screenshot (browser crashed)", partial)
        off = make_inst("playwright", None, {}, enabled=0)
        self.assertEqual(self.check("site/index.html", {"playwright": off}).splitlines()[0],
                         "Static check only: Playwright is not set up")

    def test_external_url_through_the_fake_stripe_checkout(self):
        inst = make_inst("stripe", self.company, {"api_key": STRIPE_KEY}, api_base=self.stripe.url)
        with patched({"stripe": inst}):
            made = conn_stripe.create_payment_link(inst, self.company, "Starter plan", 19,
                                                   recurring_interval="month")
        url = made.split()[3]
        self.assertTrue(url.startswith(f"{self.stripe.url}/pay/plink_"))
        with mock.patch.object(web, "ALLOW_LOCAL", ["127.0.0.1:*"]):
            out = self.check(url)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Static check only: Playwright is not set up")
        self.assertEqual(lines[1], f"GET {url} -> 200")
        self.assertIn("Title: Starter plan: checkout (test mode)", out)
        self.assertIn("form found", out)
        self.assertIn("Text: Starter plan", out)
        self.assertIn("Nothing was clicked or submitted", out)
        # Without the test exemption the same address is this PC, which agents may not reach.
        with mock.patch.object(web, "ALLOW_LOCAL", []), patched({}):
            refused = tools.execute(self.ctx, "browser_check", {"path_or_url": url})
        self.assertTrue(refused.startswith("ERROR: Local and private network addresses are blocked"), refused)

    def test_paths_outside_site_are_refused(self):
        with patched({}):
            self.assertIn("is not in site/", tools.execute(self.ctx, "browser_check", {"path_or_url": "docs/x.md"}))
            self.assertIn("outside the company folder",
                          tools.execute(self.ctx, "browser_check", {"path_or_url": "../../etc/passwd"}))
            self.assertIn("doesn't exist", tools.execute(self.ctx, "browser_check", {"path_or_url": "site/no.html"}))
            self.assertIn("Only http(s)", tools.execute(self.ctx, "browser_check", {"path_or_url": "ftp://x.example"}))
        self.assertEqual(tools.risk_of("browser_check"), "read")

    def test_own_preview_url_is_checked_as_a_site_page(self):
        url = f"http://127.0.0.1:{self.port}/preview/{self.company['slug']}/about.html"
        out = self.check(url)
        self.assertIn(f"GET {url} -> unreachable", out)
        self.assertIn("read site/about.html from disk", out)


# =============================================================================================== Brave

class TestBrave(unittest.TestCase):
    def tearDown(self):
        web.reset_status()

    def test_health_runs_one_search(self):
        inst = make_inst("brave", None, {})
        with patched(), mock.patch.object(db, "get_settings", return_value={"brave_api_key": ""}):
            self.assertEqual(conn_brave.health(inst)[0], "warn")
        answer = json.dumps({"web": {"results": [{"title": "T", "url": "https://x.example", "description": "d"}]}})
        with mock.patch.object(db, "get_settings", return_value={"brave_api_key": "BSAtestkey"}), \
                mock.patch.object(web, "_http", return_value=(200, answer)) as http:
            self.assertEqual(conn_brave.health(inst), ("ok", "Brave answered with 1 result(s)"))
            self.assertEqual(http.call_args.kwargs["headers"]["X-Subscription-Token"], "BSAtestkey")
        self.assertTrue(web.PROVIDER_STATUS["brave"]["ok"])
        with mock.patch.object(db, "get_settings", return_value={"brave_api_key": "BSAtestkey"}), \
                mock.patch.object(web, "_http", return_value=(401, "{}")):
            status, detail = conn_brave.health(inst)
        self.assertEqual(status, "bad")
        self.assertIn("rejected", detail)
        self.assertNotIn("BSAtestkey", detail)


if __name__ == "__main__":
    unittest.main()
