"""Phase 3 tests for Holdco Bench (holdco/bench/, docs/phases/phase-3-verify.md section 1): the scenarios against the
fake model in its normal and lazy modes, --only and the command line's JSON, the dashboard API, and mocks that stay
in the bench's own thread.

Run with:  python -m unittest tests.test_phase3_bench -v
"""
from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-phase3-bench-")

from holdco import (bench, browser, bus, config, connectors, db, llm, notify, server, tools, vault,  # noqa: E402,F401
                    verify, web, workspace)   # server registers the dashboard's routes
from holdco.bench import mocks, runner, scenarios  # noqa: E402
from holdco.routing import ROUTES, HTTPError  # noqa: E402
from tests.fake_ollama import FakeModelServer  # noqa: E402

db.init()

MODEL = "gpt-oss:20b"

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _failed(row: dict) -> dict[str, str]:
    """{scenario: why} for the scenarios that failed, for readable test failures."""
    out = {}
    for r in row["results"]:
        if not r["passed"]:
            out[r["name"]] = r["error"] or "; ".join(f"{a['assertion']}: {a['detail']}" for a in r["failed"])
    return out


class TestBench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = FakeModelServer(delay=0).start()
        cls.lazy = FakeModelServer(delay=0, lazy=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.model.stop()
        cls.lazy.stop()

    def run_bench(self, server, only=None) -> dict:
        return runner.run_all(MODEL, only=only, settings={"llm_backend": "ollama", "llm_base_url": server.url})

    def setUp(self):
        self.before = self.snapshot()

    @staticmethod
    def snapshot() -> dict:
        return {"vault": set(vault.names()), "connectors": {r["id"] for r in db.q("SELECT id FROM connectors")},
                "suppression": {r["email"] for r in db.q("SELECT email FROM suppression")}}

    def assert_clean(self):
        """Nothing of a bench is left behind: no company, folder, connector, vault secret or patched function."""
        self.assertFalse(bench.RUNNING)
        self.assertEqual(db.q("SELECT id FROM companies WHERE status='bench' OR name LIKE 'Bench: %'"), [])
        self.assertEqual([p.name for p in config.COMPANIES_DIR.glob("bench-*")], [])
        self.assertNotIn(mocks.BENCH_MCP, connectors.DEFINITIONS)
        self.assertEqual(self.snapshot(), self.before)
        for mod, name in ((web, "search"), (web, "http_get"), (web, "check_public_url"), (db, "get_settings"),
                          (db, "get_company"), (notify, "send"), (notify, "approval_requested"), (llm, "chat"),
                          (tools, "execute"), (connectors, "visible"), (browser, "_fetch")):
            self.assertEqual(getattr(mod, name).__module__, mod.__name__, f"{mod.__name__}.{name} is still patched")
        self.assertEqual(tools.REGISTRY["write_file"].fn.__module__, "holdco.tools")

    def test_fake_model_passes_everything(self):
        row = self.run_bench(self.model)
        self.assertEqual(row["scenarios"], len(scenarios.SCENARIOS))
        self.assertEqual(len(scenarios.SCENARIOS), 33)
        self.assertEqual(_failed(row), {})
        self.assertEqual((row["passed"], row["pass_rate"]), (33, 1.0))
        # The bench_runs row with all four scores, newest first in the API's list.
        stored = runner.get_run(row["id"])
        for key in ("pass_rate", "tool_validity", "tokens_per_second", "steps_avg"):
            self.assertIsNotNone(stored[key], key)
        self.assertGreater(stored["tool_validity"], 0.9)
        self.assertTrue(stored["finished_at"])
        self.assertEqual(len(stored["results"]), 33)
        self.assertEqual(runner.list_runs(1)[0]["id"], row["id"])
        # Speed from llm.Reply's eval counts: the fake reports 80 tokens generated in 2 s for every reply.
        self.assertEqual(stored["tokens_per_second"], 40.0)
        self.assertNotIn("approximate", stored["notes"])
        self.assert_clean()

    def test_speed_is_approximate_without_eval_times(self):
        """The OpenAI-compatible API reports tokens but no generation time: all tokens over the wall time of the model
        calls, and the notes say so."""
        row = runner.run_all("local-model", only=["budget_respected"],
                             settings={"llm_backend": "openai", "llm_base_url": self.model.url})
        self.assertEqual(_failed(row), {})
        self.assertIn("Tokens per second is approximate", row["notes"])
        self.assertGreater(row["tokens_per_second"], 0)
        self.assertGreater(row["results"][0]["eval_count"], 0)   # completion tokens are reported...
        self.assertEqual(row["results"][0]["eval_seconds"], 0)   # ...their time is not
        self.assert_clean()

    def test_lazy_model_fails_the_right_ones(self):
        row = self.run_bench(self.lazy)
        failed = set(_failed(row))
        expected = {s.name for s in scenarios.SCENARIOS if s.lazy_fails}
        self.assertEqual(failed, expected, f"failed only in lazy mode: {sorted(failed - expected)}; passed although "
                                           f"lazy: {sorted(expected - failed)}")
        self.assert_clean()

    def test_trace_is_stored_and_capped(self):
        """Every scenario result keeps what the model did, for diagnosing a failure on the bench page: its tool calls
        (arguments and results cut short), plain-text replies and how each run ended, at most runner.TRACE_CAP
        characters. One run's page has the traces; the list of runs leaves them out."""
        row = self.run_bench(self.model, only=["respect_approval", "chat_creates_task", "triage_inbound"])
        self.assertEqual(_failed(row), {})
        stored = {r["name"]: r for r in runner.get_run(row["id"])["results"]}
        email = stored["respect_approval"]["trace"]
        calls = [e for e in email if "tool" in e]
        self.assertEqual([e["tool"] for e in calls], ["write_file", "send_email", "finish"])
        self.assertEqual([e["step"] for e in calls], [1, 2, 3])
        self.assertIn("jo@happypaws.com", calls[1]["args"])
        self.assertIn("approval", calls[1]["result"].lower())
        for e in calls:
            self.assertLessEqual(len(e["args"]), runner.TRACE_ARGS)
            self.assertLessEqual(len(e["result"]), runner.TRACE_RESULT)
        self.assertEqual((email[-1]["run"], email[-1]["status"], email[-1]["steps"]), ("task", "done", 3))
        self.assertIn("intro email", email[-1]["summary"])
        chat = stored["chat_creates_task"]["trace"]
        self.assertEqual(chat[0]["tool"], "create_task")
        self.assertEqual(chat[1], {"step": 2, "text": "Got it. I've queued that for Echo in marketing."})
        self.assertEqual((chat[-1]["run"], chat[-1]["status"]), ("chat", "done"))
        self.assertIn('"labels"', stored["triage_inbound"]["trace"][0]["text"])   # a job: its output
        for r in stored.values():
            self.assertLessEqual(len(json.dumps(r["trace"], ensure_ascii=False)), runner.TRACE_CAP)
        self.assertTrue(all("trace" not in r for r in runner.list_runs(1)[0]["results"]))
        # A call the loop guard answered instead of running is in the trace with the guard's answer.
        lazy = self.run_bench(self.lazy, only=["no_repeat_failed_call"])["results"][0]["trace"]
        fetches = [e for e in lazy if e.get("tool") == "fetch_url"]
        self.assertGreaterEqual(len(fetches), 3)
        self.assertTrue(fetches[1]["result"].startswith("You already made this exact call"), fetches)
        self.assertEqual(fetches[2]["result"], "Session stopped: repeated action.")
        self.assertEqual((lazy[-1]["run"], lazy[-1]["status"]), ("task", "partial"))
        # A long session: the first and the last steps stay, the middle ones become one note, within the cap.
        long = [{"step": i, "tool": "web_search", "args": "q" * runner.TRACE_ARGS,
                 "result": "r" * runner.TRACE_RESULT} for i in range(1, 61)]
        capped = runner.cap_trace(long)
        self.assertLessEqual(len(json.dumps(capped, ensure_ascii=False)), runner.TRACE_CAP)
        notes = [e for e in capped if "note" in e]
        self.assertEqual(len(notes), 1)
        self.assertEqual((capped[0]["step"], capped[-1]["step"]), (1, 60))
        self.assertIn(f"{60 - (len(capped) - 1)} more entries left out", notes[0]["note"])
        self.assertEqual(runner.cap_trace(long[:3]), long[:3])
        self.assertEqual(runner._clip("a  b\n c" + "x" * 400, 10), "a b cxxxx…")
        self.assert_clean()

    def test_search_mock_answers_the_query(self):
        """The scripted search finds what a query asks for, as a search engine would, instead of the same results for
        every query (real models searched again and again for pages the scenario does not have). Every research
        competitor the results list has a page with a price."""
        titles = lambda q: [r["title"] for r in mocks.relevant(scenarios.RESULTS, q)]   # noqa: E731
        self.assertEqual(titles("dog meal plan subscription prices"),
                         ["PawChef pricing: fresh meal plans for dogs", "DogDish prices", "Kibble & Co plans"])
        self.assertEqual(titles("dogdish.com"), ["DogDish prices"])
        self.assertEqual(titles("home cooked dog food nutrition basics"), ["Home-cooked dog food: what vets say"])
        self.assertEqual(titles("dog owners population United States"), [])   # not in this world: nothing
        self.assertEqual(len(titles("dog")), 3)   # only common words: everything that has one
        m = mocks.install({"search": scenarios.RESULTS}, None)
        try:
            self.assertEqual(web.search("dog owners population United States", 6), [])
            self.assertEqual([r["url"] for r in web.search("dogdish", 6)], [scenarios.DOGDISH])
        finally:
            m.remove()
        for r in scenarios.RESULTS[:3]:   # the three competitors
            self.assertRegex(scenarios.PAGES[r["url"]], r"\$\d+ per (month|week)")
        self.assert_clean()

    def test_only_and_cli_json(self):
        row = self.run_bench(self.model, only=["kill_switch_respected", "budget_respected"])
        self.assertEqual([r["name"] for r in row["results"]], ["kill_switch_respected", "budget_respected"])
        self.assertEqual(row["passed"], 2)
        with self.assertRaises(ValueError):
            runner.run_all(MODEL, only=["no_such_scenario"])
        # The command line: its own temporary data folder, JSON on stdout, exit 0 when everything passed, 1 otherwise.
        env = {**os.environ, "HOLDCO_PORT": str(_free_port()), "PYTHONIOENCODING": "utf-8"}
        env.pop("HOLDCO_DATA", None)
        for server, code in ((self.model, 0), (self.lazy, 1)):
            p = subprocess.run([sys.executable, "-m", "holdco.bench", "--model", MODEL, "--only", "chat_creates_task",
                                "--json", "--base-url", server.url], cwd=str(ROOT), env=env, capture_output=True,
                               text=True, encoding="utf-8", timeout=120)
            self.assertEqual(p.returncode, code, p.stderr[-2000:])
            out = json.loads(p.stdout)
            self.assertEqual(out["scenarios"], 1)
            self.assertEqual(out["passed"], 1 - code)
            self.assertEqual(out["results"][0]["name"], "chat_creates_task")
            self.assertEqual(out["settings"], {"llm_base_url": server.url})
        p = subprocess.run([sys.executable, "-m", "holdco.bench", "--model", MODEL, "--only", "nope"], cwd=str(ROOT),
                           env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 2)
        self.assertIn("Unknown scenario", p.stderr)
        self.assert_clean()

    def test_api(self):
        def call(method, path, body=None, query=None):
            h = type("H", (), {"query": query or {}})()
            for mth, rx, fn in ROUTES:
                m = rx.match(path)
                if mth == method and m:
                    return fn(h, m, body)
            raise AssertionError(f"no route {method} {path}")

        saved = {k: db.get_settings()[k] for k in ("llm_base_url", "llm_backend")}
        db.set_settings({"llm_base_url": self.model.url, "llm_backend": "ollama"})   # a dashboard bench: live settings
        try:
            with self.assertRaises(HTTPError) as e:
                call("POST", "/api/bench/run", {"model": MODEL, "only": ["nope"]})
            self.assertEqual(e.exception.status, 400)
            run_id = call("POST", "/api/bench/run", {"model": MODEL, "only": ["budget_respected"]})["run_id"]
            deadline = time.time() + 60
            while call("GET", "/api/bench/status")["running"] and time.time() < deadline:
                time.sleep(0.05)
            status = call("GET", "/api/bench/status")
            self.assertFalse(status["running"])
            self.assertEqual((status["run_id"], status["done"], status["total"]), (run_id, 1, 1))
            runs = call("GET", "/api/bench/results", query={"limit": ["5"]})["runs"]
            self.assertEqual(runs[0]["id"], run_id)
            one = call("GET", f"/api/bench/results/{run_id}")
            self.assertEqual((one["scenarios"], one["passed"]), (1, 1))
            self.assertEqual(one["results"][0]["name"], "budget_respected")
            with self.assertRaises(HTTPError) as e:
                call("GET", "/api/bench/results/999999")
            self.assertEqual(e.exception.status, 404)
            bench.STATUS["running"] = True   # one bench at a time
            try:
                with self.assertRaises(HTTPError) as e:
                    call("POST", "/api/bench/run", {"model": MODEL})
                self.assertEqual(e.exception.status, 409)
            finally:
                bench.STATUS["running"] = False
        finally:
            db.set_settings(saved)
        self.assert_clean()

    def test_timeout_and_crash_clean_up(self):
        settings = {"llm_backend": "ollama", "llm_base_url": self.model.url}
        base = scenarios.BY_NAME["budget_respected"]
        late = runner.run_scenario(dataclasses.replace(base, timeout_s=0), MODEL, settings)
        self.assertFalse(late["passed"])
        self.assertEqual(late["error"], "timed out after 0 s")
        self.assertEqual(late["steps"], 0)   # stopped at the first step boundary
        broken = runner.run_scenario(dataclasses.replace(base, run={**base.run, "role": "nobody"}), MODEL, settings)
        self.assertFalse(broken["passed"])
        self.assertIn("no nobody agent", broken["error"])
        self.assert_clean()

    def test_bench_companies_stay_out_of_the_owners_view(self):
        """A bench run's companies are temporary: their requests, agents, events and audit rows never reach the Inbox,
        the counts, the feed or /status, and nothing of theirs is sent to the owner's phone, from any thread."""
        def call(path, query=None):
            h = type("H", (), {"query": {k: [v] for k, v in (query or {}).items()}})()
            for mth, rx, fn in ROUTES:
                m = rx.match(path)
                if mth == "GET" and m:
                    return fn(h, m, None)
            raise AssertionError(f"no route GET {path}")

        company, _secret = runner._create_company(scenarios.BY_NAME["respect_approval"])
        try:
            cid = company["id"]
            before = call("/api/state")["approvals"]
            agent = db.one("SELECT * FROM agents WHERE company_id=? ORDER BY id LIMIT 1", (cid,))
            aid = db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, "
                         "created_at) VALUES(?,?,?,?,?,?,?,?)", (cid, agent["id"], None, "send_email", "Email to x",
                                                                 json.dumps({"to": "x@bench.example"}), "pending",
                                                                 db.now()))
            db.run("INSERT INTO audit(created_at, company_id, action, proposed_by, approved_by, payload, result) "
                   "VALUES(?,?,?,?,?,?,?)", (db.now(), cid, "social_post", agent["name"], "policy:auto", "{}", "ok"))
            db.run("INSERT INTO events(company_id, agent_id, kind, message, created_at) VALUES(?,?,?,?,?)",
                   (cid, agent["id"], "info", "a bench event", db.now()))
            state = call("/api/state")
            self.assertEqual(state["approvals"], before)
            self.assertNotIn(cid, [c["id"] for c in state["companies"]])
            self.assertNotIn(cid, [a["company_id"] for a in state["agents"]])
            self.assertNotIn(aid, [a["id"] for a in call("/api/approvals")["approvals"]])
            self.assertNotIn(cid, [r["company_id"] for r in call("/api/audit")["audit"]])
            self.assertNotIn(cid, [e["company_id"] for e in call("/api/events", {"limit": "300"})["events"]])
            self.assertNotIn(company["name"], notify.format_status())
            # The phone: neither a request nor an error of a bench company is sent; a real company's still is.
            sent = []
            with mock.patch.object(notify, "configured", lambda s=None: True), \
                    mock.patch.object(notify, "_enqueue", lambda fn, *a, **kw: sent.append(a[0] if a else "")):
                notify.approval_requested(db.one("SELECT * FROM approvals WHERE id=?", (aid,)))
                notify.error(db.get_company(cid), f"bench error {aid}")
                notify.error({"id": cid, "name": company["name"]}, f"bench error {aid} again")
                self.assertEqual(sent, [])
                notify.error({"id": None, "name": "Owner Co"}, f"real error {aid}")
            self.assertEqual(len(sent), 1)
            # Open dashboards are not sent a running bench's events (they stay in the database for its checks).
            qu = bus.subscribe()
            try:
                with mock.patch.object(bench, "RUNNING", True):
                    bus.emit(cid, "error", f"bench feed {aid}")
                    bus.emit(None, "info", f"owner feed {aid}")
                live = [qu.get_nowait()["message"] for _ in range(qu.qsize())]
            finally:
                bus.unsubscribe(qu)
            self.assertNotIn(f"bench feed {aid}", live)
            self.assertIn(f"owner feed {aid}", live)
            self.assertTrue(db.one("SELECT id FROM events WHERE company_id=? AND message=?", (cid, f"bench feed {aid}")))
        finally:
            runner._delete_company(company)
        self.assert_clean()

    def test_web_page_rule_is_the_gates(self):
        """browser_check_pass applies verify.judge_browser_check, the gate's own rule, so the Bench and the
        verification gate can never disagree about a page."""
        sc = scenarios.BY_NAME["fix_page_failing_qa"]
        company, _secret = runner._create_company(sc)
        try:
            page = workspace.safe_path(company, "site/index.html")
            page.parent.mkdir(parents=True, exist_ok=True)
            state = {"company": company, "company_id": company["id"], "scenario": sc, "run": None, "task_id": None}
            for html, passes in ((scenarios.BROKEN_PAGE, False), (scenarios.HOME_PAGE, True)):
                page.write_text(html, encoding="utf-8")
                with mock.patch.object(verify, "judge_browser_check", wraps=verify.judge_browser_check) as judge:
                    ok, detail = runner.check(("file_check", "site/index.html", "browser_check_pass"), state)
                self.assertEqual(judge.call_count, 1)
                gate_ok, problems, mode, _facts = verify.judge_browser_check(
                    company, browser.browser_check(runner._ctx(state), "site/index.html"))
                self.assertEqual((ok, gate_ok, mode), (passes, passes, "static"))
                self.assertEqual(detail, ("; ".join(problems) or "the page passes") + " (static)")
            self.assertFalse(hasattr(runner, "_browser_rule"))
        finally:
            runner._delete_company(company)
        self.assert_clean()

    def test_mocks_stay_in_their_thread(self):
        owner = db.get_settings()["owner_name"]
        seen = {}
        m = mocks.install({"search": "blocked"}, None, settings={"owner_name": "Bench owner"})
        try:
            self.assertEqual(db.get_settings()["owner_name"], "Bench owner")
            with self.assertRaises(web.WebError):
                web.search("dog food", 3)
            status, _ctype, body, _final = web.http_get("https://www.example.com/missing")
            self.assertEqual(status, 404)
            t = threading.Thread(target=lambda: seen.update(owner=db.get_settings()["owner_name"]))
            t.start()
            t.join()
            self.assertEqual(seen["owner"], owner)   # another thread (the dashboard, the scheduler) sees the real one
        finally:
            m.remove()
            m.remove()   # twice is harmless
        self.assertEqual(db.get_settings()["owner_name"], owner)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
