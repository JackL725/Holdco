"""Phase 4 integration: where the four builders' work meets each other and Phases 1 to 3 (the MCP server, portfolio
allocation and the new connectors against the decisions queue, the briefs API, the brain, the phone and the weekly
review). Each test fails on the merged branch before its fix. Only the outside world is faked: the phone.

Run with:  python -m unittest tests.test_phase4_seams -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-phase4-seams-"))
os.environ.setdefault("HOLDCO_VAULT_BACKEND", "file")   # never touch a developer's keyring

from holdco import (actions, agents, brain, config, connectors, db, decisions, mcp_server, notify, planning,  # noqa: E402
                    portfolio, rhythms, roles, routing, server, workspace)
from holdco.routing import HTTPError  # noqa: E402

db.init()

_created: list[int] = []   # archived after each test, so other modules' scheduler checks never see them
HOLDCO = {"id": None, "name": "Holdco"}
REVIEW_OPTIONS = list(portfolio.OPTIONS.values())


def make_company(name: str = "SeamCo", members=("ceo", "marketing")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "Meal plans for dogs", "active", "building", json.dumps({"truth_scan_done": True}),
                  db.now()))
    _created.append(cid)
    workspace.create(db.get_company(cid))
    for m in members:
        roles.add_agent(cid, preset=m)
    return db.get_company(cid)


def agent_of(company: dict, role: str = "ceo") -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def call(method: str, path: str, query: dict | None = None, body=None):
    """Dispatch to a dashboard route the way server.Handler does, with a query string."""
    class Handler:
        pass
    Handler.query = {k: [str(v)] for k, v in (query or {}).items()}
    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.fullmatch(path)):
            return fn(Handler(), m, body)
    raise AssertionError(f"no route for {method} {path}")


def approval(company: dict, action: str, payload: dict, status: str = "done", result: str = "",
             agent_id=None) -> int:
    return db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, result, "
                  "created_at, decided_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (company["id"], agent_id, None, action, actions._summary(action, payload), json.dumps(payload),
                   status, result, db.now(), db.now() if status != "pending" else None))


class Base(unittest.TestCase):
    def setUp(self):
        self.phone: list = []
        self._patches = [mock.patch.object(notify, "approval_requested", lambda row: self.phone.append(row)),
                         mock.patch.object(notify, "send_later", lambda *a, **kw: None)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        while _created:
            db.update("companies", _created.pop(), status="archived")


class TestRoutes(unittest.TestCase):
    def test_the_server_registers_the_portfolio_route_itself(self):
        # GET /api/portfolio existed only once something imported the scheduler; the server imports portfolio now.
        code = ("import sys; import holdco.server, holdco.routing as r; "
                "assert 'holdco.scheduler' not in sys.modules; "
                "print(any(m == 'GET' and rx.match('/api/portfolio') for m, rx, fn in r.ROUTES))")
        env = {**os.environ, "HOLDCO_DATA": tempfile.mkdtemp(prefix="holdco-phase4-seams-import-")}
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True,
                             timeout=120)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "True"), out.stderr[-2000:])


class TestBoardDecisions(Base):
    def test_board_decisions_neither_fill_the_cap_nor_take_in_a_question(self):
        c = make_company()
        ceo = agent_of(c)
        # Holdco's own decisions for the board: the monthly review's and a trust promotion
        review = decisions.create(c, HOLDCO, None, "SeamCo: scale, hold or shut down?", REVIEW_OPTIONS, "Hold",
                                  deadline_hours=168, origin={"kind": "portfolio", "brief_id": 1})
        promote = decisions.create(c, HOLDCO, None, "Let agents post on social media on their own, up to 3 a day?",
                                   ["Yes, up to 3 a day", "Yes, always", "No, keep asking"], "Yes, up to 3 a day",
                                   deadline_hours=168, origin={"kind": "trust", "action": "social_post", "cap": 3})
        self.assertTrue(review.startswith("Decision #") and promote.startswith("Decision #"), (review, promote))
        self.assertEqual(planning.pending_agent_questions(c["id"]), [])
        # ...so the team still has its three: a question about posting is not folded into the trust promotion
        asks = ["Should we post on social media every day or three times a week?",
                "Should we price the starter plan at $29 or $49 a month?",
                "Should we launch first on r/dogs or on the PawTalk forum?"]
        for q in asks:
            out = decisions.create(c, ceo, None, q, ["The first", "The second"], "The first")
            self.assertTrue(out.startswith("Decision #"), out)
        self.assertEqual(len(planning.pending_agent_questions(c["id"])), 3)
        fourth = decisions.create(c, ceo, None, "Should the newsletter go out on Mondays or on Fridays?",
                                  ["Mondays", "Fridays"], "Mondays")
        self.assertTrue(fourth.startswith("ERROR: 3 questions from the team are already waiting"), fourth)

    def test_the_phone_says_holdco_asks_for_a_board_decision(self):
        c = make_company()
        decisions.create(c, HOLDCO, None, "SeamCo: scale, hold or shut down?", REVIEW_OPTIONS, "Hold",
                         deadline_hours=168, origin={"kind": "portfolio", "brief_id": 1})
        text, buttons, kind = notify.build_approval(self.phone[-1])
        self.assertTrue(text.startswith(f"<b>Holdco</b> asks you to decide about <b>SeamCo</b> (decision "
                                        f"#{self.phone[-1]['id']}):"), text)
        self.assertEqual([b[0]["text"] for b in buttons], [REVIEW_OPTIONS[0], notify.CHECK + "Hold", REVIEW_OPTIONS[2]])
        # an agent's own decision still names the agent
        decisions.create(c, agent_of(c), None, "Should we launch first on r/dogs or on the PawTalk forum?",
                         ["r/dogs", "PawTalk"], "r/dogs")
        text, _, _ = notify.build_approval(self.phone[-1])
        self.assertIn(f"<b>{agent_of(c)['name']}</b> at <b>SeamCo</b> asks you to decide", text)


class TestBriefs(Base):
    def test_the_portfolio_review_is_a_kind_the_api_and_mcp_can_ask_for(self):
        bid = db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
                     (None, "portfolio", "Review body", "{}", db.now()))
        try:
            got = call("GET", "/api/briefs", {"company_id": 0, "kind": "portfolio"})["briefs"]
            self.assertEqual(got[0]["id"], bid)
            with self.assertRaises(HTTPError) as e:
                call("GET", "/api/briefs", {"kind": "yearly"})
            self.assertEqual(e.exception.status, 400)
            self.assertEqual(call("GET", "/api/briefs/latest", {"company_id": 0})["portfolio"]["id"], bid)
            reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "latest_brief", "arguments": {"kind": "portfolio"}}})
            self.assertFalse(reply["result"]["isError"], reply)
            self.assertEqual(reply["result"]["structuredContent"]["brief"]["id"], bid)
        finally:
            db.run("DELETE FROM briefs WHERE id=?", (bid,))


class TestMcpDecider(Base):
    def test_a_decision_answered_through_mcp_reads_as_the_owners(self):
        self.assertIn("mcp", brain.DECIDERS)
        c = make_company()
        out = decisions.create(c, agent_of(c), None, "Should we launch first on r/dogs or on the PawTalk forum?",
                               ["r/dogs", "PawTalk"], "r/dogs")
        aid = int(out.split("#")[1].split()[0])
        actions.decide(aid, "approve", answer="PawTalk", decided_by="mcp", approved_by="mcp:desktop")
        row = db.one("SELECT * FROM decisions WHERE company_id=? ORDER BY id DESC LIMIT 1", (c["id"],))
        self.assertEqual(row["decided_by"], "mcp")
        self.assertTrue(brain.decision_line(row).endswith("Decided by the owner (MCP client)."), brain.decision_line(row))
        # the phone's button, pressed after Claude Desktop answered, says who was first
        with self.assertRaises(ValueError) as e:
            actions.decide(aid, "approve", answer="r/dogs", decided_by="telegram")
        self.assertEqual(str(e.exception), f"Request #{aid} is already answered (through an MCP client)")


class TestMoneySpent(Base):
    def test_real_domain_purchases_count_and_dry_runs_do_not(self):
        c = make_company()
        buy = {"connector": "porkbun", "tool": "register_domain", "tag": "spend", "tags": ["spend", "irreversible"],
               "args": {"domain": "pawplan.co", "years": 1, "price_usd": 10.81}, "amount_usd": 10.81}
        approval(c, "spend_money", {"item": "logo", "amount_usd": 12})
        approval(c, "connector_call", {**buy, "dry_run": True},
                 result="Dry run (nothing bought): Porkbun would register pawplan.co for $10.81 (1 year).")
        approval(c, "connector_call", {**buy, "dry_run": False},
                 result="Registered pawplan.co for $10.81 (1 year, live mode). It cannot be undone.")
        # queued while dry run was on, approved after the owner turned it off: the executor's answer decides
        approval(c, "connector_call", {**buy, "amount_usd": 9.5, "dry_run": True},
                 result="Registered pawplan.de for $9.50 (1 year, live mode). It cannot be undone.")
        approval(c, "connector_call", {**buy, "dry_run": False}, status="failed", result="Porkbun said no")
        approval(c, "connector_call", {"connector": "n8n", "tool": "trigger", "tag": "publish", "args": {}})
        self.assertEqual(portfolio._money_spent(c["id"], "2000-01-01T00:00:00Z"), 12 + 10.81 + 9.5)


class TestPhone(Base):
    def test_the_domain_purchase_shows_its_price_warning_and_dry_run(self):
        c = make_company()
        notice = ("Buys pawplan.co for $10.81 (1 year) at Porkbun. This buys a domain and cannot be undone. Dry run "
                  "is on: approving only shows what Porkbun would do; nothing is bought.")
        p = {"connector_id": 1, "connector": "porkbun", "tool": "register_domain", "tag": "spend",
             "tags": ["spend", "irreversible"], "args": {"domain": "pawplan.co", "years": 1, "price_usd": 10.81},
             "amount_usd": 10.81, "dry_run": True, "notice": notice, "_why_asked": notice, "company_id": c["id"]}
        aid = approval(c, "connector_call", p, status="pending", agent_id=agent_of(c)["id"])
        text, buttons, kind = notify.build_approval(db.one("SELECT * FROM approvals WHERE id=?", (aid,)))
        self.assertIn(f"<b>{notify.escape(notice)}</b>", text)
        self.assertIn("Porkbun: register_domain [spend, irreversible]", text)
        self.assertIn("Price: $10.81", text)
        self.assertIn("Dry run: on.", text)
        self.assertEqual([b["text"] for b in buttons[0]], ["Approve", "Reject"])
        live = {**p, "dry_run": False, "notice": notice.split(" Dry run")[0]}
        aid = approval(c, "connector_call", live, status="pending", agent_id=agent_of(c)["id"])
        text, _, _ = notify.build_approval(db.one("SELECT * FROM approvals WHERE id=?", (aid,)))
        self.assertIn("This buys a domain and cannot be undone.", text)
        self.assertIn("<b>Dry run: off. Approving makes it real.</b>", text)


class TestOwnerTasksFromMcp(Base):
    def test_a_task_an_mcp_client_created_is_the_owners(self):
        self.assertIn(mcp_server.CREATED_BY, config.OWNER_CREATORS)
        c = make_company()
        mkt = agent_of(c, "marketing")

        def task(title, created_by):
            return db.run("INSERT INTO tasks(company_id, title, role, agent_id, status, priority, created_by, "
                          "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                          (c["id"], title, "marketing", mkt["id"], "todo", 2, created_by, db.now(), db.now()))
        mine, theirs = task("Sketch a treat line", mcp_server.CREATED_BY), task("Tidy the drive", mkt["name"])
        # the weekly review cancels an agent's task without a goal, never the owner's
        self.assertEqual(rhythms.after_weekly(db.get_company(c["id"])), [theirs])
        self.assertEqual(db.get_task(mine)["status"], "todo")
        self.assertEqual(agents._goal_label(db.get_task(mine), {}), "No goal (owner's task)")


class TestConnectorKinds(unittest.TestCase):
    def test_an_instance_shows_the_transport_its_mode_picks(self):
        d = connectors.DEFINITIONS["github"]
        self.assertEqual(connectors._kind(d, {"config": {"mode": "docker"}}), "mcp-stdio")
        self.assertEqual(connectors._kind(d, {"config": {"mode": "hosted"}}), "mcp-http")
        self.assertEqual(connectors._kind(connectors.DEFINITIONS["porkbun"], {"config": {}}), "api")
        # the transport and the price hook live on different definitions and both are read by the framework
        self.assertTrue(callable(connectors.DEFINITIONS["porkbun"].get("prepare_call")))
        self.assertNotIn("prepare_call", d)
        # what GET /api/connectors shows for an instance (turned off here, and removed again at once)
        iid = db.run("INSERT INTO connectors(company_id, key, enabled, config, created_at) VALUES(?,?,?,?,?)",
                     (None, "github", 0, json.dumps({"mode": "docker"}), db.now()))
        try:
            self.assertEqual(connectors.public(connectors.get(iid))["kind"], "mcp-stdio")
        finally:
            db.run("DELETE FROM connectors WHERE id=?", (iid,))


class TestSettingsMcp(unittest.TestCase):
    def test_settings_name_the_mcp_entry_point_without_the_password(self):
        with mock.patch.object(config, "PASSWORD", "pw-not-shown"):
            out = call("GET", "/api/settings")
            self.assertEqual(out["mcp"], server.mcp_info())
        mcp = out["mcp"]
        self.assertEqual((mcp["port"], mcp["password_set"], mcp["install_dir"]),
                         (config.PORT, True, str(config.BASE_DIR)))
        self.assertEqual(mcp["url"], f"http://127.0.0.1:{config.PORT}/mcp")
        self.assertEqual((mcp["command"], mcp["args"]), ("python", ["-m", "holdco.mcp_server"]))
        self.assertNotIn("pw-not-shown", json.dumps(out))
        with mock.patch.object(config, "PASSWORD", ""):
            self.assertFalse(call("GET", "/api/settings")["mcp"]["password_set"])


if __name__ == "__main__":
    unittest.main()
