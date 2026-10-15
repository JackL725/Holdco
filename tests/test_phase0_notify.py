"""Phase 0 unit tests for phone notifications (holdco/notify.py) against tests/fake_telegram.py.

Run with:  python -m unittest tests.test_phase0_notify -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-notify-")

from holdco import actions, db, notify, roles, server, workspace  # noqa: E402
from tests.fake_telegram import FakeTelegram  # noqa: E402
from tests.helpers import free_port, wait_for  # noqa: E402

db.init()


def make_company(name="TestCo", members=("ceo", "engineering", "marketing", "sales", "qa")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "An idea", "active", "building", "{}", db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def make_approval(company: dict, action: str, payload: dict, role="marketing", task_id=None) -> dict:
    """A pending approvals row the way actions.gate would queue it."""
    agent = db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))
    aid = db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, created_at)"
                 " VALUES(?,?,?,?,?,?,?,?)",
                 (company["id"], agent["id"], task_id, action, actions._summary(action, payload),
                  json.dumps(payload, sort_keys=True), "pending", db.now()))
    return db.one("SELECT * FROM approvals WHERE id=?", (aid,))


def approval(aid: int) -> dict:
    return db.one("SELECT * FROM approvals WHERE id=?", (aid,))


def reset_state():
    notify.flush()   # a send still on the sender thread must not land in the next test's records
    notify._recent.clear()
    notify._errors.clear()
    notify._failures.update(count=0, last_error="", last_at=None)


class NotifyCase(unittest.TestCase):
    """A fake Telegram per class; settings point Holdco at it (Telegram and ntfy) with notify_level important."""

    @classmethod
    def setUpClass(cls):
        notify.stop_poller()   # whatever an earlier class left running
        cls.tg = FakeTelegram().start()
        cls.settings = {"telegram_api_base": cls.tg.url, "telegram_bot_token": cls.tg.token,
                        "telegram_chat_id": str(cls.tg.chat_id), "ntfy_server": cls.tg.url + "/ntfy",
                        "ntfy_topic": "holdco", "notify_level": "important", "kill_switch": False}
        db.set_settings(cls.settings)

    @classmethod
    def tearDownClass(cls):
        notify.stop_poller()
        cls.tg.stop()
        # Test modules share one database under discover: leave nothing pointing at the stopped fake.
        db.set_settings({"telegram_bot_token": "", "telegram_chat_id": "", "ntfy_topic": "",
                         "telegram_api_base": "https://api.telegram.org", "ntfy_server": "https://ntfy.sh",
                         "notify_level": "important", "kill_switch": False})
        reset_state()

    def setUp(self):
        db.set_settings(self.settings)
        reset_state()
        self.tg.clear()

    def tearDown(self):
        notify.flush()

    def announce(self, row: dict) -> dict:
        """approval_requested sends on the sender thread; wait for it so the test can look at the message."""
        notify.approval_requested(row)
        self.assertTrue(notify.flush(5), "the sender thread did not finish in time")
        self.assertTrue(self.tg.sent, "nothing reached the fake Telegram")
        return self.tg.sent[-1]


class TestSend(NotifyCase):
    def test_html_text_buttons_and_ntfy(self):
        ok = notify.send("<b>Hi</b> &amp; bye " + notify.escape("<x>"), [[{"text": "OK", "data": "noop"},
                                                                          {"text": "No", "data": "a:1:reject"}]],
                         kind="important")
        self.assertTrue(ok)
        self.assertEqual(len(self.tg.sent), 1)
        m = self.tg.sent[0]
        self.assertEqual(m["chat_id"], str(self.tg.chat_id))
        self.assertEqual(m["parse_mode"], "HTML")
        self.assertTrue(m["disable_web_page_preview"])
        self.assertEqual(m["text"], "<b>Hi</b> &amp; bye &lt;x&gt;")
        self.assertEqual(m["reply_markup"], {"inline_keyboard": [[{"text": "OK", "callback_data": "noop"},
                                                                   {"text": "No", "callback_data": "a:1:reject"}]]})
        # ntfy gets the same message as plain text with the Holdco title, no buttons.
        self.assertEqual(len(self.tg.ntfy), 1)
        self.assertEqual(self.tg.ntfy[0]["topic"], "holdco")
        self.assertEqual(self.tg.ntfy[0]["body"], "Hi & bye <x>")
        self.assertEqual(self.tg.ntfy[0]["title"], "Holdco")

    def test_escape(self):
        self.assertEqual(notify.escape('<a href="x">&</a>'), '&lt;a href="x"&gt;&amp;&lt;/a&gt;')
        self.assertEqual(notify.escape(None), "")

    def test_identical_text_not_resent_within_an_hour(self):
        self.assertTrue(notify.send("Same thing", kind="important"))
        self.assertFalse(notify.send("Same thing", kind="important"))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertTrue(notify.send("Different thing", kind="important"))
        notify._recent[notify._key("Same thing")] = time.time() - notify.REPEAT_SECONDS - 1
        self.assertTrue(notify.send("Same thing", kind="important"))
        self.assertEqual(len(self.tg.sent), 3)
        self.assertTrue(notify.send("Same thing", kind="important", dedupe=False))   # confirmations always go

    def test_notify_level(self):
        self.assertFalse(notify.send("Forge finished task #1", kind="info"))
        self.assertEqual(self.tg.sent, [])
        for kind in ("important", "request", "escalation", "error", "brief"):
            self.assertTrue(notify.send(f"{kind} message", kind=kind), kind)
        self.assertEqual(len(self.tg.sent), 5)
        db.set_settings({"notify_level": "all"})
        self.assertTrue(notify.send("Forge finished task #1", kind="info"))
        self.assertEqual(len(self.tg.sent), 6)

    def test_nothing_configured(self):
        db.set_settings({"telegram_bot_token": "", "telegram_chat_id": "", "ntfy_topic": ""})
        self.assertFalse(notify.configured())
        self.assertFalse(notify.send("Hello", kind="important"))
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(notify._failures["count"], 0)

    def test_never_raises_and_never_prints_the_token(self):
        closed = f"http://127.0.0.1:{free_port()}"
        db.set_settings({"telegram_api_base": closed, "ntfy_server": closed + "/ntfy",
                         "telegram_bot_token": "s3cret-token"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(notify.send("Hello", kind="important"))
        self.assertEqual(notify._failures["count"], 2)
        self.assertNotIn("s3cret-token", out.getvalue())
        self.assertNotIn("s3cret-token", notify._failures["last_error"])
        self.assertIn("notify:", out.getvalue())
        # ntfy alone still counts as delivered when Telegram is down.
        db.set_settings({"ntfy_server": self.tg.url + "/ntfy"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(notify.send("Hello again", kind="important"))
        self.assertEqual(len(self.tg.ntfy), 1)
        self.assertEqual(self.tg.sent, [])

    def test_wrong_token_is_rejected(self):
        db.set_settings({"telegram_bot_token": "wrong"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(notify.send("Hello", kind="important"))   # ntfy still accepted it
        self.assertEqual(self.tg.sent, [])
        self.assertIn("401", notify._failures["last_error"])

    def test_plain_fallback_when_html_does_not_parse(self):
        # The fake accepts any HTML, so drive the fallback through a stubbed Telegram call.
        calls = []
        real = notify._telegram

        def fake(s, method, body, timeout=notify.HTTP_TIMEOUT):
            calls.append(dict(body))
            if body.get("parse_mode"):
                raise notify.NotifyError("Telegram sendMessage: HTTP 400 Bad Request: can't parse entities")
            return {"message_id": 7}
        notify._telegram = fake
        try:
            self.assertTrue(notify.send("<b>bold<i>", kind="important"))
        finally:
            notify._telegram = real
        self.assertEqual(len(calls), 2)
        self.assertNotIn("parse_mode", calls[1])
        self.assertEqual(calls[1]["text"], "bold")


class TestApprovalMessages(NotifyCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = make_company("PawPlan", ("ceo", "marketing", "sales", "ops"))

    def test_email_request_has_approve_and_reject(self):
        row = make_approval(self.company, "send_email", {"to": "jo@happypaws.example", "subject": "Quick <idea>",
                                                          "body": "Hi Jo,\n\nWe built PawPlan & more. " + "x" * 400},
                            role="sales")
        agent = db.get_agent(row["agent_id"])
        self.announce(row)
        self.assertEqual(len(self.tg.sent), 1)
        text = self.tg.sent[0]["text"]
        self.assertIn(f"<b>{agent['name']}</b> at <b>PawPlan</b>", text)
        self.assertIn("<b>Send email</b>", text)
        self.assertIn(f"request #{row['id']}", text)
        self.assertIn("To: jo@happypaws.example", text)
        self.assertIn("Subject: Quick &lt;idea&gt;", text)
        self.assertIn("PawPlan &amp; more.", text)
        self.assertNotIn("x" * 301, text)   # body cut at 300 characters
        self.assertEqual(self.tg.sent[0]["reply_markup"]["inline_keyboard"],
                         [[{"text": "Approve", "callback_data": f"a:{row['id']}:approve"},
                           {"text": "Reject", "callback_data": f"a:{row['id']}:reject"}]])
        self.assertEqual(len(self.tg.ntfy), 1)
        self.assertNotIn("<b>", self.tg.ntfy[0]["body"])

    def test_other_actions_show_their_key_fields(self):
        cases = {
            "social_post": ({"platform": "x", "content": "Meet PawPlan"}, ["X post:", "Meet PawPlan"]),
            "http_post": ({"url": "https://hooks.example/abc", "method": "post"}, ["POST https://hooks.example/abc"]),
            "run_code": ({"language": "python", "code": "print(1)\nprint(2)"},
                         ["Run python:", "<pre>print(1)\nprint(2)</pre>"]),
            "spend_money": ({"item": "pawplan.co domain", "amount_usd": 12, "vendor": "Namecheap", "reason": "Brand"},
                            ["Item: pawplan.co domain", "Amount: $12 (Namecheap)", "Reason: Brand", "<b>Purchase</b>"]),
            "deploy_site": ({}, ["Publish the website from site/", "<b>Deploy website</b>"]),
        }
        for action, (payload, needles) in cases.items():
            row = make_approval(self.company, action, payload, role="ops")
            text, buttons, kind = notify.build_approval(row)
            for needle in needles:
                self.assertIn(needle, text, action)
            self.assertEqual(kind, "request")
            self.assertEqual([b["data"] for b in buttons[0]], [f"a:{row['id']}:approve", f"a:{row['id']}:reject"])

    def test_watchdog_question_has_one_button_per_option(self):
        options = ["Skip this task", "Retry once more with my note", "Pause the company"]
        payload = {"question": "Task #3 failed twice. What now?", "options": options, "recommended": "Skip this task",
                   "pattern": "task_failed_twice:3", "context": "Attempt 2 ended out_of_steps <after> 14 steps",
                   "company_id": self.company["id"]}
        row = make_approval(self.company, "question", payload, role="ceo")
        text, buttons, kind = notify.build_approval(row)
        self.assertEqual(kind, "escalation")
        self.assertIn("needs a decision", text)
        self.assertIn("Task #3 failed twice. What now?", text)
        self.assertIn("Recommended: Skip this task", text)
        self.assertIn("ended out_of_steps &lt;after&gt; 14 steps", text)
        self.assertEqual([b[0]["data"] for b in buttons], [f"q:{row['id']}:0", f"q:{row['id']}:1", f"q:{row['id']}:2"])
        self.assertEqual(buttons[0][0]["text"], notify.CHECK + "Skip this task")
        self.assertEqual(buttons[1][0]["text"], "Retry once more with my note")
        self.announce(row)
        self.assertEqual(len(self.tg.sent[0]["reply_markup"]["inline_keyboard"]), 3)

    def test_plain_question_without_options(self):
        row = make_approval(self.company, "question", {"question": "Which colour for the logo?"}, role="marketing")
        text, buttons, kind = notify.build_approval(row)
        self.assertEqual(kind, "request")
        self.assertIsNone(buttons)
        self.assertIn("asks", text)
        self.assertIn("Which colour for the logo?", text)
        self.assertIn("Answer it in the dashboard", text)

    def test_approval_requested_never_raises(self):
        notify.approval_requested(None)
        with contextlib.redirect_stdout(io.StringIO()):
            notify.approval_requested({"action": "send_email"})   # no id: the renderer fails, the hook swallows it
        self.assertEqual(notify._failures["count"], 1)
        self.announce({"id": 999999, "action": "send_email", "payload": "{not json"})
        self.assertEqual(len(self.tg.sent), 1)   # no company or agent: still rendered with fallbacks
        self.assertIn("<b>An agent</b> at <b>Holdco</b>", self.tg.sent[0]["text"])

    def test_error_rate_limited_per_message_per_hour(self):
        notify.error(self.company, "Webhook returned HTTP 500: boom")
        notify.error(self.company, "Webhook returned HTTP 500: boom")
        self.assertTrue(notify.flush())   # the hourly rule is decided synchronously; the send is in the background
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("<b>Error</b> at <b>PawPlan</b>: Webhook returned HTTP 500: boom", self.tg.sent[0]["text"])
        self.assertEqual(self.tg.ntfy[0]["priority"], "high")
        notify.error(None, "Model server unreachable <timeout>")
        self.assertTrue(notify.flush())
        self.assertEqual(len(self.tg.sent), 2)
        self.assertIn("<b>Error</b>: Model server unreachable &lt;timeout&gt;", self.tg.sent[1]["text"])
        # An hour later the same error is reported again (send()'s identical-text rule expires with it).
        key = notify._error_key("Webhook returned HTTP 500: boom")
        notify._errors[key] = time.time() - notify.ERROR_REPEAT_SECONDS - 1
        notify._recent.clear()
        notify.error(self.company, "Webhook returned HTTP 500: boom")
        self.assertTrue(notify.flush())
        self.assertEqual(len(self.tg.sent), 3)

    def test_normalise_collapses_numbers_and_urls(self):
        a = notify.normalise("Webhook returned HTTP 500: run 123 at https://x.example/a?b=1")
        b = notify.normalise("Webhook  returned HTTP 502: run 9 at https://y.example/zzz")
        self.assertEqual(a, b)
        self.assertNotEqual(notify.normalise("Email failed"), notify.normalise("Deploy failed"))

    def test_error_events_reach_the_phone_through_bus(self):
        from holdco import bus
        bus.emit(self.company["id"], "error", "Send email failed: SMTP login refused")
        bus.emit(self.company["id"], "error", "Send email failed: SMTP login refused")
        self.assertTrue(notify.flush())
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("SMTP login refused", self.tg.sent[0]["text"])
        bus.emit(self.company["id"], "info", "not an error")
        self.assertTrue(notify.flush())
        self.assertEqual(len(self.tg.sent), 1)


class TestPoller(NotifyCase):
    """One poller thread for the class, started against the fake; stopped in tearDownClass."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = make_company("PawPlan", ("ceo", "marketing"))
        notify.ensure_poller()
        cls.poller = notify.POLLER
        assert cls.poller is not None and cls.poller.is_alive()

    def wait_sent(self, needle: str, timeout: float = 10) -> dict:
        return wait_for(lambda: next((m for m in self.tg.sent if needle in m["text"]), None), timeout, 0.05,
                        f"a message containing {needle!r}")

    def test_approve_button_runs_the_request(self):
        row = make_approval(self.company, "social_post", {"platform": "x", "content": "Meet PawPlan"})
        self.announce(row)
        mid = self.tg.sent[-1]["message_id"]
        self.tg.push_callback(f"a:{row['id']}:approve", message_id=mid)
        wait_for(lambda: len(self.tg.edits) >= 2, 10, 0.05, "the message to be edited")
        self.assertEqual(approval(row["id"])["status"], "done")
        self.assertIn("No posting webhook", approval(row["id"])["result"])
        self.assertEqual(self.tg.answered[0]["text"], "Approved")
        self.assertEqual([e["method"] for e in self.tg.edits], ["editMessageReplyMarkup", "editMessageText"])
        self.assertEqual(self.tg.edits[0]["reply_markup"], {"inline_keyboard": []})
        self.assertEqual(self.tg.edits[0]["message_id"], mid)
        self.assertTrue(self.tg.edits[1]["text"].startswith(self.tg.sent[0]["text"]))
        self.assertTrue(self.tg.edits[1]["text"].endswith("<b>You: Approved</b>"))
        self.assertEqual(self.tg.edits[1]["parse_mode"], "HTML")
        audit = db.one("SELECT * FROM audit WHERE approval_id=?", (row["id"],))
        self.assertEqual(audit["approved_by"], "owner")

    def test_reject_button(self):
        row = make_approval(self.company, "social_post", {"platform": "x", "content": "Reject me"})
        self.announce(row)
        self.tg.push_callback(f"a:{row['id']}:reject", message_id=self.tg.sent[-1]["message_id"])
        wait_for(lambda: len(self.tg.edits) >= 2, 10, 0.05, "the message to be edited")
        self.assertEqual(approval(row["id"])["status"], "rejected")
        self.assertEqual(self.tg.answered[0]["text"], "Rejected")
        self.assertIn("You: Rejected", self.tg.edits[1]["text"])

    def test_option_button_answers_a_question(self):
        tid = db.run("INSERT INTO tasks(company_id, title, role, status, priority, created_by, created_at, updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?)", (self.company["id"], "Fetch a dead page", "marketing", "blocked", 2,
                                                  "ceo", db.now(), db.now()))
        options = ["Skip this task", "Retry once more with my note", "Pause the company"]
        payload = {"question": "Task failed twice. What now?", "options": options, "recommended": "Skip this task",
                   "pattern": f"task_failed_twice:{tid}", "task_id": tid, "company_id": self.company["id"]}
        row = make_approval(self.company, "question", payload, role="ceo", task_id=tid)
        self.announce(row)
        self.tg.push_callback(f"q:{row['id']}:1", message_id=self.tg.sent[-1]["message_id"])
        wait_for(lambda: len(self.tg.edits) >= 2, 10, 0.05, "the message to be edited")
        r = approval(row["id"])
        self.assertEqual(r["status"], "answered")
        self.assertEqual(r["result"], "Retry once more with my note")
        self.assertEqual(self.tg.answered[0]["text"], "Retry once more with my note")
        self.assertIn("You: Retry once more with my note", self.tg.edits[1]["text"])
        note = db.one("SELECT content FROM memory WHERE company_id=? ORDER BY id DESC LIMIT 1", (self.company["id"],))
        self.assertIn("Retry once more with my note", note["content"])

    def test_callback_from_another_chat_is_ignored(self):
        row = make_approval(self.company, "social_post", {"platform": "x", "content": "Stranger danger"})
        self.announce(row)
        self.tg.push_callback(f"a:{row['id']}:approve", message_id=self.tg.sent[-1]["message_id"], chat_id=99)
        self.tg.push_message("/status")   # processed after the stranger's update, so its reply proves the order
        self.wait_sent("Kill switch")
        self.assertEqual(approval(row["id"])["status"], "pending")
        self.assertEqual(self.tg.answered, [])
        self.assertEqual(self.tg.edits, [])
        self.tg.push_message("/stop", chat_id=99)
        self.tg.push_message("hello")
        self.wait_sent("Commands:")
        self.assertFalse(db.get_settings().get("kill_switch"))

    def test_already_decided_or_unknown_button(self):
        row = make_approval(self.company, "social_post", {"platform": "x", "content": "Twice"})
        actions.decide(row["id"], "reject")
        self.tg.push_callback(f"a:{row['id']}:approve", message_id=1)
        self.wait_sent("Could not do that")
        self.assertIn("already rejected", self.tg.answered[0]["text"])
        self.assertIn("already rejected", self.tg.sent[-1]["text"])
        self.assertEqual(self.tg.edits, [])
        self.tg.push_callback("garbage", message_id=1)
        wait_for(lambda: len(self.tg.answered) >= 2, 10, 0.05, "the second callback answer")
        self.assertIn("not recognised", self.tg.answered[1]["text"])
        self.tg.push_callback(f"q:{row['id']}:7", message_id=1)
        wait_for(lambda: len(self.tg.answered) >= 3, 10, 0.05, "the third callback answer")
        self.assertIn("no longer exists", self.tg.answered[2]["text"])

    def test_status_command(self):
        make_approval(self.company, "social_post", {"platform": "x", "content": "Pending for status"})
        self.tg.push_message("/status@holdco_test_bot")
        m = self.wait_sent("Kill switch: off")
        self.assertIn("<b>PawPlan</b> (active):", m["text"])
        self.assertIn("open tasks", m["text"])
        self.assertIn("Requests waiting for you: ", m["text"])
        self.assertEqual(m["parse_mode"], "HTML")

    def test_stop_and_resume_commands(self):
        self.tg.push_message("/stop")
        wait_for(lambda: db.get_settings().get("kill_switch") is True, 10, 0.05, "the kill switch to turn on")
        self.wait_sent("Kill switch ON")
        ev = db.one("SELECT * FROM events WHERE kind='info' ORDER BY id DESC LIMIT 1")
        self.assertIn("Kill switch ON", ev["message"])
        self.assertEqual(json.loads(ev["data"])["by"], "telegram")
        # Approving from the phone is refused while the switch is on, with the reason sent back.
        row = make_approval(self.company, "social_post", {"platform": "x", "content": "Frozen"})
        self.tg.push_callback(f"a:{row['id']}:approve", message_id=1)
        self.wait_sent("The kill switch is on")
        self.assertEqual(approval(row["id"])["status"], "pending")
        self.tg.push_message("/resume")
        wait_for(lambda: db.get_settings().get("kill_switch") is False, 10, 0.05, "the kill switch to turn off")
        self.wait_sent("Kill switch off")
        self.assertTrue(any(n["body"].startswith("Kill switch ON") for n in self.tg.ntfy))

    def test_noop_button_on_the_test_message(self):
        r = notify.api_notify_test(None, None, {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.tg.sent[-1]["text"], notify.TEST_TEXT)
        self.tg.push_callback("noop", message_id=self.tg.sent[-1]["message_id"])
        wait_for(lambda: len(self.tg.edits) >= 2, 10, 0.05, "the test message to be edited")
        self.assertEqual(self.tg.answered[0]["text"], "OK")

    def test_ensure_poller_keeps_one_thread(self):
        before = self.poller
        notify.ensure_poller()
        notify.ensure_poller()
        self.assertIs(notify.POLLER, before)
        self.assertTrue(before.is_alive())
        self.assertEqual(sum(1 for t in __import__("threading").enumerate() if t.name == "telegram-poller"), 1)


class TestPollerLifecycle(NotifyCase):
    def test_not_started_without_token_and_chat_id(self):
        db.set_settings({"telegram_chat_id": ""})
        notify.ensure_poller()
        self.assertIsNone(notify.POLLER)
        db.set_settings({"telegram_chat_id": "42", "telegram_bot_token": ""})
        notify.ensure_poller()
        self.assertIsNone(notify.POLLER)

    def test_settings_change_stops_the_thread_and_backoff_grows(self):
        notify.ensure_poller()
        p = notify.POLLER
        self.assertTrue(p.is_alive())
        wait_for(lambda: any(r["method"] == "getUpdates" for r in self.tg.requests), 5, 0.05, "the first poll")
        body = next(r["body"] for r in self.tg.requests if r["method"] == "getUpdates")
        self.assertEqual(body["timeout"], notify.POLL_TIMEOUT)
        self.assertEqual(body["offset"], 0)
        self.assertIn("callback_query", body["allowed_updates"])
        db.set_settings({"telegram_bot_token": "rotated"})
        wait_for(lambda: not p.is_alive(), 5, 0.05, "the old poller to exit")
        # A fresh poller against a token the server rejects backs off instead of spinning.
        with contextlib.redirect_stdout(io.StringIO()):
            notify.ensure_poller()
            p2 = notify.POLLER
            self.assertIsNot(p2, p)
            wait_for(lambda: p2.backoff > notify.BACKOFF_MIN, 5, 0.05, "backoff to grow")
        self.assertIn("401", p2.last_error)
        self.assertNotIn("rotated", p2.last_error)
        notify.stop_poller()
        self.assertIsNone(notify.POLLER)
        wait_for(lambda: not p2.is_alive(), 5, 0.05, "the second poller to stop")


class TestHealthAndTestButton(NotifyCase):
    def test_health(self):
        self.assertEqual(notify.health(),
                         ("ok", f"Telegram bot @holdco_test_bot; ntfy topic set on {self.tg.url}/ntfy"))
        s = dict(db.get_settings(), telegram_bot_token="wrong")
        status, detail = notify.health(s)
        self.assertEqual(status, "bad")
        self.assertIn("401", detail)
        self.assertNotIn("wrong", detail)
        s = dict(db.get_settings(), telegram_chat_id="")
        self.assertEqual(notify.health(s)[0], "warn")
        s = dict(db.get_settings(), telegram_bot_token="", telegram_chat_id="")
        self.assertEqual(notify.health(s), ("ok", f"ntfy topic set on {self.tg.url}/ntfy"))
        s["ntfy_topic"] = ""
        status, detail = notify.health(s)
        self.assertEqual(status, "warn")
        self.assertIn("No phone notifications", detail)
        self.assertEqual(notify.check_telegram(), (True, "Telegram bot @holdco_test_bot"))
        notify._failures.update(count=1, last_error="ntfy: HTTP 500", last_at=time.time())
        status, detail = notify.health()
        self.assertEqual(status, "warn")
        self.assertIn("last failure", detail)

    def test_api_notify_test_sends_and_finds_the_chat_id(self):
        r = notify.api_notify_test(None, None, {})
        self.assertTrue(r["ok"])
        self.assertEqual(self.tg.sent[-1]["text"], notify.TEST_TEXT)
        self.assertEqual(self.tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "noop")
        self.assertEqual(self.tg.ntfy[-1]["body"], notify.TEST_TEXT)
        self.assertTrue(notify.api_notify_test(None, None, {})["ok"])   # a second press is not deduplicated
        self.assertEqual(len(self.tg.sent), 2)
        db.set_settings({"telegram_chat_id": ""})
        r = notify.api_notify_test(None, None, {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["chat_ids"], [])
        self.assertIn("Send any message to your bot", r["detail"])
        # Phase 0 fix: anyone who finds the bot can message it, so the ids are listed, never presented as "yours".
        self.tg.push_message("hi", chat_id=99)   # a stranger got there first
        self.tg.push_message("hi")
        r = notify.api_notify_test(None, None, {})
        self.assertEqual(r["chat_ids"], [99, 42])
        self.assertIn("Chat ids that have messaged the bot: 99, 42", r["detail"])
        self.assertIn("make sure the one you save is yours", r["detail"])
        self.assertNotIn("Your chat id is", r["detail"])
        self.assertEqual(db.get_settings()["telegram_chat_id"], "")   # nothing is saved on the owner's behalf
        db.set_settings({"telegram_bot_token": "", "ntfy_topic": ""})
        r = notify.api_notify_test(None, None, {})
        self.assertFalse(r["ok"])
        self.assertIn("Nothing to send to yet", r["detail"])

    def test_route_is_registered(self):
        from holdco import routing
        self.assertTrue(any(m == "POST" and rx.match("/api/notify/test") for m, rx, _ in routing.ROUTES))

    def test_set_kill_switch_helper(self):
        self.assertTrue(notify.set_kill_switch(True))
        self.assertTrue(db.get_settings()["kill_switch"])
        self.assertTrue(notify.flush())   # the confirmation goes through the sender thread
        self.assertEqual(self.tg.sent[-1]["text"], notify.KILL_ON_TEXT)
        self.assertFalse(notify.set_kill_switch(False))
        self.assertFalse(db.get_settings()["kill_switch"])
        self.assertTrue(notify.flush())
        self.assertEqual(self.tg.sent[-1]["text"], notify.KILL_OFF_TEXT)
        last = db.one("SELECT data FROM events ORDER BY id DESC LIMIT 1")
        self.assertEqual(json.loads(last["data"]), {"by": "owner"})


class TestScrub(NotifyCase):
    """The bot token never leaves the process in an error text (Phase 0 fix)."""

    def test_scrub_masks_the_token_and_the_bot_url(self):
        s = {"telegram_bot_token": "123456:ABC-SECRET\n", "telegram_api_base": "https://api.telegram.org"}
        # http.client quotes the URL with repr(), so the pasted newline shows up as the two characters "\n".
        out = notify.scrub("URL can't contain control characters. '/bot123456:ABC-SECRET\\n/getMe' "
                           "(found at least '\\n')", s)
        self.assertNotIn("ABC-SECRET", out)
        self.assertIn("'[token]/getMe'", out)
        self.assertEqual(notify.scrub("unknown url type: 'api.telegram.org/bot123456:ABC-SECRET/sendMessage'", s),
                         "unknown url type: '[token]/sendMessage'")
        self.assertEqual(notify.scrub("https://api.telegram.org/bot123456:ABC-SECRET", s), "[token]")
        self.assertEqual(notify.scrub("token 123456:ABC-SECRET\n was rejected", s), "token [token] was rejected")
        self.assertEqual(notify.scrub("Telegram getMe: HTTP 401 Unauthorized", s),
                         "Telegram getMe: HTTP 401 Unauthorized")
        self.assertEqual(notify.scrub("http://127.0.0.1:9/botXYZ/getMe", {}), "[token]/getMe")   # URL shape alone
        self.assertEqual(notify.scrub(None, {}), "")
        self.assertEqual(notify.scrub("plain " + self.tg.token), "plain [token]")   # settings read from the database
        self.assertIs(notify._scrub, notify.scrub)

    def test_malformed_api_base_or_token_never_leaks(self):
        secret = "123456:ABC-SECRET"
        # No scheme on the api base: urllib's ValueError quotes the whole URL, token included.
        db.set_settings({"telegram_api_base": "api.telegram.org", "telegram_bot_token": secret, "ntfy_topic": ""})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(notify.send("Hello", kind="important"))
            ok, why = notify.check_telegram()
            status, detail = notify.health()
            r = notify.api_notify_test(None, None, {})
            db.set_settings({"telegram_chat_id": ""})
            r2 = notify.api_notify_test(None, None, {})
        self.assertFalse(ok)
        self.assertEqual(status, "bad")
        self.assertFalse(r["ok"])
        self.assertIn("Could not send", r["detail"])
        self.assertEqual(r2["chat_ids"], [])   # no 500 either: discover_chat_ids swallows the bad URL
        for text in (out.getvalue(), notify._failures["last_error"], why, detail, json.dumps(r), json.dumps(r2)):
            self.assertNotIn("ABC-SECRET", text)
        self.assertIn("[token]", why)
        # A token pasted with a trailing newline: http.client refuses the URL and quotes it in the message.
        db.set_settings({"telegram_api_base": self.tg.url, "telegram_bot_token": secret + "\n",
                         "telegram_chat_id": str(self.tg.chat_id)})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(notify.send("Hello", kind="important"))
            ok, why = notify.check_telegram()
        self.assertFalse(ok)
        for text in (out.getvalue(), notify._failures["last_error"], why):
            self.assertNotIn("ABC-SECRET", text)
            self.assertIn("[token]", text)


class TestHooksNeverBlock(NotifyCase):
    """The hooks hand their sends to the sender thread: the caller is back long before the phone answers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = make_company("SlowCo", ("ceo", "marketing"))

    def setUp(self):
        super().setUp()
        self.tg.delay = 1.0   # the fake answers sendMessage after a second, like a bad phone network

    def tearDown(self):
        super().tearDown()
        self.tg.delay = 0.0

    def test_approval_and_error_hooks_return_at_once(self):
        row = make_approval(self.company, "social_post", {"platform": "x", "content": "Slow phone"})
        t0 = time.monotonic()
        notify.approval_requested(row)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(self.tg.sent, [])
        wait_for(lambda: len(self.tg.sent) == 1, 2, 0.02, "the request to reach the phone")
        self.assertIn("Slow phone", self.tg.sent[0]["text"])
        t0 = time.monotonic()
        notify.error(self.company, "Deploy failed: HTTP 500")
        notify.error(self.company, "Deploy failed: HTTP 500")   # the hourly rule is decided here, synchronously
        self.assertLess(time.monotonic() - t0, 0.5)
        wait_for(lambda: len(self.tg.sent) == 2, 2, 0.02, "the error to reach the phone")
        self.assertTrue(notify.flush())
        self.assertEqual(len(self.tg.sent), 2)
        self.assertEqual(len(self.tg.ntfy), 2)

    def test_gate_returns_before_the_phone_is_reached(self):
        agent = db.one("SELECT * FROM agents WHERE company_id=? AND role='marketing'", (self.company["id"],))
        t0 = time.monotonic()
        out = actions.gate(self.company, agent, None, "social_post", {"platform": "x", "content": "Gate speed"})
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertIn("Queued for the owner's approval", out)
        self.assertEqual(self.tg.sent, [])
        m = wait_for(lambda: next((m for m in self.tg.sent if "Gate speed" in m["text"]), None), 2, 0.02,
                     "the request to reach the phone")
        self.assertEqual(m["reply_markup"]["inline_keyboard"][0][0]["text"], "Approve")


class TestKillSwitch(NotifyCase):
    """The confirmation skips the identical-text rule, and the dashboard route shares the helper (Phase 0 fix)."""

    def test_confirmed_every_time_even_within_the_hour(self):
        self.assertTrue(notify.set_kill_switch(True))
        self.assertFalse(notify.set_kill_switch(False))
        self.assertTrue(notify.set_kill_switch(True))   # ON again minutes later: still confirmed
        self.assertTrue(notify.flush())
        self.assertEqual([m["text"] for m in self.tg.sent],
                         [notify.KILL_ON_TEXT, notify.KILL_OFF_TEXT, notify.KILL_ON_TEXT])
        self.assertEqual([n["body"] for n in self.tg.ntfy],
                         [notify.KILL_ON_TEXT, notify.KILL_OFF_TEXT, notify.KILL_ON_TEXT])
        self.assertTrue(db.get_settings()["kill_switch"])

    def test_api_kill_uses_the_helper(self):
        self.tg.delay = 0.5
        try:
            t0 = time.monotonic()
            out = server.api_kill(None, None, {"on": True})
            self.assertLess(time.monotonic() - t0, 0.3)   # the button answers before Telegram does
            self.assertEqual(out, {"kill_switch": True})
            self.assertEqual(server.api_kill(None, None, {"on": True}), {"kill_switch": True})
            self.assertTrue(notify.flush())
        finally:
            self.tg.delay = 0.0
        self.assertEqual([m["text"] for m in self.tg.sent], [notify.KILL_ON_TEXT, notify.KILL_ON_TEXT])
        ev = db.one("SELECT * FROM events WHERE kind='info' ORDER BY id DESC LIMIT 1")
        self.assertIn("Kill switch ON", ev["message"])
        self.assertEqual(json.loads(ev["data"])["by"], "owner")
        self.assertEqual(server.api_kill(None, None, {"on": False}), {"kill_switch": False})
        self.assertTrue(notify.flush())
        self.assertEqual(self.tg.sent[-1]["text"], notify.KILL_OFF_TEXT)
        self.assertFalse(db.get_settings()["kill_switch"])
        from holdco import routing
        self.assertTrue(any(mth == "POST" and rx.match("/api/kill") for mth, rx, _ in routing.ROUTES))


if __name__ == "__main__":
    unittest.main()
