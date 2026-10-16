"""Phase 2: regression tests for the findings of the independent review.

One class per finding, in the review's order:
 1. the dashboard's Markdown never lets a link's address open a second link inside it (an injected attribute);
 2. write_brief's number check sees scaled numbers ($1.5k, 10M, 3x) and money that looks like a year ($2000);
 3. a strategy change routed to the owner follows the rules for owner questions: the claims gate, one decision per
    field, and the cap on the team's pending questions and decisions;
 4. a brief rhythm (evening brief, weekly review, monthly check) cannot finish as done without its brief;
 5. a strategy proposal whose decision the owner dismissed is rejected, not left waiting on the owner for good;
 6. a contact proposal's email address stays out of the digest and out of brain_lookup for roles without email;
 7. the planning prompt's "no goal" line lists exactly what the weekly review cancels (never a reply to an inbound
    email), and a brain proposal's line in it is capped;
 8. a metrics row with an empty source is self-reported in the weekly data, as it is everywhere else;
 9. the owner and the deadline answering one decision at the same moment record it once;
10. send_email refuses a contact the brain marks unsubscribed.

Only the outside world is faked: the phone (notify.send_later) and, for the race, the moment two answers meet.

Run with:  python -m unittest tests.test_phase2_review -v
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-phase2-review-"))

from holdco import (actions, agents, brain, contracts, db, decisions, notify, presets, rhythms, roles,  # noqa: E402
                    server, tools, workspace)

db.init()
assert server   # imported for its routes

_created: list[int] = []   # archived after each test, so other modules that scan every company never see them


def make_company(name="ReviewCo", members=("ceo", "engineering", "marketing", "sales", "design")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, charter, settings, created_at) "
                 "VALUES(?,?,?,?,?,?,?,?)", (name, slug, "Meal plans for dogs", "active", "building", "",
                                             json.dumps({"truth_scan_done": True}), db.now()))
    _created.append(cid)
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def agent_of(company, role="ceo") -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def ctx_for(company, role="ceo", task=None, kind="task") -> tools.Ctx:
    a = agent_of(company, role)
    c = tools.Ctx(company=db.get_company(company["id"]), agent=a, kind=kind, run_id=0, task=task,
                  settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(a, kind)}
    return c


def run(ctx, name, /, **args) -> str:
    return tools.execute(ctx, name, args)


def add_task(company, title, created_by="Atlas", goal_id=None, role="engineering") -> dict:
    agent = agent_of(company, role)
    ts = db.now()
    tid = db.run("INSERT INTO tasks(company_id, title, role, agent_id, status, priority, created_by, goal_id, "
                 "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (company["id"], title, role, agent["id"], "todo", 2, created_by, goal_id, ts, ts))
    return db.get_task(tid)


def pending_decisions(company) -> list[dict]:
    return db.q("SELECT * FROM approvals WHERE company_id=? AND action='decision' AND status='pending' ORDER BY id",
                (company["id"],))


class Base(unittest.TestCase):
    def setUp(self):
        self._kill = db.get_settings().get("kill_switch")
        db.set_settings({"kill_switch": False})
        self.sent: list = []
        self._patches = [mock.patch.object(notify, "send_later", lambda text, buttons=None, kind="info", **kw:
                                           self.sent.append((text, kind))),
                         mock.patch.object(notify, "send", lambda text, buttons=None, kind="info", **kw:
                                           self.sent.append((text, kind)) or True)]
        for p in self._patches:
            p.start()
        self.c = make_company()
        brain._upsert_strategy(self.c["id"], {f: "set" for f in brain.STRATEGY_FIELDS})

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        db.set_settings({"kill_switch": bool(self._kill)})
        while _created:
            db.update("companies", _created.pop(), status="archived")


# ================================================================ 1. Markdown links in the dashboard

class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _js_inline():
    """app.js's inline() run in Python: the same regular expressions, applied in the same order to esc(s)."""
    src = (ROOT / "holdco" / "static" / "app.js").read_text(encoding="utf-8")
    body = src[src.index("function inline(s)"):src.index("function md(src)")]
    steps = re.findall(r"\.replace\(/(.+?)/g, '(.*?)'\)", body)

    def inline(s: str) -> str:
        out = html.escape(s, quote=True).replace("&#x27;", "&#39;")
        for pattern, replacement in steps:
            out = re.sub(pattern, re.sub(r"\$(\d)", r"\\g<\1>", replacement), out)
        return out
    return steps, inline


class TestMarkdownLinks(unittest.TestCase):
    def test_a_link_address_cannot_carry_a_second_link(self):
        steps, inline = _js_inline()
        self.assertEqual(len(steps), 5, "inline() is code, bold, emphasis, Markdown link and bare URL")
        attack = "[x](https://a(https://onmouseover=location='javascript:alert%281%29'//)"
        for text in (attack, f"Brief: {attack} done", "See [docs](https://example.com/a) and (https://example.com/b)."):
            p = _Tags()
            p.feed(inline(text))
            for tag, attrs in p.tags:
                self.assertFalse([a for a in attrs if a.startswith("on")], f"{text!r} -> {inline(text)}")
                if tag == "a":
                    self.assertLessEqual(set(attrs), {"href", "target", "rel"}, inline(text))
                    self.assertRegex(attrs["href"], r"^https?://")
        p = _Tags()
        p.feed(inline("See [docs](https://example.com/a) and (https://example.com/b)."))
        self.assertEqual([a["href"] for t, a in p.tags if t == "a"], ["https://example.com/a", "https://example.com/b"])


# ================================================================ 2. numbers in a brief

class TestBriefNumbers(unittest.TestCase):
    def test_scaled_numbers_and_money_are_claims(self):
        self.assertEqual(rhythms.numbers_in("MRR is $50k, ARR $1.2M, 3x the signups, $2000 in revenue"),
                         [("50k", 50000.0), ("1.2M", 1200000.0), ("3x", 3.0), ("2000", 2000.0)])
        self.assertEqual(rhythms.numbers_in("In 2026 we ran for 24h; the 3rd test, sent_7d"), [])
        data = {"metrics": [{"name": "stripe.revenue_usd_30d", "value": 1523, "source": "stripe"}]}
        self.assertEqual(rhythms.unsupported_numbers("Revenue $1.5k [stripe].", data), [], "1,523 rounds to 1.5k")
        self.assertEqual(rhythms.unsupported_numbers("Revenue $50k and $2000 more.", data), ["50k", "2000"])

    def test_write_brief_refuses_an_invented_scaled_number(self):
        c = make_company("NumbersCo", members=("ceo",))
        try:
            ctx = ctx_for(c, "ceo")
            with mock.patch.object(notify, "send_later", lambda *a, **k: None):
                out = run(ctx, "write_brief", kind="daily", body="## What moved\nRevenue reached $12k this week.")
            self.assertTrue(out.startswith("ERROR: not stored"), out)
            self.assertIn("12k", out)
        finally:
            db.update("companies", c["id"], status="archived")


# ================================================================ 3. strategy changes routed to the owner

class TestOwnerProposals(Base):
    def test_one_decision_per_field_and_the_cap_on_pending_questions(self):
        sales, mk = ctx_for(self.c, "sales"), ctx_for(self.c, "marketing")
        why = "Competitors charge more; see research/competitors.md."
        first = run(sales, "propose_brain_change", section="strategy", change='{"price": "$15/month"}', reason=why)
        self.assertIn("Decision #", first)
        again = run(mk, "propose_brain_change", section="strategy", change='{"price": "$19"}', reason=why)
        self.assertIn("already waits for the owner's decision", again)
        self.assertIn("Decision #", run(mk, "propose_brain_change", section="strategy",
                                        change='{"icp": "dog walkers"}', reason=why))
        self.assertIn("Decision #", run(mk, "propose_brain_change", section="strategy",
                                        change='{"offer": "a PDF plus a call"}', reason=why))
        capped = run(sales, "propose_brain_change", section="strategy", change='{"positioning": "cheaper than a vet"}',
                     reason=why)
        self.assertTrue(capped.startswith("ERROR: 3 questions from the team are already waiting"), capped)
        self.assertEqual(len(pending_decisions(self.c)), 3)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM brain_changes WHERE company_id=?", (self.c["id"],)), 3,
                         "a refused proposal leaves no row behind")
        self.assertIn("Proposal #", run(mk, "propose_brain_change", section="strategy", change='{"voice": "warm"}',
                                        reason=why), "a change the CEO reviews is not capped")

    def test_the_reason_the_owner_reads_passes_the_claims_gate(self):
        out = run(ctx_for(self.c, "sales"), "propose_brain_change", section="strategy", change='{"price": "$15"}',
                  reason="We sent 300 emails this week and 40 people paid.")
        self.assertTrue(out.startswith("ERROR: not sent to the owner"), out)
        self.assertEqual(pending_decisions(self.c), [])
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM brain_changes WHERE company_id=?", (self.c["id"],)), 0)


# ================================================================ 4. a brief rhythm's deliverable is its brief

class TestBriefDeliverable(Base):
    def _evening_task(self) -> dict:
        r = next(x for x in presets.PRESETS["ceo"]["routines"] if x["title"] == "Evening brief")
        tid = rhythms.create_routine_task(self.c, agent_of(self.c), r, "ceo:Evening brief")
        return db.get_task(tid)

    def test_finish_without_the_brief_is_refused_once(self):
        ctx = ctx_for(self.c, task=self._evening_task())
        out = run(ctx, "finish", summary="Wrote the brief.", status="done")
        self.assertIn('write_brief(kind="daily"', out)
        self.assertFalse(ctx.finished)
        self.assertTrue(run(ctx, "write_brief", kind="daily", body="## What moved\nNothing moved today.")
                        .startswith("Brief #"))
        self.assertEqual(run(ctx, "finish", summary="Stored the brief.", status="done"), "Session ended.")
        self.assertEqual(ctx.finish_status, "done")

    def test_a_brief_from_an_earlier_attempt_counts_and_a_second_refusal_is_partial(self):
        task = self._evening_task()
        ctx = ctx_for(self.c, task=task)
        run(ctx, "finish", summary="Done.", status="done")
        self.assertEqual(run(ctx, "finish", summary="Done.", status="done"), "Session ended.")
        self.assertEqual(ctx.finish_status, "partial", "refused once, then accepted as partial")
        db.run("INSERT INTO briefs(company_id, kind, body, data, created_at) VALUES(?,?,?,?,?)",
               (self.c["id"], "daily", "An earlier attempt's brief.", "{}", db.now()))
        retry = ctx_for(self.c, task=db.get_task(task["id"]))
        self.assertEqual(run(retry, "finish", summary="Stored it last attempt.", status="done"), "Session ended.")
        self.assertEqual(retry.finish_status, "done")


# ================================================================ 5. a dismissed strategy decision

class TestDismissedProposal(Base):
    def test_dismissing_the_decision_rejects_the_proposal(self):
        mk = ctx_for(self.c, "marketing")
        self.assertIn("Decision #", run(mk, "propose_brain_change", section="strategy", change='{"price": "$15"}'))
        aid = pending_decisions(self.c)[0]["id"]
        actions.decide(aid, "reject")   # the owner dismisses it without choosing
        self.assertEqual(db.one("SELECT status FROM brain_changes WHERE company_id=?", (self.c["id"],))["status"],
                         "rejected")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM decisions WHERE company_id=?", (self.c["id"],)), 0,
                         "nothing was decided, so the brain records nothing")
        self.assertIn("Decision #", run(mk, "propose_brain_change", section="strategy", change='{"price": "$15"}'),
                      "the agent may propose it again")


# ================================================================ 6. contact emails in proposals

class TestProposalEmails(Base):
    def test_digest_and_lookup_hide_the_address_from_roles_without_email(self):
        run(ctx_for(self.c, "marketing"), "propose_brain_change", section="contacts",
            change='{"name": "Jane Roe", "email": "jane@petshop.example"}', reason="met at an expo")
        digest = brain.digest(db.get_company(self.c["id"]))
        self.assertIn("Jane Roe", digest)
        self.assertNotIn("jane@petshop.example", digest)
        self.assertNotIn("jane@petshop.example", run(ctx_for(self.c, "design"), "brain_lookup", section="proposals"))
        self.assertIn("jane@petshop.example", run(ctx_for(self.c, "sales"), "brain_lookup", section="proposals"))
        self.assertIn("jane@petshop.example", contracts.cycle_sections(db.get_company(self.c["id"])),
                      "the CEO reviewing it sees the address")


# ================================================================ 7. the planning prompt

class TestPlanningPrompt(Base):
    def test_the_no_goal_line_lists_what_the_weekly_review_cancels(self):
        brain._insert("goals", self.c["id"], {"title": "10 customers", "metric": "stripe.customers", "target": 10,
                                              "deadline": "2099-01-01", "owner_role": "sales"})
        reply = add_task(self.c, "Reply to email #3 from a customer", created_by="inbox", role="sales")
        owner = add_task(self.c, "Owner's idea", created_by="owner")
        loose = add_task(self.c, "Write a blog post", goal_id=0)
        company = db.get_company(self.c["id"])
        line = next(ln for ln in contracts.cycle_sections(company).splitlines() if ln.startswith("(no goal)"))
        self.assertIn(f"#{loose['id']}", line)
        self.assertNotIn(f"#{reply['id']}", line)
        self.assertNotIn(f"#{owner['id']}", line)
        self.assertEqual(rhythms.after_weekly(company), [loose["id"]], "the same tasks the weekly review cancels")
        lines = agents._task_lines(company, tools.OPEN_STATUSES, "priority, id", 40, by_goal=True)
        self.assertIn(f"No goal (reply to an inbound email): #{reply['id']}", lines)

    def test_a_long_proposal_is_capped_in_the_planning_prompt(self):
        run(ctx_for(self.c, "marketing"), "propose_brain_change", section="lessons",
            change="A long research dump. " * 400, reason="it matters")
        section = contracts.cycle_sections(db.get_company(self.c["id"])).split("(review_brain_change)\n", 1)[1]
        self.assertLessEqual(len(section.splitlines()[0]), contracts.PROPOSAL_LINE_CAP)


# ================================================================ 8. an empty metric source

class TestEmptySource(Base):
    def test_an_empty_source_is_self_reported(self):
        cid = self.c["id"]
        brain._insert("goals", cid, {"title": "10 customers", "metric": "stripe.customers", "target": 10,
                                     "deadline": "2099-01-01"})
        db.run("INSERT INTO metrics(company_id, agent_id, name, value, note, source, created_at) VALUES(?,?,?,?,?,?,?)",
               (cid, None, "stripe.customers", 7, "", "", db.now()))
        data = rhythms.weekly_data(db.get_company(cid))
        self.assertIsNone(data["goals"][0]["current"], "not scored from a row nobody can trace to a connector")
        self.assertEqual(data["metrics"], [])
        self.assertEqual([m["name"] for m in data["self_reported"]], ["stripe.customers"])


# ================================================================ 9. two answers to one decision

class TestDecisionRace(Base):
    def test_the_owner_and_the_deadline_record_one_answer(self):
        out = decisions.create(self.c, agent_of(self.c), None, "Should we raise the starter plan to $15 a month?",
                               ["Raise it to $15", "Keep $9"], "Keep $9", deadline_hours=1)
        self.assertTrue(out.startswith("Decision #"), out)
        aid = pending_decisions(self.c)[0]["id"]
        real = db.get_company
        fired = []

        def deadline_first(cid):
            # The scheduler's deadline answers while the owner's answer is on its way.
            if not fired:
                fired.append(True)
                actions.decide(aid, "approve", answer="Keep $9", decided_by="deadline")
            return real(cid)

        with mock.patch.object(db, "get_company", side_effect=deadline_first):
            with self.assertRaises(ValueError):
                actions.decide(aid, "approve", answer="Raise it to $15", decided_by="owner")
        row = db.one("SELECT status, result, decided_by FROM approvals WHERE id=?", (aid,))
        self.assertEqual((row["status"], row["result"], row["decided_by"]), ("answered", "Keep $9", "deadline"))
        self.assertEqual([r["decided_by"] for r in db.q("SELECT decided_by FROM decisions WHERE company_id=?",
                                                        (self.c["id"],))], ["deadline"])


# ================================================================ 10. an unsubscribed contact

class TestUnsubscribedContact(Base):
    def test_send_email_refuses_a_contact_marked_unsubscribed(self):
        db.update_company_settings(self.c["id"], postal_address="1 Main St, Springfield")
        sales = ctx_for(self.c, "sales")
        added = run(sales, "add_contact", name="Jane Roe", email="Jane@PawShop.co", stage="contacted")
        cid = int(re.search(r"#(\d+)", added).group(1))
        first = run(sales, "send_email", to="jane@pawshop.co", subject="Hello", body="A short note. Jane, hi.")
        self.assertNotIn("unsubscribed", first)
        run(sales, "update_contact", contact_id=cid, unsubscribed=True)
        out = run(sales, "send_email", to="JANE@pawshop.co", subject="Hello again", body="Another note.")
        self.assertTrue(out.startswith("ERROR: email not queued"), out)
        self.assertIn(f"unsubscribed (contact #{cid})", out)


if __name__ == "__main__":
    unittest.main()
