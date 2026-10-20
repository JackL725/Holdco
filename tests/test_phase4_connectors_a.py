"""Phase 4 connectors, part A: Porkbun, Cloudflare (the deploy target), n8n (webhook and MCP) and the later tier.

The real definitions run in the real framework (connectors.py) with the real file vault, against fakes on
127.0.0.1 (tests/fake_porkbun.py, fake_cloudflare.py, fake_n8n.py, and fake_mcp_server.py for n8n's MCP server).
Instances are created through the dashboard's route functions, so the setup cards, the vault and the test switch
(HOLDCO_ALLOW_LOCAL, without which a card's hidden api_base is refused) are exercised too. Nothing touches the
network: every address is a fake's.

Run with:  python -m unittest tests.test_phase4_connectors_a -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-phase4a-"))
os.environ["HOLDCO_VAULT_BACKEND"] = "file"   # never touch a developer's keyring
os.environ.pop("HOLDCO_VAULT_PASSPHRASE", None)

from holdco import (actions, conn_cloudflare, conn_later, conn_n8n, conn_porkbun, connectors, db,  # noqa: E402
                    notify, roles, routing, server, tools, trust, vault, workspace)
from holdco.routing import HTTPError  # noqa: E402
from tests.fake_cloudflare import FakeCloudflare  # noqa: E402
from tests.fake_mcp_server import FakeMCPHttp  # noqa: E402
from tests.fake_n8n import FakeN8n  # noqa: E402
from tests.fake_porkbun import FakePorkbun  # noqa: E402

db.init()
assert server   # imported for its routes

MASK = "•" * 8
LATER = {
    "comfyui": ("Local image generation shares the GPU with the language model. It will be scheduled in the quiet "
                "window once a company has traction."),
    "ads": "Ad platforms spend money per hour. They come after revenue justifies a hard daily cap per company.",
    "accounting": "QuickBooks or Xero sync comes once there are transactions to reconcile.",
}


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


def set_policies(company: dict, **policies) -> dict:
    cs = json.loads(db.get_company(company["id"])["settings"] or "{}")
    cs["policies"] = {**cs.get("policies", {}), **policies}
    db.update("companies", company["id"], settings=json.dumps(cs))
    return db.get_company(company["id"])


class _Handler:
    def __init__(self, query=None):
        self.query = {k: [str(v)] for k, v in (query or {}).items()}


def call(method: str, path: str, body=None, query: dict | None = None):
    """Dispatch like server.Handler does, so the route patterns are exercised too."""
    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(_Handler(query), m, body)
    raise AssertionError(f"no route for {method} {path}")


def pending(company: dict, action: str = "connector_call") -> list[dict]:
    return db.q("SELECT * FROM approvals WHERE company_id=? AND action=? AND status='pending' ORDER BY id",
                (company["id"], action))


def write_site(company: dict, files: dict[str, str]) -> None:
    site = workspace.safe_path(company, "site")
    for rel, text in files.items():
        p = site / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


class Base(unittest.TestCase):
    """Fakes started per test; every connector instance a test creates is removed afterwards (with its vault
    secrets: a global instance would otherwise reach the companies of later test modules); notify.send silenced;
    Porkbun's remembered sandbox flags forgotten; no waiting on Porkbun's rate limit."""

    def setUp(self):
        self.alerts: list = []
        for p in (mock.patch.object(notify, "send", side_effect=lambda text, *a, **kw: self.alerts.append(text)),
                  mock.patch.dict(os.environ, {"HOLDCO_ALLOW_LOCAL": "127.0.0.1:*"}),
                  mock.patch.object(conn_porkbun, "RETRY_WAIT", 0)):
            p.start()
            self.addCleanup(p.stop)
        db.set_settings({"kill_switch": False})
        self.before = {r["id"] for r in db.q("SELECT id FROM connectors")}
        self.addCleanup(self._remove_new_instances)
        conn_porkbun.forget_modes()
        self.addCleanup(conn_porkbun.forget_modes)

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

    def assert_secret_in_vault_only(self, iid: int, field: str, value: str):
        self.assertEqual(vault.get(f"connector.{iid}.{field}"), value)
        self.assertNotIn(value, db.scalar("SELECT config FROM connectors WHERE id=?", (iid,)))
        listing = json.dumps(call("GET", "/api/connectors"))
        self.assertNotIn(value, listing)


# ================================================================ Porkbun

class TestPorkbun(Base):
    def connect(self, fake, api_key=None, **cfg) -> tuple[dict, dict, str, str]:
        api_key = api_key or key("pk1_sb_")
        secret = key("sk1_sb_")
        out = self.save("porkbun", None, {"api_key": api_key, "secret_key": secret, "api_base": fake.url, **cfg})
        return out, out["instance"], api_key, secret

    def test_setup_card_and_secrets_go_to_the_vault(self):
        porkbun = self.fake(FakePorkbun)
        card = call("GET", "/api/connectors")["definitions"]["porkbun"]
        self.assertEqual((card["scope"], card["kind"], card["category"]), ("global", "api", "Domains"))
        fields = {f["key"]: f for f in card["setup"]["fields"]}
        self.assertTrue(fields["api_key"]["secret"] and fields["secret_key"]["secret"])
        self.assertEqual((fields["dry_run"]["type"], fields["dry_run"]["default"]), ("bool", True))
        self.assertEqual(card["hidden_fields"], ["api_base"])
        self.assertTrue(card["has_test_mode"])
        self.assertIn("without purchase rights", " ".join(card["setup"]["permissions"]))
        self.assertIn("$100 a month", " ".join(card["setup"]["permissions"]))
        self.assertIn("sandbox", card["setup"]["where_to_get_key"])
        tags = {t["tool"]: t["tag"] for t in card["tools"]}
        self.assertEqual(tags, {"check_domain": "read", "list_domains": "read", "dns_records": "read",
                                "set_dns_record": "write", "register_domain": "spend"})
        self.assertIn("cannot be undone", next(t for t in card["tools"] if t["tool"] == "register_domain")
                      ["description"])
        self.assertTrue(card["available"])

        out, inst, api_key, secret = self.connect(porkbun)
        self.assertEqual(out["health"]["status"], "ok", out["health"])
        self.assertEqual(inst["config"], {"api_key": MASK, "secret_key": MASK, "api_base": porkbun.url})
        self.assertIsNone(inst["company_id"])   # global: the keys are account-level
        self.assert_secret_in_vault_only(inst["id"], "api_key", api_key)
        self.assert_secret_in_vault_only(inst["id"], "secret_key", secret)
        # A card saved without dry_run is a dry run; the hidden API address needs the test switch.
        self.assertTrue(conn_porkbun.dry_run(connectors.get(inst["id"])))
        with mock.patch.dict(os.environ, {"HOLDCO_ALLOW_LOCAL": ""}):
            with self.assertRaises(HTTPError):
                self.save("porkbun", None, {"api_base": "http://127.0.0.1:9"}, id=inst["id"])
        # The mask keeps a secret; removing the card deletes it from the vault.
        self.save("porkbun", None, {"api_key": MASK}, id=inst["id"])
        self.assertEqual(vault.get(f"connector.{inst['id']}.api_key"), api_key)
        call("DELETE", f"/api/connectors/{inst['id']}")
        self.assertIsNone(vault.get(f"connector.{inst['id']}.api_key"))
        self.assertIsNone(vault.get(f"connector.{inst['id']}.secret_key"))

    def test_porkbun(self):
        porkbun = self.fake(FakePorkbun, owned=("pawplan.example",), taken=("taken.co",))
        c = make_company("DomainCo")

        # Health with the header auth; the mode from the pk1_sb_ key, confirmed by "sandbox": true in the reply.
        out, inst, api_key, secret = self.connect(porkbun)
        iid = inst["id"]
        self.assertEqual(out["health"], {"status": "ok",
                                         "detail": "sandbox mode, dry run on: nothing is bought or changed"})
        ping = porkbun.calls("/ping")[-1]
        self.assertEqual((ping["headers"]["x-api-key"], ping["headers"]["x-secret-api-key"]), (api_key, secret))
        self.assertNotIn("apikey", ping["body"])   # the keys travel in headers only
        self.assertEqual(inst["mode"], "test")
        self.assertIn("porkbun (test mode)", connectors.prompt_section(c))

        # A key without the prefix is live, unless Porkbun's answers say sandbox.
        live = self.save("porkbun", None, {"api_key": key("pk1_live"), "dry_run": False}, id=iid)
        self.assertEqual(live["health"]["detail"], "live mode, dry run off: approved purchases and DNS changes "
                                                   "are real")
        self.assertEqual(live["instance"]["mode"], "live")
        porkbun.sandbox_flag = True
        self.assertEqual(connectors.check(connectors.get(iid))[1],
                         "sandbox mode, dry run off: approved purchases and DNS changes are real")
        self.assertEqual(conn_porkbun.mode(connectors.get(iid)), "sandbox")
        # A sandbox-looking key whose answers do not say sandbox counts as live.
        porkbun.sandbox_flag = False
        status, detail = self.save("porkbun", None, {"api_key": key("pk1_sb_")}, id=iid)["health"].values()
        self.assertEqual(status, "warn")
        self.assertIn("treats it as live", detail)
        porkbun.sandbox_flag = None
        self.save("porkbun", None, {"api_key": api_key, "dry_run": True}, id=iid)
        self.assertEqual(conn_porkbun.mode(connectors.get(iid)), "sandbox")

        # check_domain is a read: it runs at once, no approval.
        ctx = ctx_for(c, "ceo")
        found = tools.execute(ctx, "find_tool", {"query": "check domain available price"})
        self.assertIn("porkbun__check_domain [read]", found)
        got = tools.execute(ctx, "porkbun__check_domain", {"domain": "PawPlan.co"})
        self.assertEqual(got, "pawplan.co is available: $10.81 for the first year, then $10.81 a year (sandbox "
                              "mode). Buying it needs the owner's approval (porkbun__register_domain).")
        self.assertIn("not available", tools.execute(ctx, "porkbun__check_domain", {"domain": "taken.co"}))
        self.assertEqual(pending(c), [])
        ev = db.one("SELECT * FROM events WHERE company_id=? AND kind='tool' ORDER BY id DESC LIMIT 1", (c["id"],))
        self.assertEqual(ev["message"], "called porkbun check_domain")

        # register_domain always queues as spend: every policy on auto and the ladder at its top change nothing.
        c = set_policies(c, spend_money="auto", irreversible="auto", connector_write="auto",
                         connector_publish="auto")
        trust._save(c["id"], "spend_money", trust.AUTO, 0, 0)
        ctx = ctx_for(c, "ceo")
        self.assertIn("porkbun__register_domain [spend]",
                      tools.execute(ctx, "find_tool", {"query": "register domain buy"}))
        queued = tools.execute(ctx, "porkbun__register_domain", {"domain": "pawplan.co", "price_usd": 0.01})
        self.assertIn("Queued for the owner's approval", queued)
        self.assertEqual(porkbun.calls("/domain/create"), [])   # nothing bought before the owner said yes
        [row] = pending(c)
        p = json.loads(row["payload"])
        self.assertEqual((p["connector"], p["tool"], p["tag"], p["tags"]),
                         ("porkbun", "register_domain", "spend", ["spend", "irreversible"]))
        self.assertEqual(p["args"], {"domain": "pawplan.co", "years": 1, "price_usd": 10.81})   # Porkbun's price
        self.assertEqual((p["amount_usd"], p["dry_run"], p["company_id"]), (10.81, True, c["id"]))
        self.assertIn("Buys pawplan.co for $10.81 (1 year) at Porkbun. This buys a domain and cannot be undone.",
                      p["notice"])
        self.assertIn("Dry run is on", p["notice"])
        self.assertEqual(p["_why_asked"], p["notice"])   # the Inbox card's "why you are asked" line shows it
        self.assertTrue(row["summary"].startswith("Buys pawplan.co for $10.81 (1 year) at Porkbun. This buys a "
                                                  "domain and cannot be undone."), row["summary"])
        text, _buttons, _kind = notify.build_approval(row)
        self.assertIn("$10.81", text)
        self.assertIn("cannot be undone", text)
        [card] = [a for a in call("GET", "/api/approvals", query={"company_id": c["id"]})["approvals"]
                  if a["id"] == row["id"]]
        self.assertEqual((card["tag"], card["payload"]["args"]["price_usd"]), ("spend", 10.81))
        self.assertEqual(db.effective_policy(c, "spend_money"), "approve")
        self.assertEqual(trust.gate_policy(c, "spend_money", p, "auto", [])[0], "approve")

        # Approved while dry run is on: the dry-run result, nothing bought, the approval id keys the request.
        done = actions.decide(row["id"], "approve")
        self.assertEqual(done["status"], "done", done["result"])
        self.assertTrue(done["result"].startswith("Dry run (nothing bought): Porkbun would register pawplan.co for "
                                                  "$10.81 (1 year)"), done["result"])
        self.assertEqual(porkbun.registered, [])
        create = porkbun.calls("/domain/create/pawplan.co")[-1]
        self.assertEqual(create["body"], {"cost": 1081, "agreeToTerms": "yes", "dryRun": True})
        self.assertTrue(create["idempotency_key"].startswith(f"holdco-approval-{row['id']}-"))
        audit = db.one("SELECT * FROM audit WHERE approval_id=?", (row["id"],))
        self.assertEqual((audit["action"], audit["approved_by"]), ("connector_call", "owner"))
        self.assertNotIn(api_key, audit["payload"] + audit["result"])

        # Dry run off: the purchase is real. Its reply is lost, the owner retries the same request, and the same
        # Idempotency-Key gets Porkbun's first answer back: the domain is bought once.
        self.save("porkbun", None, {"dry_run": False}, id=iid)
        queued = tools.execute(ctx, "porkbun__register_domain", {"domain": "pawplan.co"})
        self.assertIn("Queued for the owner's approval", queued)
        [row] = pending(c)
        self.assertNotIn("Dry run", json.loads(row["payload"])["notice"])
        porkbun.fail_after_apply = 1
        failed = actions.decide(row["id"], "approve")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("HTTP 502", failed["result"])
        self.assertEqual([r["domain"] for r in porkbun.registered], ["pawplan.co"])
        actions.retry(row["id"])
        done = actions.decide(row["id"], "approve")
        self.assertEqual(done["status"], "done", done["result"])
        self.assertTrue(done["result"].startswith("Registered pawplan.co for $10.81 (1 year, order "), done["result"])
        self.assertIn("It cannot be undone.", done["result"])
        self.assertEqual([r["domain"] for r in porkbun.registered], ["pawplan.co"])   # not twice
        keys = [r["idempotency_key"] for r in porkbun.calls("/domain/create/pawplan.co")]
        self.assertEqual(keys[-1], keys[-2])
        self.assertEqual(porkbun.replays, 1)

        # A name that is not available is refused before it reaches the Inbox.
        refused = tools.execute(ctx, "porkbun__register_domain", {"domain": "taken.co"})
        self.assertTrue(refused.startswith("ERROR: taken.co is not available to register."), refused)
        self.assertEqual(pending(c), [])

    def test_a_higher_price_at_approval_buys_nothing(self):
        porkbun = self.fake(FakePorkbun)
        c = make_company("PriceCo")
        self.connect(porkbun, dry_run=False)
        ctx = ctx_for(c, "ceo")
        tools.execute(ctx, "find_tool", {"query": "register domain"})
        self.assertIn("Queued", tools.execute(ctx, "porkbun__register_domain", {"domain": "newname.com", "years": 2}))
        [row] = pending(c)
        self.assertEqual(json.loads(row["payload"])["args"]["price_usd"], 22.16)   # two years at $11.08
        porkbun.prices["com"] = "12.00"
        done = actions.decide(row["id"], "approve")
        self.assertEqual(done["status"], "failed")
        self.assertIn("went up from $22.16 to $24.00", done["result"])
        self.assertEqual(porkbun.registered, [])
        # Rate limited checks are waited out once on the paths an agent cannot retry.
        porkbun.prices["com"] = "11.08"
        porkbun.rate_limit = 1
        self.assertIn("Queued", tools.execute(ctx, "porkbun__register_domain", {"domain": "other.com"}))
        # A purchase never runs outside an approved request.
        inst = connectors.instance("porkbun", c)
        with self.assertRaises(RuntimeError):
            connectors.execute({"connector_id": inst["id"], "connector": "porkbun", "tool": "register_domain",
                                "args": {"domain": "x.com", "price_usd": 99}, "company_id": c["id"]})

    def test_dns_reads_and_the_write_asks_under_connector_write(self):
        porkbun = self.fake(FakePorkbun, owned=("pawplan.example",))
        c = make_company("DnsCo")
        self.connect(porkbun)
        ctx = ctx_for(c, "engineering")
        tools.execute(ctx, "find_tool", {"query": "dns record"})
        self.assertIn("No DNS records", tools.execute(ctx, "porkbun__dns_records", {"domain": "pawplan.example"}))
        tools.execute(ctx, "find_tool", {"query": "set dns record"})
        args = {"domain": "pawplan.example", "type": "a", "name": "www", "content": "203.0.113.7"}
        self.assertIn("Queued for the owner's approval", tools.execute(ctx, "porkbun__set_dns_record", args))
        [row] = pending(c)
        p = json.loads(row["payload"])
        self.assertEqual((p["tag"], p.get("notice")), ("write", None))
        done = actions.decide(row["id"], "approve")   # dry run on: nothing changes
        self.assertTrue(done["result"].startswith("Dry run (nothing changed): Porkbun would create the A record "
                                                  "www.pawplan.example -> 203.0.113.7 (ttl 600)"), done["result"])
        self.assertEqual(porkbun.dns["pawplan.example"], [])
        # Dry run off, and connector_write on auto: it runs now, keyed randomly (no approval to replay).
        self.save("porkbun", None, {"dry_run": False}, id=connectors.instance("porkbun", c)["id"])
        set_policies(c, connector_write="auto")
        ctx = ctx_for(c, "engineering")
        tools.execute(ctx, "find_tool", {"query": "set dns record"})
        out = tools.execute(ctx, "porkbun__set_dns_record", args)
        self.assertTrue(out.startswith("Created the A record www.pawplan.example -> 203.0.113.7 (ttl 600), id "), out)
        self.assertEqual([r["content"] for r in porkbun.dns["pawplan.example"]], ["203.0.113.7"])
        self.assertTrue(porkbun.calls("/dns/create/")[-1]["idempotency_key"].startswith("holdco-"))
        self.assertIn("already points to 203.0.113.7", tools.execute(ctx, "porkbun__set_dns_record", args))
        out = tools.execute(ctx, "porkbun__set_dns_record", {**args, "content": "203.0.113.8", "name": "www."
                                                             "pawplan.example"})
        self.assertTrue(out.startswith("Updated the A record www.pawplan.example -> 203.0.113.8"), out)
        self.assertEqual([r["content"] for r in porkbun.dns["pawplan.example"]], ["203.0.113.8"])
        tools.execute(ctx, "find_tool", {"query": "list domains account"})
        listing = tools.execute(ctx, "porkbun__list_domains", {})
        self.assertIn("pawplan.example: active, expires 2027-10-01, auto-renew on", listing)
        bogus = tools.execute(ctx, "porkbun__set_dns_record", {**args, "type": "BOGUS"})
        self.assertTrue(bogus.startswith("FAILED: type must be one of A, AAAA"), bogus)


# ================================================================ Cloudflare

class TestCloudflare(Base):
    SITE = {"index.html": "<!doctype html><title>Paw</title><h1>PawPlan</h1>", "style.css": "h1{color:#123}",
            "about/index.html": "<h1>About</h1>"}

    def connect(self, cf, company, **cfg) -> dict:
        token = key("cftoken")
        out = self.save("cloudflare", company, {"api_token": token, "account_id": cf.account, "api_base": cf.url,
                                                **cfg})
        self.token = token
        return out

    def test_setup_card_and_secrets_go_to_the_vault(self):
        cf = self.fake(FakeCloudflare)
        c = make_company("CardCo")
        card = call("GET", "/api/connectors")["definitions"]["cloudflare"]
        self.assertEqual((card["scope"], card["kind"]), ("company", "api"))
        self.assertEqual([f["key"] for f in card["setup"]["fields"] if not f["hidden"]],
                         ["api_token", "account_id", "worker_name", "custom_domain"])
        self.assertIn("recommended target over Pages", card["setup"]["where_to_get_key"])
        self.assertEqual([(t["tool"], t["tag"]) for t in card["tools"]], [("deploy_status", "read")])
        self.assertFalse(card["has_test_mode"])
        out = self.connect(cf, c)
        self.assertEqual(out["health"], {"status": "ok",
                                         "detail": "token active, deploys go to <worker>.holdco-test.workers.dev"})
        self.assert_secret_in_vault_only(out["instance"]["id"], "api_token", self.token)
        verify = [r for r in cf.requests if r["path"] == "/user/tokens/verify"][-1]
        self.assertEqual(verify["auth"], self.token)
        # A rejected token, a missing account id and the wrong account.
        bad = self.save("cloudflare", c, {"api_token": "bad-token"}, id=out["instance"]["id"])
        self.assertEqual(bad["health"], {"status": "bad", "detail": "token rejected"})
        self.save("cloudflare", c, {"api_token": key("cftoken"), "account_id": ""}, id=out["instance"]["id"])
        self.assertEqual(connectors.check(connectors.get(out["instance"]["id"]))[0], "warn")
        wrong = self.save("cloudflare", c, {"account_id": "someone-else"}, id=out["instance"]["id"])
        self.assertEqual(wrong["health"]["status"], "bad")
        self.assertIn("may not use Workers", wrong["health"]["detail"])

    def test_cloudflare_deploy(self):
        cf = self.fake(FakeCloudflare)
        c = make_company("DeployCo", settings={"site_provider": "cloudflare"})
        write_site(c, self.SITE)
        inst = self.connect(cf, c)["instance"]
        self.assertEqual(conn_cloudflare.provider(db.get_company(c["id"])), "cloudflare")
        netlify = mock.patch.object(actions, "_deploy_netlify", side_effect=AssertionError("Netlify was used"))
        netlify.start()
        self.addCleanup(netlify.stop)

        # deploy_site is still the gate: it queues under its policy, then approval uploads site/ to the Worker.
        ctx = ctx_for(c, "engineering")
        self.assertIn("Queued for the owner's approval", tools.execute(ctx, "deploy_site", {}))
        self.assertEqual(cf.deployments, [])
        [row] = pending(c, "deploy_site")
        done = actions.decide(row["id"], "approve")
        self.assertEqual(done["status"], "done", done["result"])
        worker = f"{c['slug']}-site"
        url = f"https://{worker}.holdco-test.workers.dev"
        self.assertEqual(done["result"], f"Website deployed to Cloudflare Workers: {url} (3 files, 3 uploaded)")
        self.assertEqual(db.company_settings(db.get_company(c["id"]))["site_url"], url)
        self.assertEqual(cf.site(worker), {"/index.html": self.SITE["index.html"].encode(),
                                           "/style.css": self.SITE["style.css"].encode(),
                                           "/about/index.html": self.SITE["about/index.html"].encode()})
        dep = cf.deployments[-1]
        self.assertEqual(dep["metadata"]["bindings"], [{"type": "assets", "name": "ASSETS"}])
        self.assertIn("env.ASSETS.fetch", dep["module"])
        self.assertTrue(cf.subdomain_enabled[worker])
        self.assertTrue(all(r["auth"] in (self.token,) or r["auth"].startswith("jwt-session-")
                            for r in cf.requests))
        self.assertNotIn(self.token, json.dumps(db.get_company(c["id"])["settings"]))

        # The read tool: last deploy time and address.
        tools.execute(ctx, "find_tool", {"query": "cloudflare deploy status"})
        status = tools.execute(ctx, "cloudflare__deploy_status", {})
        self.assertTrue(status.startswith(f"Last Cloudflare deploy of the Worker {worker}: 20"), status)
        self.assertIn(f"Address: {url}.", status)
        self.assertEqual(pending(c), [])

        # A second deploy uploads only what changed; a custom domain is attached in its zone and becomes site_url.
        write_site(c, {"style.css": "h1{color:#456}"})
        self.save("cloudflare", c, {"custom_domain": "www.pawplan.example"}, id=inst["id"])
        out = actions.execute(db.get_company(c["id"]), "deploy_site", {"folder": "site"}, approval_id=None)
        self.assertEqual(out, "Website deployed to Cloudflare Workers: https://www.pawplan.example (3 files, "
                              "1 uploaded)")
        self.assertEqual(cf.domains[-1], {"environment": "production", "hostname": "www.pawplan.example",
                                          "service": worker, "zone_id": "zone-1"})
        self.assertEqual(db.company_settings(db.get_company(c["id"]))["site_url"], "https://www.pawplan.example")
        self.assertEqual(cf.site(worker)["/style.css"], b"h1{color:#456}")

        # Trust and taint still govern it: promoted, deploy_site runs on its own; a tainted session still asks.
        trust._save(c["id"], "deploy_site", trust.AUTO, 0, 0)
        ctx = ctx_for(c, "engineering")
        before = len(cf.deployments)
        self.assertTrue(tools.execute(ctx, "deploy_site", {}).startswith("Website deployed to Cloudflare Workers"))
        self.assertEqual(len(cf.deployments), before + 1)
        ctx.taint_sources.append("example.org")
        self.assertIn("Queued for the owner's approval", tools.execute(ctx, "deploy_site", {}))
        self.assertEqual(len(cf.deployments), before + 1)

        # site_provider netlify: the old path runs, Cloudflare is not touched.
        netlify.stop()
        db.update_company_settings(c["id"], site_provider="netlify")
        with mock.patch.object(actions, "_deploy_netlify", return_value="Website deployed: https://x.netlify.app") \
                as old:
            self.assertEqual(actions.execute(db.get_company(c["id"]), "deploy_site", {}),
                             "Website deployed: https://x.netlify.app")
        old.assert_called_once()
        self.assertEqual(len(cf.deployments), before + 1)
        netlify.start()

    def test_auto_follows_the_card_and_cloudflare_without_a_card_is_a_clear_error(self):
        cf = self.fake(FakeCloudflare)
        c = make_company("AutoCo")
        write_site(c, self.SITE)
        with mock.patch.object(actions, "_deploy_netlify", return_value="Website deployed: https://n.netlify.app"):
            # auto without a card: Netlify as today
            self.assertEqual(actions.execute(c, "deploy_site", {}), "Website deployed: https://n.netlify.app")
            inst = self.connect(cf, c)["instance"]
            # auto with a healthy card: Cloudflare
            self.assertTrue(actions.execute(c, "deploy_site", {}).startswith("Website deployed to Cloudflare"))
            # auto with a failing card: Netlify again
            db.update("connectors", inst["id"], status="bad")
            self.assertEqual(actions.execute(c, "deploy_site", {}), "Website deployed: https://n.netlify.app")
            # cloudflare chosen but turned off: a clear error, never a quiet fall back
            self.save("cloudflare", c, {}, id=inst["id"], enabled=False)
            db.update_company_settings(c["id"], site_provider="cloudflare")
            with self.assertRaises(RuntimeError) as err:
                actions.execute(db.get_company(c["id"]), "deploy_site", {})
            self.assertIn("no Cloudflare card turned on", str(err.exception))
        other = make_company("NoSiteCo", settings={"site_provider": "cloudflare"})
        self.connect(cf, other)
        with self.assertRaises(RuntimeError) as err:
            actions.execute(db.get_company(other["id"]), "deploy_site", {})
        self.assertEqual(str(err.exception), "site/index.html doesn't exist yet.")
        tools_ctx = ctx_for(other, "engineering")
        tools.execute(tools_ctx, "find_tool", {"query": "cloudflare deploy status"})
        self.assertIn("Nothing deployed to Cloudflare yet", tools.execute(tools_ctx, "cloudflare__deploy_status", {}))


# ================================================================ n8n

class TestN8n(Base):
    def test_n8n_trigger(self):
        secret = key("n8n-secret-")
        n8n = self.fake(FakeN8n, secret=secret)
        c = make_company("FlowCo", members=("ceo", "sales"))
        self.assertEqual(set(conn_n8n.DEFINITIONS), {"n8n", "n8n_mcp"})
        card = call("GET", "/api/connectors")["definitions"]["n8n"]
        self.assertEqual([(t["tool"], t["tag"]) for t in card["tools"]], [("trigger", "publish")])
        self.assertEqual([f["key"] for f in card["setup"]["fields"]], ["webhook_url", "webhook_secret"])

        # Health: a POST of {"ping": true} with the secret header, expecting a 2xx.
        out = self.save("n8n", None, {"webhook_url": n8n.hook_url, "webhook_secret": secret})
        self.assertEqual(out["health"], {"status": "ok", "detail": "the webhook answered HTTP 200"})
        self.assertEqual(n8n.received[-1]["body"], {"ping": True})
        self.assertEqual(n8n.received[-1]["headers"]["x-holdco-secret"], secret)
        iid = out["instance"]["id"]
        self.assert_secret_in_vault_only(iid, "webhook_secret", secret)
        wrong = self.save("n8n", None, {"webhook_secret": key("wrong-")}, id=iid)
        self.assertEqual(wrong["health"]["status"], "bad")
        self.assertIn("HTTP 403", wrong["health"]["detail"])
        self.save("n8n", None, {"webhook_secret": secret}, id=iid)

        # n8n__trigger queues under connector_publish; approval posts to the webhook with the secret header.
        ctx = ctx_for(c, "sales")
        found = tools.execute(ctx, "find_tool", {"query": "trigger n8n workflow"})
        self.assertIn("n8n__trigger [publish]", found)
        queued = tools.execute(ctx, "n8n__trigger", {"workflow": "add_to_crm",
                                                     "payload": {"email": "lead@example.com"}})
        self.assertIn("Queued for the owner's approval", queued)
        seen = len(n8n.received)
        [row] = pending(c)
        p = json.loads(row["payload"])
        self.assertEqual((p["connector"], p["tool"], p["tag"], p["company_id"]), ("n8n", "trigger", "publish",
                                                                                   c["id"]))
        self.assertEqual(len(n8n.received), seen)   # nothing sent before the owner said yes
        done = actions.decide(row["id"], "approve")
        self.assertEqual(done["status"], "done", done["result"])
        self.assertEqual(done["result"], "n8n ran 'add_to_crm' (HTTP 200). Its answer: "
                                         '{"message": "Workflow was started"}')
        got = n8n.received[-1]
        self.assertEqual(got["headers"]["x-holdco-secret"], secret)
        self.assertEqual({k: got["body"][k] for k in ("workflow", "payload", "company", "company_id", "approval_id")},
                         {"workflow": "add_to_crm", "payload": {"email": "lead@example.com"}, "company": "FlowCo",
                          "company_id": c["id"], "approval_id": row["id"]})
        # A workflow error fails the approval with the status.
        n8n.status = 500
        tools.execute(ctx, "n8n__trigger", {"workflow": "broken"})
        [row] = pending(c)
        failed = actions.decide(row["id"], "approve")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("HTTP 500", failed["result"])

    def test_n8n_mcp_tools_are_unclassified_and_ask(self):
        hosted = self.fake(FakeMCPHttp)
        c = make_company("McpFlowCo", members=("ceo", "marketing"))
        token = key("n8nmcp-")
        out = self.save("n8n_mcp", None, {"mcp_url": hosted.mcp_url, "mcp_token": token})
        self.assertEqual(out["health"]["status"], "ok", out["health"])
        self.assertIn("each one asks you", out["health"]["detail"])
        self.assert_secret_in_vault_only(out["instance"]["id"], "mcp_token", token)
        auth = {str(r["headers"].get("authorization")) for r in hosted.requests if r["method"] == "POST"}
        self.assertEqual(auth, {f"Bearer {token}"})
        ctx = ctx_for(c, "marketing")
        found = tools.execute(ctx, "find_tool", {"query": "echo text"})
        self.assertIn("n8n_mcp__echo [unclassified]", found)
        self.assertIn("Loaded: n8n_mcp__echo", found)
        self.assertIn("Queued for the owner's approval", tools.execute(ctx, "n8n_mcp__echo", {"text": "hi"}))
        [row] = pending(c)
        self.assertEqual(json.loads(row["payload"])["tag"], "unclassified")
        self.assertEqual([x for x in hosted.calls if x[0] == "tools/call"], [])   # nothing ran before the yes
        done = actions.decide(row["id"], "approve")
        self.assertEqual(done["status"], "done", done["result"])
        self.assertIn("hi", done["result"])


# ================================================================ the later tier

class TestLaterTier(Base):
    def test_later_tier_cards(self):
        listing = call("GET", "/api/connectors")
        for k, reason in LATER.items():
            with self.subTest(k):
                card = listing["definitions"][k]
                self.assertIs(card["available"], False)
                self.assertEqual(card["reason"], reason)
                self.assertEqual(conn_later.REASONS[k], reason)
                self.assertEqual((card["setup"]["fields"], card["tools"]), ([], []))
        self.assertTrue(all(d["available"] and d["reason"] == "" for k, d in listing["definitions"].items()
                            if k not in LATER))
        c = make_company("LaterCo")
        for k in LATER:
            with self.assertRaises(HTTPError) as err:
                self.save(k, c, {})
            self.assertEqual(err.exception.status, 400)
            self.assertIn("not available yet", str(err.exception))
        # Even a row put straight into the table is never offered, checked, polled or shown on the Doctor page.
        ids = [db.run("INSERT INTO connectors(company_id, key, enabled, config, status, detail, created_at) "
                      "VALUES(?,?,?,?,?,?,?)", (c["id"] if k != "comfyui" else None, k, 1, "{}", "", "", db.now()))
               for k in LATER]
        self.assertFalse({i["key"] for i in connectors.visible(c)} & set(LATER))
        self.assertFalse([e for e in connectors.catalog(c) if e["connector"] in LATER])
        ctx = ctx_for(c, "ceo")
        for query in ("generate image", "ads campaign", "accounting quickbooks"):
            self.assertNotIn("comfyui__", tools.execute(ctx, "find_tool", {"query": query}))
        self.assertIn("ERROR", connectors.call(ctx, "ads__create_campaign", {}))
        self.assertFalse([r for r in connectors.check_all() if r[0] in ids])
        self.assertFalse([r for r in connectors.doctor_rows() if r.get("instance_id") in ids])
        self.assertEqual([m for m in connectors.poll_metrics() if m["company_id"] == c["id"]], [])
        self.assertEqual(connectors.check(connectors.get(ids[0]))[0], "warn")
        self.assertNotIn("comfyui", connectors.prompt_section(c))


if __name__ == "__main__":
    unittest.main()
