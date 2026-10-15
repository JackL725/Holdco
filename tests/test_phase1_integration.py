"""Phase 1 integration: the parts built separately (vault, MCP client and OAuth, connector framework, the five
connectors and browser_check) joined together, with no stand-ins between them.

The real definitions (conn_stripe, conn_resend, conn_buffer, conn_playwright, conn_brave) run in the real framework
(connectors.py) with the real file vault, against the fakes on 127.0.0.1 (tests/fake_stripe.py, fake_resend.py,
fake_buffer.py, fake_mcp_server.py, the stdio one started with sys.executable). Instances are created through the
dashboard's route functions. Nothing touches the network or a real keyring: Brave's one search is patched and
HOLDCO_VAULT_BACKEND is file.

Also here: the vault findings (no resolved secret is written back to companies.settings, the failing-key notice
prints once, deleting a company deletes its secrets and connectors).

Run with:  python -m unittest tests.test_phase1_integration -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import re
import secrets
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-integration-"))
os.environ["HOLDCO_VAULT_BACKEND"] = "file"   # never touch a developer's keyring
os.environ.pop("HOLDCO_VAULT_PASSPHRASE", None)

from holdco import (actions, bus, config, conn_playwright, connectors, context, db, emailer, notify,  # noqa: E402
                    oauth, planning, roles, routing, server, tools, truth, vault, web, workspace)
from holdco.routing import HTTPError  # noqa: E402
from tests.fake_buffer import FakeBuffer  # noqa: E402
from tests.fake_mcp_server import FakeMCPHttp  # noqa: E402
from tests.fake_oauth import FakeOAuth  # noqa: E402
from tests.fake_resend import FakeResend  # noqa: E402
from tests.fake_stripe import FakeStripe  # noqa: E402

db.init()
assert server   # imported for its routes (/api/companies/..., /api/settings)

MASK = "•" * 8
FAKE_MCP = str(ROOT / "tests" / "fake_mcp_server.py")


def setUpModule():
    if vault.backend() != "file":   # an earlier module chose another backend before this one set the variable
        vault._reset()


def key(prefix: str) -> str:
    """A secret no other test (or real key) will ever contain."""
    return f"{prefix}{secrets.token_hex(12)}"


def make_company(name: str, members=("ceo", "engineering", "marketing"), settings=None) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "An idea", "active", "building", json.dumps(settings or {}), db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def agent_of(company: dict, role: str) -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def ctx_for(company: dict, role: str) -> tools.Ctx:
    agent = agent_of(company, role)
    c = tools.Ctx(company=db.get_company(company["id"]), agent=agent, kind="task", run_id=0, task=None,
                  settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(agent, "task")}
    return c


class _Handler:
    def __init__(self, query=None):
        self.query = {k: [str(v)] for k, v in (query or {}).items()}


def call(method: str, path: str, body=None, query: dict | None = None):
    """Dispatch like server.Handler does, so the route patterns are exercised too."""
    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(_Handler(query), m, body)
    raise AssertionError(f"no route for {method} {path}")


def stored_settings(company: dict) -> dict:
    """companies.settings as stored (markers, not secrets)."""
    return json.loads(db.scalar("SELECT settings FROM companies WHERE id=?", (company["id"],)) or "{}")


class Base(unittest.TestCase):
    """Fakes started per test, every connector instance a test creates removed afterwards (global ones would
    otherwise reach the companies of later test modules), notify.send silenced."""

    def setUp(self):
        self.alerts: list = []
        p = mock.patch.object(notify, "send", side_effect=lambda text, *a, **kw: self.alerts.append(text))
        p.start()
        self.addCleanup(p.stop)
        # The test switch: only with it may a card's hidden api_base or mcp_url point at the fakes.
        env = mock.patch.dict(os.environ, {"HOLDCO_ALLOW_LOCAL": "127.0.0.1:*"})
        env.start()
        self.addCleanup(env.stop)
        db.set_settings({"kill_switch": False})
        self.before = {r["id"] for r in db.q("SELECT id FROM connectors")}
        self.addCleanup(self._remove_new_instances)

    def _remove_new_instances(self):
        for r in db.q("SELECT id FROM connectors"):
            if r["id"] not in self.before:
                with contextlib.redirect_stdout(io.StringIO()):
                    connectors.remove(r["id"])
        connectors._alerted.clear()

    def fake(self, cls, **kw):
        f = cls(**kw).start()
        self.addCleanup(f.stop)
        return f

    def save(self, key_: str, company: dict | None, config_values: dict, **extra) -> dict:
        body = {"key": key_, "company_id": company["id"] if company else None, "config": config_values, **extra}
        return call("POST", "/api/connectors", body)


# ================================================================ Stripe: card, find_tool, gate, audit, metrics

class TestStripe(Base):
    def test_card_find_tool_gate_approve_audit_metrics_and_browser_check(self):
        stripe = self.fake(FakeStripe)
        c = make_company("StripeCo")
        sk = key("sk_test_int")
        out = self.save("stripe", c, {"api_key": sk, "api_base": stripe.url, "use_mcp": "no"}, enabled=True)
        self.assertEqual(out["health"], {"status": "ok", "detail": "test mode, balance available"})
        inst = out["instance"]
        iid = inst["id"]
        self.assertEqual(inst["config"], {"api_key": MASK, "api_base": stripe.url, "use_mcp": False})   # bool field
        self.assertEqual(inst["mode"], "test")
        self.assertEqual(vault.get(f"connector.{iid}.api_key"), sk)
        self.assertNotIn(sk, db.scalar("SELECT config FROM connectors WHERE id=?", (iid,)))
        row = connectors.instance("stripe", c)   # what browser_check, _social and emailer read
        self.assertEqual((row["status"], row["detail"], row["enabled"]), ("ok", "test mode, balance available", 1))

        # The setup cards: both Stripe definitions, hidden fields flagged, Buffer's beta note passed through.
        cards = call("GET", "/api/connectors", query={"company_id": c["id"]})["definitions"]
        self.assertTrue({"stripe", "stripe_mcp", "resend", "playwright", "buffer", "brave"} <= set(cards), cards)
        fields = {f["key"]: f for f in cards["stripe"]["setup"]["fields"]}
        self.assertTrue(fields["api_base"]["hidden"] and not fields["api_key"]["hidden"])
        self.assertEqual(fields["use_mcp"]["type"], "bool")
        self.assertEqual(cards["stripe"]["hidden_fields"], ["api_base"])
        self.assertEqual(cards["stripe_mcp"]["hidden_fields"], ["mcp_url"])
        self.assertEqual((cards["stripe_mcp"]["parent"], cards["stripe_mcp"]["parent_field"]), ("stripe", "use_mcp"))
        self.assertTrue(cards["buffer"]["beta"])
        self.assertFalse(cards["buffer"]["enabled_by_default"])
        self.assertIn("Beta", cards["buffer"]["note"])
        tags = {t["tool"]: t["tag"] for t in cards["stripe"]["tools"]}
        self.assertEqual((tags["create_payment_link"], tags["create_refund"]), ("publish", "irreversible"))
        self.assertIn("stripe (test mode)", connectors.prompt_section(c))
        self.assertIn("Stripe (payments, test mode)", [a["account"] for a in connectors.connected(c)])

        # find_tool loads the typed tool; calling it goes through the gate under connector_publish.
        ctx = ctx_for(c, "engineering")
        found = tools.execute(ctx, "find_tool", {"query": "stripe payment link"})
        self.assertTrue(found.splitlines()[0].startswith("stripe__create_payment_link [publish]"), found)
        self.assertIn("stripe__create_payment_link", ctx.loaded_by_name)
        # an agent pasted the key into the description: the audit row must not keep it
        queued = tools.execute(ctx, "stripe__create_payment_link", {
            "name": "Starter plan", "amount_usd": "19", "recurring_interval": "month",
            "description": f"Starter plan, internal ref {sk}"})
        self.assertIn("Queued for the owner's approval", queued)
        self.assertEqual(stripe.posts(), [])   # nothing reached Stripe before the owner said yes
        appr = db.one("SELECT * FROM approvals WHERE company_id=? AND action='connector_call' AND status='pending'",
                      (c["id"],))
        payload = json.loads(appr["payload"])
        self.assertEqual((payload["connector"], payload["tool"], payload["tag"], payload["connector_id"]),
                         ("stripe", "create_payment_link", "publish", iid))
        self.assertEqual(payload["args"]["amount_usd"], 19.0)   # clean_args coerced the number
        done = actions.decide(appr["id"], "approve")
        self.assertEqual(done["status"], "done", done["result"])
        link = f"{stripe.url}/pay/plink_1"
        self.assertTrue(done["result"].startswith(f"Created payment link {link} (id plink_1, Starter plan, "
                                                  f"$19.00/month, test mode)"), done["result"])
        self.assertEqual([r["path"] for r in stripe.posts()], ["/v1/products", "/v1/prices", "/v1/payment_links"])
        price = stripe.posts("/v1/prices")[0]["body"]
        self.assertEqual((price["unit_amount"], price["recurring"]), ("1900", {"interval": "month"}))
        audit = db.one("SELECT * FROM audit WHERE approval_id=?", (appr["id"],))
        self.assertEqual((audit["action"], audit["approved_by"]), ("connector_call", "owner"))
        self.assertIn(link, audit["result"])
        self.assertIn(vault.REDACTED, audit["payload"])
        self.assertNotIn(sk, audit["payload"] + audit["result"])

        # A read tool runs at once and is an event, not an approval.
        tools.execute(ctx, "find_tool", {"query": "get payment link"})
        got = tools.execute(ctx, "stripe__get_payment_link", {"id": "plink_1"})
        self.assertIn("active: true", got)
        self.assertIn("item: Starter plan, $19.00/month", got)
        ev = db.one("SELECT * FROM events WHERE company_id=? AND kind='tool' ORDER BY id DESC LIMIT 1", (c["id"],))
        self.assertEqual(ev["message"], "called stripe get_payment_link")

        # browser_check opens the link: no Playwright, so the static check of an external page.
        with mock.patch.object(web, "ALLOW_LOCAL", ["127.0.0.1:*"]):
            check = tools.execute(ctx, "browser_check", {"path_or_url": link})
        self.assertTrue(check.startswith("Static check only: Playwright is not set up"), check)
        self.assertIn(f"GET {link} -> 200", check)
        self.assertIn("Title: Starter plan", check)
        self.assertIn("Form: form found", check)

        # A test purchase becomes revenue through the metric pipe, source stripe.
        stripe.add_charge(amount=1900, currency="usd", paid=True)
        out = call("POST", f"/api/connectors/{iid}/test", {})
        self.assertEqual(out["health"]["status"], "ok")
        polled = {m["name"]: m["value"] for m in out["metrics"]}
        self.assertEqual(polled["stripe.revenue_usd_30d"], 19.0)
        self.assertEqual((polled["stripe.payments_30d"], polled["stripe.mrr_usd"]), (1.0, 0.0))
        rows = db.q("SELECT * FROM metrics WHERE company_id=? AND name LIKE 'stripe.%'", (c["id"],))
        self.assertTrue(rows and all(r["source"] == "stripe" and r["agent_id"] is None for r in rows))
        card = next(i for i in call("GET", "/api/connectors", query={"company_id": c["id"]})["instances"]
                    if i["id"] == iid)
        self.assertIn("stripe.revenue_usd_30d", {m["name"] for m in card["metrics"]})
        self.assertNotIn(sk, json.dumps(card))
        # The facts block lists the account and shows the polled revenue, labelled test mode: a test purchase is
        # not a real signal (independent review; a live key counts, tests/test_phase1_review.py).
        facts = truth.facts_section(db.get_company(c["id"]))
        self.assertIn("revenue 19 (stripe.revenue_usd_30d, from stripe) (test mode)", facts)
        self.assertIn("Signups, revenue, replies: none recorded from a real source.", facts)
        self.assertIn("Stripe (payments, test mode)", facts)

    def test_stripe_mcp_stays_off_until_use_mcp(self):
        stripe = self.fake(FakeStripe)
        hosted = self.fake(FakeMCPHttp)   # stands in for https://mcp.stripe.com (accepts any bearer)
        c = make_company("StripeMcpCo")
        sk = key("rk_test_int")
        parent = self.save("stripe", c, {"api_key": sk, "api_base": stripe.url})["instance"]
        self.assertFalse(parent["config"].get("use_mcp", False))
        out = self.save("stripe_mcp", c, {"mcp_url": hosted.mcp_url})
        child = out["instance"]
        self.assertEqual(out["health"]["status"], "warn")
        self.assertIn("Off: turn on \"Also use Stripe's hosted MCP server\" on the Stripe card", out["health"]["detail"])
        self.assertEqual(child["config"], {"mcp_url": hosted.mcp_url, "api_key": ""})
        self.assertEqual([i["key"] for i in connectors.visible(c)], ["stripe"])
        self.assertNotIn(f"connector:{child['id']}", {r["key"] for r in connectors.doctor_rows()})
        ctx = ctx_for(c, "engineering")
        self.assertIn("No connected tool matches 'echo'", tools.execute(ctx, "find_tool", {"query": "echo"}))
        self.assertEqual(hosted.requests, [])   # nothing was sent to the hosted server while off

        # Ticking use_mcp on the Stripe card turns the MCP part on: it is checked again and works with the key.
        self.save("stripe", None, {"use_mcp": True}, id=parent["id"])
        self.assertIsNone(connectors.get(child["id"])["last_check"])   # marked for the scheduler's next tick
        status, detail = connectors.check(connectors.get(child["id"]))
        self.assertEqual((status, detail), ("ok", "6 Stripe MCP tools, test mode"))
        auth = {str(r["headers"].get("authorization")) for r in hosted.requests if r["method"] == "POST"}
        self.assertEqual(auth, {f"Bearer {sk}"})   # mcp_headers: the Stripe card's key
        self.assertEqual(sorted(i["key"] for i in connectors.visible(c)), ["stripe", "stripe_mcp"])
        found = tools.execute(ctx, "find_tool", {"query": "echo"})
        # A tool Stripe's map does not name is irreversible (independent review; it was unclassified)
        self.assertIn("stripe_mcp__echo [irreversible]", found)
        self.assertIn("Loaded: stripe_mcp__echo", found)
        queued = tools.execute(ctx, "stripe_mcp__echo", {"text": "hi"})   # locked to the owner's approval
        self.assertIn("Queued for the owner's approval", queued)
        # tag_for_call: a refund through stripe_api_write is irreversible (locked), whatever the map says.
        ctx.loaded_by_name["stripe_mcp__stripe_api_write"] = {
            "name": "stripe_mcp__stripe_api_write", "tag": "publish", "instance_id": child["id"], "allowed": True,
            "parameters": {"type": "object", "properties": {}}}
        out = connectors.call(ctx, "stripe_mcp__stripe_api_write", {"method": "POST", "path": "/v1/refunds"})
        self.assertIn("Queued for the owner's approval", out)
        row = db.one("SELECT payload FROM approvals WHERE company_id=? AND action='connector_call' ORDER BY id DESC "
                     "LIMIT 1", (c["id"],))
        self.assertEqual(json.loads(row["payload"])["tag"], "irreversible")

        # Unticking it turns the MCP part off again: left out, and a loaded tool is refused.
        self.save("stripe", None, {"use_mcp": False}, id=parent["id"])
        self.assertEqual([i["key"] for i in connectors.visible(c)], ["stripe"])
        refused = tools.execute(ctx, "stripe_mcp__echo", {"text": "hi"})
        self.assertTrue(refused.startswith("ERROR: Stripe MCP (hosted) is turned off."), refused)
        self.assertEqual(connectors.check(connectors.get(child["id"]))[0], "warn")


# ================================================================ Resend through emailer

class TestResend(Base):
    def test_emailer_sends_through_resend_with_the_provider_auto(self):
        resend = self.fake(FakeResend, domains=("pawplan.example",))
        c = make_company("ResendCo")
        self.assertEqual(db.company_settings(c)["email_provider"], "auto")
        rk = key("re_int")
        out = self.save("resend", c, {"api_key": rk, "from_address": "hello@pawplan.example", "from_name": "PawPlan",
                                      "api_base": resend.url})
        self.assertEqual(out["health"], {"status": "ok", "detail": "pawplan.example verified"})
        iid = out["instance"]["id"]
        text = emailer.send(db.get_company(c["id"]), "jo@shop.test", "Hello Jo", "A short note.")
        self.assertEqual(text, "Email sent to jo@shop.test (through Resend).")
        self.assertEqual(len(resend.sent), 1)
        body = resend.sent[0]["body"]
        self.assertEqual((body["to"], body["subject"], body["text"]), ("jo@shop.test", "Hello Jo", "A short note."))
        self.assertEqual(body["from"], "PawPlan <hello@pawplan.example>")
        self.assertTrue(resend.sent[0]["idempotency_key"])
        row = db.one("SELECT * FROM emails WHERE company_id=? AND direction='out'", (c["id"],))
        self.assertEqual((row["uid"], row["message_id"]), (resend.sent[0]["id"], body["headers"]["Message-ID"]))
        # metric_prefix: Resend's numbers are email.*, from source resend
        out = call("POST", f"/api/connectors/{iid}/test", {})
        self.assertEqual({m["name"] for m in out["metrics"]}, {"email.sent_7d", "email.replies_7d"})
        self.assertEqual(next(m["value"] for m in out["metrics"] if m["name"] == "email.sent_7d"), 1.0)
        self.assertEqual({r["source"] for r in db.q("SELECT source FROM metrics WHERE company_id=?", (c["id"],))},
                         {"resend"})
        # With no SMTP details the company still has email: the facts, the outreach guard and the dashboard agree
        fresh = db.get_company(c["id"])
        self.assertEqual(emailer.outbound(fresh), {"via": "resend", "sender": "hello@pawplan.example"})
        self.assertTrue(truth._state(fresh)["email"]["line"].startswith(
            "Email: connected, sending as hello@pawplan.example through Resend."))
        self.assertIsNone(truth.task_guard(ctx_for(c, "marketing"), "outreach", "Email 10 dog groomers"))
        self.assertTrue(call("GET", f"/api/companies/{c['id']}")["email_ready"])
        # A rejected key: auto falls back to SMTP, which this company does not have.
        out = self.save("resend", None, {"api_key": "nope"}, id=iid)
        self.assertEqual(out["health"]["status"], "bad")
        with self.assertRaises(emailer.EmailError) as e:
            emailer.send(db.get_company(c["id"]), "jo@shop.test", "Again", "Body")
        self.assertIn("SMTP", str(e.exception))
        self.assertEqual(len(resend.sent), 1)
        self.assertIsNone(emailer.outbound(db.get_company(c["id"])))
        self.assertIn("Email is not connected", truth.task_guard(ctx_for(c, "marketing"), "outreach", "Email 10"))
        self.assertFalse(call("GET", f"/api/companies/{c['id']}")["email_ready"])   # the last check failed

    def test_email_ready_follows_the_provider_and_the_resend_check(self):
        resend = self.fake(FakeResend, domains=("pawplan.example",))
        c = make_company("ReadyCo")
        ready = lambda: call("GET", f"/api/companies/{c['id']}")["email_ready"]   # noqa: E731
        self.assertFalse(ready())   # no SMTP, no Resend
        iid = self.save("resend", c, {"api_key": key("re_ready"), "from_address": "hi@pawplan.example",
                                      "api_base": resend.url})["instance"]["id"]
        self.assertTrue(ready())    # auto with a healthy Resend
        for provider, expected in (("resend", True), ("smtp", False), ("auto", True)):
            call("PATCH", f"/api/companies/{c['id']}", {"settings": {"email_provider": provider}})
            self.assertEqual(ready(), expected, provider)
        self.save("resend", None, {}, id=iid, enabled=False)
        self.assertFalse(ready())   # turned off
        call("PATCH", f"/api/companies/{c['id']}", {"settings": {"smtp_host": "smtp.test", "smtp_user": "u@x.test",
                                                                "smtp_password": key("pw-")}})
        self.assertTrue(ready())    # SMTP alone still counts


# ================================================================ Buffer through actions._social

class TestBuffer(Base):
    def test_saved_off_by_default_then_posts_through_social(self):
        buffer = self.fake(FakeBuffer)
        c = make_company("BufferCo")
        # default channels: approved posts go only there (independent review)
        out = self.save("buffer", c, {"api_key": buffer.token, "api_base": buffer.url,
                                      "default_channels": "chan_x"})   # no "enabled": beta
        inst = out["instance"]
        self.assertFalse(inst["enabled"])
        self.assertEqual(out["health"], {"status": "warn", "detail": connectors.OFF_TEXT})
        # Test works on the card while it is still off ("press Test, then turn it on")
        tested = call("POST", f"/api/connectors/{inst['id']}/test", {})["health"]
        self.assertEqual(tested["status"], "ok")
        self.assertTrue(tested["detail"].startswith("signed in as owner@pawplan.example (beta API)"), tested)
        self.assertIn(connectors.OFF_TEXT, tested["detail"])
        # Still off: a social post does not go to Buffer
        res = actions.execute(db.get_company(c["id"]), "social_post", {"platform": "x", "content": "Hello dogs"})
        self.assertIn("No posting webhook", res)
        self.assertEqual(buffer.posts, [])
        out = self.save("buffer", None, {}, id=inst["id"], enabled=True)
        self.assertEqual((out["instance"]["enabled"], out["health"]["status"]), (True, "ok"))
        # Approved through the gate: _social posts through Buffer, and the audit log has it.
        agent = agent_of(c, "marketing")
        queued = actions.gate(db.get_company(c["id"]), agent, None, "social_post",
                              {"platform": "x", "content": "Meet BufferCo", "link": ""})
        self.assertIn("Queued", queued)
        aid = db.scalar("SELECT id FROM approvals WHERE company_id=? AND action='social_post' AND status='pending'",
                        (c["id"],))
        done = actions.decide(aid, "approve")
        self.assertEqual(done["status"], "done", done["result"])
        self.assertTrue(done["result"].startswith("Added to the Buffer queue: x PawPlan on X (post post_1)"),
                        done["result"])
        self.assertEqual([(p["input"]["channelId"], p["input"]["text"]) for p in buffer.posts],
                         [("chan_x", "Meet BufferCo")])
        self.assertEqual(db.one("SELECT approved_by FROM audit WHERE approval_id=?", (aid,))["approved_by"], "owner")
        # Buffer's own read tool, loaded with find_tool
        ctx = ctx_for(c, "marketing")
        self.assertIn("buffer__list_channels", tools.execute(ctx, "find_tool", {"query": "buffer channels"}))
        self.assertIn("chan_li: linkedin", tools.execute(ctx, "buffer__list_channels", {}))


# ================================================================ Brave: the card writes the global setting

class TestBrave(Base):
    def tearDown(self):
        call("PUT", "/api/settings", {"brave_api_key": ""})   # no later test may search with this key
        web.reset_status()

    def test_card_writes_the_global_setting_into_the_vault(self):
        bk = key("BSAint")
        seen: list = []

        def fake_brave(query, n, settings):
            seen.append(settings.get("brave_api_key"))
            return [{"title": "T", "url": "https://x.example", "snippet": "s"}]
        with mock.patch.object(web, "_brave", side_effect=fake_brave):
            out = self.save("brave", None, {"brave_api_key": bk})
        self.assertEqual(out["health"], {"status": "ok", "detail": "Brave answered with 1 result(s)"})
        self.assertEqual(seen, [bk])
        iid = out["instance"]["id"]
        self.assertEqual(out["instance"]["config"], {"brave_api_key": MASK})
        raw = db.scalar("SELECT value FROM settings WHERE key='brave_api_key'")
        self.assertEqual(json.loads(raw), "vault:settings.brave_api_key")
        self.assertEqual(vault.get("settings.brave_api_key"), bk)
        self.assertEqual(db.get_settings()["brave_api_key"], bk)
        self.assertNotIn(f"connector.{iid}.brave_api_key", vault.names())
        self.assertEqual(json.loads(db.scalar("SELECT config FROM connectors WHERE id=?", (iid,))), {})
        self.assertEqual(call("GET", "/api/settings")["settings"]["brave_api_key"], MASK)
        # The mask keeps it; an empty value clears the setting and the vault entry.
        with mock.patch.object(web, "_brave", side_effect=fake_brave):
            self.save("brave", None, {"brave_api_key": MASK}, id=iid)
            self.assertEqual(db.get_settings()["brave_api_key"], bk)
            out = self.save("brave", None, {"brave_api_key": ""}, id=iid)
        self.assertEqual(out["health"]["status"], "warn")
        self.assertEqual(db.get_settings()["brave_api_key"], "")
        self.assertNotIn("settings.brave_api_key", vault.names())


# ================================================================ Playwright: health, launch, refusals

class TestPlaywright(Base):
    def test_health_warns_without_npx_and_browser_check_falls_back(self):
        timeouts: list = []
        real_timed = connectors._timed

        def timed(fn, timeout, *args):
            timeouts.append(timeout)
            return real_timed(fn, timeout, *args)
        with mock.patch.object(conn_playwright.shutil, "which", return_value=None), \
                mock.patch.object(connectors, "_timed", side_effect=timed):
            out = self.save("playwright", None, {}, enabled=True)
        self.assertEqual(out["health"], {"status": "warn", "detail": "npx not found: install Node.js"})
        self.assertEqual(timeouts, [120.0])   # the first start may download a browser
        iid = out["instance"]["id"]
        self.assertEqual({r["key"]: r for r in connectors.doctor_rows()}[f"connector:{iid}"]["status"], "warn")
        c = make_company("BrowserCo", members=("ceo", "qa"))
        site = workspace.safe_path(c, "site")
        site.mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text("<!doctype html><title>Hi</title><h1>Hello</h1>", encoding="utf-8")
        ctx = ctx_for(c, "qa")
        text = tools.execute(ctx, "browser_check", {"path_or_url": "site/index.html"})
        self.assertTrue(text.startswith("Static check only: Playwright failed: npx not found: install Node.js"), text)
        # refuse_call answers without "ERROR: "; the framework adds it. Blocked tools are refused for every role.
        ctx.loaded_by_name["playwright__browser_navigate"] = {
            "name": "playwright__browser_navigate", "tag": "read", "instance_id": iid, "allowed": True,
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}
        refused = connectors.call(ctx, "playwright__browser_navigate", {"url": "http://127.0.0.1:8420/preview/x/"})
        self.assertEqual(refused, "ERROR: This PC's addresses are off limits here: use browser_check for your own "
                                  "site.")
        self.assertEqual(connectors.call(ctx, "playwright__browser_evaluate", {}),
                         "ERROR: browser_evaluate is not available through Holdco")

    def test_command_override_reaches_the_mcp_client(self):
        line = f'"{sys.executable}" "{FAKE_MCP}" --stdio'
        out = self.save("playwright", None, {"command": line}, enabled=True)
        inst = connectors.get(out["instance"]["id"])
        kw = connectors._mcp_settings(connectors.DEFINITIONS["playwright"], inst)
        self.assertEqual((kw["transport"], kw["command"], kw["args"]), ("stdio", sys.executable, [FAKE_MCP, "--stdio"]))
        # The fake server started from the override answered tools/list, without a browser_navigate tool.
        self.assertEqual(out["health"]["status"], "bad")
        self.assertIn("no browser_navigate tool", out["health"]["detail"])


# ================================================================ a generic MCP connector over stdio

FAKESTDIO = {
    "label": "Fake stdio server", "category": "Test", "kind": "mcp-stdio", "scope": "company",
    "setup": {"where_to_get_key": "Nothing.", "permissions": [], "fields": [], "test_mode": "", "docs_url": "",
              "agents_can": ["echo and add"]},
    "mcp": {"command": sys.executable, "args": [FAKE_MCP, "--stdio"],
            "classify": [("echo", "read"), ("add", "write"), ("fail", "read"), ("image", "read")],
            "allow": {"*": ["echo"], "engineering": ["*"]}, "blocked": ["slow"]},
}


class TestStdioMCP(Base):
    def setUp(self):
        super().setUp()
        connectors.DEFINITIONS["fakestdio"] = FAKESTDIO
        self.addCleanup(connectors.DEFINITIONS.pop, "fakestdio", None)

    def test_find_tool_read_runs_write_asks_then_runs_and_is_audited(self):
        c = make_company("StdioCo")
        out = self.save("fakestdio", c, {})
        self.assertEqual(out["health"], {"status": "ok", "detail": "6 tools available."})
        ctx = ctx_for(c, "engineering")
        found = tools.execute(ctx, "find_tool", {"query": "echo add numbers"})
        self.assertIn("Loaded: fakestdio__add, fakestdio__echo", found)
        self.assertNotIn("slow", found)
        self.assertEqual(tools.execute(ctx, "fakestdio__echo", {"text": "hello there"}), "hello there")
        self.assertIn("Queued for the owner's approval", tools.execute(ctx, "fakestdio__add", {"a": 1, "b": 2}))
        aid = db.scalar("SELECT id FROM approvals WHERE company_id=? AND action='connector_call' AND "
                        "status='pending'", (c["id"],))
        done = actions.decide(aid, "approve")
        self.assertEqual((done["status"], done["result"]), ("done", "3"))
        audit = db.one("SELECT * FROM audit WHERE approval_id=?", (aid,))
        self.assertEqual(json.loads(audit["payload"])["tag"], "write")
        # marketing may run read tools only: add is listed, not loaded
        mctx = ctx_for(c, "marketing")
        text = tools.execute(mctx, "find_tool", {"query": "add numbers"})
        self.assertIn("fakestdio__add [write]", text)
        self.assertIn("(not allowed for your role)", text)
        self.assertNotIn("fakestdio__add", mctx.loaded_by_name)


# ================================================================ live refresh hints, and an OAuth sign-in

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class TestLiveHintsAndOAuth(Base):
    def setUp(self):
        super().setUp()
        self.queue = bus.subscribe()
        self.addCleanup(bus.unsubscribe, self.queue)

    def hints(self) -> list:
        """The "connectors" refresh hints published since the last call: [company_id, ...]."""
        out = []
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return out
            if "connectors" in str(item.get("type")):
                out.append(item.get("company_id"))

    def test_save_check_poll_and_remove_send_a_hint(self):
        stripe = self.fake(FakeStripe)
        c = make_company("HintCo")
        iid = self.save("stripe", c, {"api_key": key("sk_test_hint"), "api_base": stripe.url})["instance"]["id"]
        self.assertEqual(set(self.hints()), {c["id"]})   # the save and its health check
        connectors.check(connectors.get(iid))
        self.assertEqual(self.hints(), [c["id"]])
        self.assertTrue(connectors.poll_metrics(only=iid))
        self.assertEqual(self.hints(), [c["id"]])
        with contextlib.redirect_stdout(io.StringIO()):
            connectors.remove(iid)
        self.assertEqual(self.hints(), [c["id"]])
        self.assertEqual(connectors.poll_metrics(only=iid), [])   # nothing written, no hint
        self.assertEqual(self.hints(), [])

    def test_oauth_sign_in_through_the_framework(self):
        auth = self.fake(FakeOAuth)
        server_ = self.fake(FakeMCPHttp, auth_base=auth.url)
        connectors.DEFINITIONS["oauthmcp"] = {
            "label": "Signed MCP", "category": "Test", "kind": "mcp-http", "scope": "global",
            "setup": {"where_to_get_key": "Sign in.", "permissions": [], "fields": [], "test_mode": "",
                      "docs_url": "", "agents_can": ["echo"]},
            "mcp": {"url": server_.mcp_url, "classify": [("echo", "read")], "allow": {"*": ["*"]}}}
        self.addCleanup(connectors.DEFINITIONS.pop, "oauthmcp", None)
        for cache in (oauth._meta, oauth._registered, oauth._pending):
            cache.clear()
        out = self.save("oauthmcp", None, {})
        iid = out["instance"]["id"]
        self.assertEqual(out["health"], {"status": "bad",
                                         "detail": "Sign in needed: press Sign in on this connector's card."})
        url = call("POST", f"/api/connectors/{iid}/oauth/start", {})["url"]
        self.assertTrue(url.startswith(auth.url + "/authorize?"))
        with self.assertRaises(urllib.error.HTTPError) as e:   # the browser's part: the redirect to the callback
            _no_redirect.open(url, timeout=10)
        location = e.exception.headers["Location"]
        e.exception.close()
        self.assertTrue(location.startswith(f"http://127.0.0.1:{config.PORT}/oauth/callback?"))
        self.hints()
        code, page = oauth.callback_page({k: v[0] for k, v in
                                          urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).items()})
        self.assertEqual(code, 200)
        self.assertIn("Connected Signed MCP. You can close this tab.", page)
        self.assertEqual(self.hints(), [None])   # a global connector: every open dashboard reloads it
        stored = json.loads(vault.get(f"oauth.{iid}"))
        self.assertEqual(stored["resource"], server_.mcp_url)
        inst = connectors.get(iid)
        self.assertIsNone(inst["last_check"])   # the scheduler checks it again on its next tick
        self.assertEqual(connectors.check(inst), ("ok", "6 tools available."))
        self.assertTrue(next(i for i in call("GET", "/api/connectors")["instances"] if i["id"] == iid)["signed_in"])
        c = make_company("SignedCo", members=("ceo", "research"))
        ctx = ctx_for(c, "research")
        self.assertIn("Loaded: oauthmcp__echo", tools.execute(ctx, "find_tool", {"query": "echo"}))
        self.assertEqual(tools.execute(ctx, "oauthmcp__echo", {"text": "signed"}), "signed")
        bearer = {str(r["headers"].get("authorization")) for r in server_.requests
                  if r["method"] == "POST" and r["status"] == 200}
        self.assertEqual(bearer, {f"Bearer {stored['access_token']}"})
        with contextlib.redirect_stdout(io.StringIO()):
            connectors.remove(iid)
        self.assertNotIn(f"oauth.{iid}", vault.names())


# ================================================================ vault findings

class _Resp:
    def __init__(self, body: dict):
        self._b = json.dumps(body).encode()
        self.status = 200

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSecretsStayOutOfTheDatabase(Base):
    def assert_nowhere_in_the_database(self, *values):
        conn = sqlite3.connect(str(config.DB_PATH))
        try:
            for table in ("settings", "companies"):
                text = json.dumps(conn.execute(f"SELECT * FROM {table}").fetchall())
                for v in values:
                    self.assertFalse(v in text, f"a secret is in plain text in the {table} table")
        finally:
            conn.close()
        for path in (config.DB_PATH, Path(str(config.DB_PATH) + "-wal")):
            if path.exists():
                raw = path.read_bytes()
                for v in values:
                    self.assertFalse(v.encode() in raw, f"a secret is in plain text in {path.name}")

    def test_no_plain_text_secret_after_a_deploy_a_pin_and_a_settings_save(self):
        c = make_company("SecretCo")
        nt, sp, bk = key("nfp_int"), key("smtp-int-"), key("BSAsecret")
        call("PATCH", f"/api/companies/{c['id']}", {"settings": {"netlify_token": nt, "smtp_password": sp,
                                                                "smtp_host": "smtp.test", "smtp_user": "u@x.test"}})
        call("PUT", "/api/settings", {"brave_api_key": bk})
        self.addCleanup(call, "PUT", "/api/settings", {"brave_api_key": ""})
        stored = stored_settings(c)
        self.assertEqual(stored["netlify_token"], f"vault:company.{c['id']}.netlify_token")
        # a Netlify deploy (the API is patched: no network)
        site = workspace.safe_path(c, "site")
        site.mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text("<h1>Hi</h1>", encoding="utf-8")
        posted: list = []

        def post_json(url, body, headers=None, timeout=30):
            posted.append(headers)
            return 201, json.dumps({"id": "site_1", "ssl_url": "https://secretco.netlify.app"})
        with mock.patch.object(actions, "_post_json", side_effect=post_json), \
                mock.patch.object(actions.urllib.request, "urlopen",
                                  return_value=_Resp({"ssl_url": "https://secretco.netlify.app"})):
            self.assertEqual(actions._deploy_netlify(db.get_company(c["id"])),
                             "Website deployed: https://secretco.netlify.app")
        self.assertEqual(posted[0]["Authorization"], f"Bearer {nt}")   # the real token was still used
        # a pinned-context save, the planning and truth helpers, and an owner's settings save
        context._save_pinned(db.get_company(c["id"]), ["notes.md"])
        planning.update_company_settings(c["id"], budget_notice="2026-10-15")
        truth._mark_scanned(c["id"])
        self.assert_nowhere_in_the_database(nt, sp, bk)   # before the owner's save could tidy anything up
        call("PATCH", f"/api/companies/{c['id']}", {"settings": {"directives": "Be brief"}})
        stored = stored_settings(c)
        self.assertEqual((stored["netlify_site_id"], stored["site_url"]), ("site_1", "https://secretco.netlify.app"))
        self.assertEqual((stored["pinned_context"], stored["budget_notice"], stored["truth_scan_done"],
                          stored["directives"]), (["notes.md"], "2026-10-15", True, "Be brief"))
        for k in ("netlify_token", "smtp_password"):
            self.assertEqual(stored[k], f"vault:company.{c['id']}.{k}")
        self.assertEqual(db.company_settings(db.get_company(c["id"]))["smtp_password"], sp)
        self.assert_nowhere_in_the_database(nt, sp, bk)

    def test_update_company_settings_routes_a_secret_to_the_vault(self):
        c = make_company("RouteCo")
        sp = key("route-int-")
        stored = db.update_company_settings(c["id"], smtp_password=sp, smtp_port=465)
        self.assertEqual(stored["smtp_password"], f"vault:company.{c['id']}.smtp_password")
        self.assertEqual(vault.get(f"company.{c['id']}.smtp_password"), sp)
        db.update_company_settings(c["id"], smtp_password="")
        self.assertIsNone(vault.get(f"company.{c['id']}.smtp_password"))
        self.assertEqual(stored_settings(c)["smtp_password"], "")
        self.assert_nowhere_in_the_database(sp)

    def test_a_failing_secret_is_reported_once(self):
        name = f"company.999{secrets.randbelow(10**6)}.smtp_password"
        c = {"settings": json.dumps({"smtp_password": "vault:" + name})}
        out = io.StringIO()
        with mock.patch.object(vault, "get", side_effect=vault.VaultError(f"Secret {name} was stored with dpapi on "
                                                                          "another machine")), \
                contextlib.redirect_stdout(out):
            for _ in range(3):
                self.assertEqual(db.company_settings(c)["smtp_password"], "")
        self.assertEqual(out.getvalue().count(name), 1, out.getvalue())

    def test_deleting_a_company_deletes_its_secrets_and_connectors(self):
        stripe = self.fake(FakeStripe)
        c = make_company("DoomedCo")
        call("PATCH", f"/api/companies/{c['id']}", {"settings": {"smtp_password": key("doomed-")}})
        iid = self.save("stripe", c, {"api_key": key("sk_test_doom"), "api_base": stripe.url})["instance"]["id"]
        vault.put(f"oauth.{iid}", json.dumps({"access_token": key("at-")}))
        mine = re.compile(rf"^(company\.{c['id']}\.|connector\.{iid}\.|oauth\.{iid}$)")
        self.assertEqual(len([n for n in vault.names() if mine.match(n)]), 3)
        with self.assertRaises(HTTPError):
            call("DELETE", f"/api/companies/{c['id']}", {"confirm": "wrong"})
        with contextlib.redirect_stdout(io.StringIO()):
            call("DELETE", f"/api/companies/{c['id']}", {"confirm": "DoomedCo"})
        self.assertEqual([n for n in vault.names() if mine.match(n)], [])
        self.assertIsNone(connectors.get(iid))
        self.assertIsNone(db.get_company(c["id"]))



class TestConnectingIsNewsForPlanning(Base):
    """Connecting a service, or one breaking, changes planning's change stamp, so the CEO plans with it at once."""

    def test_saving_and_breaking_a_connector_changes_the_stamp(self):
        from holdco import planning
        stripe = self.fake(FakeStripe)
        c = make_company("NewsCo")
        before = planning.change_stamp(c)
        out = self.save("stripe", c, {"api_key": key("sk_test_news"), "api_base": stripe.url}, enabled=True)
        self.assertEqual(out["health"]["status"], "ok")
        connected = planning.change_stamp(c)
        self.assertNotEqual(before, connected)
        db.update("connectors", out["instance"]["id"], status="bad", detail="key rejected")
        self.assertNotEqual(connected, planning.change_stamp(c))


if __name__ == "__main__":
    unittest.main()
