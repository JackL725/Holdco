"""Phase 4 review: regression tests for the defects an independent review of the Scale phase found.

1. Two approvals of the same request at the same moment (dashboard, phone, MCP) ran it twice, and a rejection
   pressed while an approval was running it marked it rejected and filed a "Revise" task.
2. A Porkbun purchase queued while Dry run was on bought for real when the card changed before the approval; one
   queued with sandbox keys bought for real once live keys were saved.
3. The MCP decide tool answered a decision with text that is none of its options (a portfolio review's "Scale back"
   counted as Scale).
4. days_since_result counted a manual metric row between connector rows as a change.
5. money_spent_30d counted a purchase in Porkbun's sandbox.
6. JSON nested deeper than Python parses was an HTTP 500 at /mcp and ended the stdio proxy.
7. A JSON-RPC batch of any size ran in one request; ids of any JSON type were echoed.
8. A forms consent answer in words ("No thanks") was recorded as consent.
9. The starvation guard gave its slot to a company's first task even when that task waits on another one, so the
   company's ready work still never ran.

No network: the fakes run on 127.0.0.1.

Run with:  python -m unittest tests.test_phase4_review -v
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-phase4-review-"))
os.environ.setdefault("HOLDCO_VAULT_BACKEND", "file")   # never touch a developer's keyring

from holdco import (actions, conn_forms, conn_porkbun, connectors, contracts, db, decisions, mcp_server,  # noqa: E402
                    notify, portfolio, roles, routing, scheduler, server, tools, vault, workspace)
from tests.fake_porkbun import FakePorkbun  # noqa: E402
from tests.helpers import free_port  # noqa: E402

db.init()
assert server   # imported for its routes

_created: list[int] = []   # archived after each test, so other modules never see them


def setUpModule():
    if vault.backend() != "file":   # an earlier module chose another backend
        vault._reset()


def utc(days: float = 0) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_company(name: str, members=("ceo",), created_at: str | None = None) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, mission, status, stage, settings, created_at) "
                 "VALUES(?,?,?,?,?,?,?,?)", (name, slug, "An idea", "A mission", "active", "building",
                                             json.dumps({"truth_scan_done": True}), created_at or db.now()))
    _created.append(cid)
    workspace.create(db.get_company(cid))
    for m in members:
        roles.add_agent(cid, preset=m)
    return db.get_company(cid)


def agent_of(company: dict, role: str = "ceo") -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def ctx_for(company: dict, role: str = "ceo") -> tools.Ctx:
    agent = agent_of(company, role)
    c = tools.Ctx(company=db.get_company(company["id"]), agent=agent, kind="task", run_id=0, task=None,
                  settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(agent, "task")}
    return c


class _Handler:
    query: dict = {}


def route(method: str, path: str, body=None):
    """Dispatch like server.Handler does."""
    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(_Handler(), m, body)
    raise AssertionError(f"no route for {method} {path}")


def mcp_call(tool: str, **args) -> dict:
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": tool, "arguments": args}}, {"name": "review"})
    return reply["result"]


def http(body: bytes):
    return mcp_server.http_reply("POST", {"Content-Type": "application/json"}, body)


class Base(unittest.TestCase):
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


# ---------------------------------------------------------------- 1. one execution per request

class TestDecideOnce(Base):
    def pending(self, c: dict, action: str, p: dict) -> int:
        return db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, "
                      "created_at) VALUES(?,?,?,?,?,?,?,?)", (c["id"], agent_of(c)["id"], None, action,
                                                               actions._summary(action, p), json.dumps(p), "pending",
                                                               db.now()))

    def race(self, aid: int, *calls: tuple[str, dict]) -> list[str]:
        """actions.decide(aid, decision, **kw) for each call in its own thread. Every thread reads the row as pending,
        then they meet (at db.get_company) before any goes on, as two deciders pressing at the same moment do.
        Only these threads wait. Returns each outcome: the status, or "refused: <reason>"."""
        met = threading.Barrier(len(calls), timeout=30)
        local = threading.local()
        real = db.get_company

        def get_company(cid):
            if threading.current_thread().name.startswith("review-race") and not getattr(local, "met", False):
                local.met = True
                with contextlib.suppress(threading.BrokenBarrierError):
                    met.wait()
            return real(cid)

        outcomes: list[str] = []

        def decide(decision: str, kw: dict):
            try:
                outcomes.append(actions.decide(aid, decision, **kw)["status"])
            except ValueError as e:
                outcomes.append(f"refused: {e}")

        with mock.patch.object(db, "get_company", get_company), contextlib.redirect_stdout(io.StringIO()):
            threads = [threading.Thread(target=decide, args=call, name=f"review-race-{i}")
                       for i, call in enumerate(calls)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        return outcomes

    def test_two_approvals_at_the_same_moment_run_the_request_once(self):
        c = make_company("RaceCo")
        aid = self.pending(c, "spend_money", {"item": "domain raceco.example", "amount_usd": 12, "vendor": "Example"})
        outcomes = self.race(aid, ("approve", {"decided_by": "owner"}), ("approve", {"decided_by": "mcp"}))
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM audit WHERE approval_id=?", (aid,)), 1, outcomes)
        self.assertEqual(sorted(o.split(":")[0] for o in outcomes), ["done", "refused"], outcomes)
        self.assertIn(f"Request #{aid} is already", [o for o in outcomes if o.startswith("refused")][0])

    def test_a_rejection_at_the_same_moment_as_an_approval_does_not_land(self):
        c = make_company("RejectRaceCo")
        aid = self.pending(c, "spend_money", {"item": "logo pack", "amount_usd": 20, "vendor": "Example"})
        outcomes = self.race(aid, ("approve", {"decided_by": "mcp"}),
                             ("reject", {"feedback": "Not this one", "decided_by": "telegram"}))
        status = db.one("SELECT status FROM approvals WHERE id=?", (aid,))["status"]
        self.assertEqual(sorted(o.split(":")[0] for o in outcomes), sorted([status, "refused"]), outcomes)
        audited = db.scalar("SELECT COUNT(*) FROM audit WHERE approval_id=?", (aid,))
        revise = db.scalar("SELECT COUNT(*) FROM tasks WHERE company_id=? AND title LIKE 'Revise:%'", (c["id"],))
        # Whichever came first stands alone: run and audited, or rejected with its revision task, never both.
        self.assertEqual((audited, revise), (1, 0) if status == "done" else (0, 1), outcomes)


# ---------------------------------------------------------------- 2. what the purchase card promised

class TestPorkbunHonoursTheCard(Base):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.dict(os.environ, {"HOLDCO_ALLOW_LOCAL": "127.0.0.1:*"}),
                  mock.patch.object(conn_porkbun, "RETRY_WAIT", 0)):
            p.start()
            self.addCleanup(p.stop)
        conn_porkbun.forget_modes()
        self.addCleanup(conn_porkbun.forget_modes)
        self.porkbun = FakePorkbun().start()
        self.addCleanup(self.porkbun.stop)
        self.iid = None
        self.addCleanup(self._remove)

    def _remove(self):
        if self.iid:
            with contextlib.redirect_stdout(io.StringIO()):
                connectors.remove(self.iid)

    def save(self, cfg: dict) -> None:
        body = {"key": "porkbun", "company_id": None, "config": cfg, **({"id": self.iid} if self.iid else {})}
        self.iid = route("POST", "/api/connectors", body)["instance"]["id"]

    def queue(self, c: dict, domain: str) -> dict:
        ctx = ctx_for(c)
        tools.execute(ctx, "find_tool", {"query": "register domain buy"})
        self.assertIn("Queued for the owner's approval",
                      tools.execute(ctx, "porkbun__register_domain", {"domain": domain}))
        return db.one("SELECT * FROM approvals WHERE company_id=? AND action='connector_call' AND status='pending'",
                      (c["id"],))

    def test_a_request_queued_in_dry_run_stays_a_dry_run(self):
        c = make_company("DryCo")
        self.save({"api_key": f"pk1_sb_{secrets.token_hex(8)}", "secret_key": f"sk1_sb_{secrets.token_hex(8)}",
                   "api_base": self.porkbun.url})
        row = self.queue(c, "dryco.co")
        p = json.loads(row["payload"])
        self.assertIs(p["dry_run"], True)
        self.assertIn("nothing is bought", p["notice"])
        self.save({"dry_run": False})   # the owner unticks Dry run before approving the old request
        done = actions.decide(row["id"], "approve")
        self.assertEqual(done["status"], "done", done["result"])
        self.assertTrue(done["result"].startswith("Dry run (nothing bought)"), done["result"])
        self.assertEqual(self.porkbun.registered, [])
        self.assertIs(self.porkbun.calls("/domain/create/dryco.co")[-1]["body"].get("dryRun"), True)

    def test_a_request_queued_with_sandbox_keys_never_buys_with_live_ones(self):
        c = make_company("SandCo")
        self.save({"api_key": f"pk1_sb_{secrets.token_hex(8)}", "secret_key": f"sk1_sb_{secrets.token_hex(8)}",
                   "api_base": self.porkbun.url, "dry_run": False})
        row = self.queue(c, "sandco.co")
        self.assertEqual(json.loads(row["payload"])["mode"], "sandbox")
        self.save({"api_key": f"pk1_{secrets.token_hex(8)}", "secret_key": f"sk1_{secrets.token_hex(8)}"})
        done = actions.decide(row["id"], "approve")
        self.assertEqual(done["status"], "failed", done["result"])
        self.assertIn("sandbox keys", done["result"])
        self.assertEqual(self.porkbun.registered, [])
        self.assertEqual(self.porkbun.calls("/domain/create/sandco.co"), [])


# ---------------------------------------------------------------- 3. MCP decide takes an option

class TestMcpDecideOptions(Base):
    def test_a_decision_is_answered_with_one_of_its_options(self):
        c = make_company("OptCo")
        out = decisions.create(c, portfolio.HOLDCO, None, "OptCo: scale, hold or shut down?",
                               list(portfolio.OPTIONS.values()), portfolio.OPTIONS["hold"], deadline_hours=168,
                               origin={"kind": "portfolio", "brief_id": None, "recommendation": "hold"})
        did = int(re.search(r"decision #(\d+)", out, re.I).group(1))
        for text in ("Scale back, hold for now", "Shut it all down", "Maybe", "sh", "s"):
            res = mcp_call("decide", approval_id=did, decision="answer", answer=text)
            self.assertTrue(res["isError"], res)
            self.assertIn("Hold", res["content"][0]["text"])   # the options are named
        self.assertEqual(db.one("SELECT status FROM approvals WHERE id=?", (did,))["status"], "pending")
        self.assertEqual(db.company_settings(db.get_company(c["id"])).get("allocation_boost_until") or "", "")
        # reject dismisses without choosing: an answer sent along is not taken as the choice
        other = decisions.create(c, portfolio.HOLDCO, None, "OptCo again: scale, hold or shut down?",
                                 list(portfolio.OPTIONS.values()), portfolio.OPTIONS["hold"], deadline_hours=168,
                                 origin={"kind": "portfolio", "brief_id": None, "recommendation": "hold"})
        oid = int(re.search(r"decision #(\d+)", other, re.I).group(1))
        self.assertNotEqual(oid, did, other)
        res = mcp_call("decide", approval_id=oid, decision="reject", answer="Scale it up")
        self.assertFalse(res["isError"], res)
        self.assertEqual(db.one("SELECT status, result FROM approvals WHERE id=?", (oid,)), {"status": "rejected",
                                                                                           "result": ""})
        self.assertEqual(db.company_settings(db.get_company(c["id"])).get("allocation_boost_until") or "", "")
        # The start of exactly one option (or the option in any case) is that option.
        res = mcp_call("decide", approval_id=did, decision="answer", answer="scale")
        self.assertFalse(res["isError"], res)
        row = db.one("SELECT status, result, decided_by FROM approvals WHERE id=?", (did,))
        self.assertEqual((row["status"], row["result"], row["decided_by"]),
                         ("answered", portfolio.OPTIONS["scale"], "mcp"))
        self.assertTrue(db.company_settings(db.get_company(c["id"])).get("allocation_boost_until"))

    def test_a_team_decision_too_and_questions_keep_free_text(self):
        c = make_company("TeamOptCo", members=("ceo", "marketing"))
        out = decisions.create(c, agent_of(c, "marketing"), None, "Which price should we test first for the plan?",
                               ["$9 a month", "$19 a month"], "$9 a month", deadline_hours=48)
        did = int(re.search(r"decision #(\d+)", out, re.I).group(1))
        res = mcp_call("decide", approval_id=did, decision="answer", answer="$999 a month")
        self.assertTrue(res["isError"], res)
        res = mcp_call("decide", approval_id=did, decision="answer", answer="$19 A MONTH")
        self.assertFalse(res["isError"], res)
        self.assertEqual(db.one("SELECT result FROM approvals WHERE id=?", (did,))["result"], "$19 a month")
        qid = db.run("INSERT INTO approvals(company_id, agent_id, action, summary, payload, status, created_at) "
                     "VALUES(?,?,?,?,?,?,?)", (c["id"], agent_of(c)["id"], "question", "Which day?",
                                               json.dumps({"question": "Which day suits the launch?"}), "pending",
                                               db.now()))
        res = mcp_call("decide", approval_id=qid, decision="answer", answer="Tuesday, after lunch")
        self.assertFalse(res["isError"], res)


# ---------------------------------------------------------------- 4 and 5. the scorecard

class TestScorecard(Base):
    def metric(self, c: dict, name: str, value: float, source: str, days_ago: float) -> None:
        db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, source, created_at) VALUES(?,?,?,?,?,?,?)",
               (c["id"], None, name, value, "", source, utc(-days_ago)))

    def row(self, c: dict) -> dict:
        return next(r for r in portfolio.scorecard() if r["id"] == c["id"])

    def test_a_manual_row_neither_makes_nor_hides_a_result(self):
        c = make_company("ManualCo", created_at=utc(-90))
        self.metric(c, "analytics.visitors", 100, "plausible", 40)
        self.metric(c, "analytics.visitors", 100, "plausible", 30)
        self.metric(c, "analytics.visitors", 55, "manual", 2)      # an agent typed a number
        self.metric(c, "analytics.visitors", 100, "plausible", 1)  # the connector's number did not move
        self.assertAlmostEqual(self.row(c)["days_since_result"], 40, delta=0.2)   # its first number (from 0)
        d = make_company("HiddenCo", created_at=utc(-90))
        self.metric(d, "forms.signups", 3, "forms", 20)
        self.metric(d, "forms.signups", 5, "", 4)                  # manual, the same number the connector brings
        self.metric(d, "forms.signups", 5, "forms", 3)             # a real change from 3
        self.assertAlmostEqual(self.row(d)["days_since_result"], 3, delta=0.2)

    def test_money_spent_leaves_out_sandbox_purchases(self):
        c = make_company("SpendCo")

        def bought(result: str, amount: float, mode: str):
            p = {"connector": "porkbun", "tool": "register_domain", "args": {"domain": "x.co"}, "tag": "spend",
                 "amount_usd": amount, "dry_run": False, "mode": mode}
            db.run("INSERT INTO approvals(company_id, agent_id, action, summary, payload, status, result, created_at, "
                   "decided_at) VALUES(?,?,?,?,?,?,?,?,?)", (c["id"], None, "connector_call", "buy", json.dumps(p),
                                                             "done", result, db.now(), db.now()))
        bought("Registered sandco.co for $10.81 (1 year, order 1001, sandbox mode). It cannot be undone.", 10.81,
               "sandbox")
        bought("Registered realco.co for $9.50 (1 year, order 1002, live mode). It cannot be undone.", 9.5, "live")
        self.assertEqual(portfolio._money_spent(c["id"], "2000-01-01T00:00:00Z"), 9.5)
        self.assertEqual(self.row(c)["money_spent_30d"], 9.5)


# ---------------------------------------------------------------- 9. the starvation guard

class TestStarvationGuard(Base):
    def task(self, c: dict, title: str, priority: int = 2, **cols) -> int:
        row = {"company_id": c["id"], "title": title, "role": "ceo", "agent_id": agent_of(c)["id"], "status": "todo",
               "priority": priority, "created_by": "Ada", "created_at": db.now(), "updated_at": db.now(), **cols}
        return db.run(f"INSERT INTO tasks({', '.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))

    def test_a_starving_company_gets_its_first_task_that_can_start(self):
        rich, poor = make_company("RichCo", created_at=utc(-40)), make_company("PoorCo", created_at=utc(-40))
        for _ in range(5):   # Rich ran a lot in the last hour; Poor not at all
            db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at, finished_at) "
                   "VALUES(?,?,?,?,?,?)", (rich["id"], agent_of(rich)["id"], "task", "done", utc(-0.005), utc(-0.004)))
        rich_tasks = [self.task(rich, f"Rich work {i}") for i in range(3)]
        held = self.task(poor, "Waits on the owner", status="waiting")
        blocked = self.task(poor, "Needs the waiting one", priority=1, depends_on=json.dumps([held]))
        ready = self.task(poor, "Can start now")
        shares = {rich["id"]: {"share": 1.0, "cycle_minutes": 60.0}, poor["id"]: {"share": 0.0, "cycle_minutes": 60.0}}
        queue = [t for t in scheduler._queue() if t["company_id"] in shares]
        self.assertEqual(queue[0]["id"], blocked)   # Phase 3 order: priority 1 first
        with mock.patch.object(portfolio, "allocation", lambda need=(): shares):
            ordered = [t["id"] for t in portfolio.order(queue, {"allocation_mode": "weighted"})]
            # the scheduler hands over its own dependency lookup for the tick (Phase 2: one lookup per tick)
            with mock.patch.object(contracts, "dependency_status", side_effect=AssertionError("a second lookup")):
                again = [t["id"] for t in portfolio.order(queue, {"allocation_mode": "weighted"},
                                                          {held: "waiting"})]
        self.assertEqual(sorted(ordered), sorted(t["id"] for t in queue))
        self.assertLess(ordered.index(ready), min(ordered.index(t) for t in rich_tasks), ordered)
        self.assertEqual(again, ordered)


# ---------------------------------------------------------------- 6 and 7. JSON-RPC at the edges

class TestJsonRpcEdges(Base):
    DISCOVER = {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}

    def test_json_nested_too_deep_is_a_parse_error(self):
        status, reply, _ = http(b"[" * 100000 + b"]" * 100000)
        self.assertEqual(status, 400)
        self.assertEqual(reply["error"]["code"], -32700)
        # The stdio proxy answers the same and keeps reading.
        out = io.BytesIO()
        lines = b"[" * 100000 + b"\n" + json.dumps(self.DISCOVER).encode() + b"\n"
        proxy = mcp_server._Proxy(free_port(), "", io.BytesIO(lines), out)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(proxy.run(), 0)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "holdco")

    def test_a_batch_has_a_size_limit(self):
        status, reply, _ = http(json.dumps([dict(self.DISCOVER, id=i) for i in range(mcp_server.MAX_BATCH)]).encode())
        self.assertEqual((status, len(reply)), (200, mcp_server.MAX_BATCH))
        status, reply, _ = http(json.dumps([dict(self.DISCOVER, id=i)
                                            for i in range(mcp_server.MAX_BATCH + 1)]).encode())
        self.assertEqual(status, 400)
        self.assertEqual(reply["error"]["code"], -32600)
        out = io.BytesIO()
        batch = json.dumps([dict(self.DISCOVER, id=i) for i in range(mcp_server.MAX_BATCH + 1)]).encode() + b"\n"
        proxy = mcp_server._Proxy(free_port(), "", io.BytesIO(batch), out)
        with contextlib.redirect_stderr(io.StringIO()):
            proxy.run()
        self.assertEqual(json.loads(out.getvalue().splitlines()[0])["error"]["code"], -32600)

    def test_an_id_is_a_string_a_number_or_null(self):
        for rid in ({"a": 1}, [1], True, False):
            reply = mcp_server.handle(dict(self.DISCOVER, id=rid))
            self.assertEqual((reply["id"], reply["error"]["code"]), (None, -32600), rid)
        status, reply, _ = http(b'{"jsonrpc": "2.0", "id": NaN, "method": "server/discover"}')
        self.assertEqual((reply["id"], reply["error"]["code"]), (None, -32600))
        for rid in ("x-1", 7, 2.5, None):
            reply = mcp_server.handle(dict(self.DISCOVER, id=rid))
            self.assertEqual(reply["id"], rid)
            self.assertIn("result", reply)
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


# ---------------------------------------------------------------- 8. consent in words

class TestFormsConsent(unittest.TestCase):
    def test_an_answer_that_says_no_is_not_consent(self):
        for said in ("No thanks", "no, thank you", "Not now", "I do not agree", "I don't consent", "Decline",
                     "Opt out", "nope"):
            self.assertEqual(conn_forms.consent_of({"consent": said}, True), "no", said)
        for said in ("on", "yes", "Yes, email me", "I agree to emails", "Notify me about the launch"):
            self.assertEqual(conn_forms.consent_of({"marketing_consent": said}, True), "yes", said)


if __name__ == "__main__":
    unittest.main()
