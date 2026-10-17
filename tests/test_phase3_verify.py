"""Phase 3: the verification gate and the code sandbox (docs/phases/phase-3-verify.md sections 3 and 4).

The gate decides, by the kind of work, whether finish(status="done") is accepted: a web page through browser_check
(Playwright faked like Phase 1's tests, or the labelled static check), code by running its tests (in a faked Docker,
or through the run_code policy), a payment link through the fake Stripe, every email of the run through the outreach
rules, research by its citations against the pages this task fetched, and the task's checks lines. The sandbox
tests never need Docker: sandbox.WHICH and sandbox.RUNNER are replaced by fakes. Nothing touches the network.

Run with:  python -m unittest tests.test_phase3_verify -v
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
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
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-verify-"))
os.environ.setdefault("HOLDCO_VAULT_BACKEND", "file")   # never touch a developer's keyring

from holdco import (actions, brain, config, conn_stripe, connectors, db, doctor, mcp,  # noqa: E402
                    notify, roles, sandbox, tools, truth, verify, web, workspace)
from tests.fake_stripe import FakeStripe  # noqa: E402

db.init()

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJ"
                       "RU5ErkJggg==")   # a 1x1 image
STRIPE_KEY = "sk_test_holdcoverify12345"

# A page with no audit blocker: viewport, lang, title, labelled input. Its form has no target.
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PawPlan: weekly meal plans for dogs</title>
<meta name="description" content="Weekly meal plans your dog will eat."></head><body>
<h1>Meal plans your dog will eat</h1><a href="about.html">About us</a>
<form name="waitlist"><label for="e">Email</label><input id="e" name="email" type="email">
<button type="submit">Join the waitlist</button></form></body></html>"""
FIXED = PAGE.replace('<form name="waitlist">', '<form name="waitlist" data-netlify="true">')
ABOUT = "<!doctype html><html lang='en'><title>About</title><h1>About</h1></html>"
CALC_BAD = '"""Prices for the meal plans."""\n\n\ndef add(a, b):\n    return a - b   # a bug the test finds\n'
CALC_GOOD = '"""Prices for the meal plans."""\n\n\ndef add(a, b):\n    return a + b\n'

_created: list[int] = []
_suppressed: set[str] = set()


# ---------------------------------------------------------------- helpers (local copies, like the other modules)

def make_company(name="VerifyCo", members=("ceo", "engineering", "sales", "research", "qa"), **settings) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    s = {"truth_scan_done": True, "postal_address": "1 Main St, Springfield", **settings}
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "Meal plans for dogs", "active", "building", json.dumps(s), db.now()))
    _created.append(cid)
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return db.get_company(cid)


def set_settings(company: dict, **values) -> dict:
    cs = json.loads(db.get_company(company["id"])["settings"] or "{}")
    cs.update(values)
    db.update("companies", company["id"], settings=json.dumps(cs))
    return db.get_company(company["id"])


def agent_of(company, role) -> dict:
    return db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))


def make_task(company, role, title, **cols) -> dict:
    a = agent_of(company, role)
    base = {"company_id": company["id"], "title": title, "role": role, "agent_id": a["id"], "status": "in_progress",
            "priority": 2, "created_by": "test", "created_at": db.now(), "updated_at": db.now(), **cols}
    tid = db.run(f"INSERT INTO tasks({', '.join(base)}) VALUES({','.join('?' * len(base))})", tuple(base.values()))
    return db.get_task(tid)


def start_run(company, role, task, started=None) -> int:
    return db.run("INSERT INTO runs(company_id, agent_id, task_id, kind, status, started_at) VALUES(?,?,?,?,?,?)",
                  (company["id"], agent_of(company, role)["id"], task["id"] if task else None, "task", "running",
                   started or db.now()))


def ctx_for(company, role, task=None, run_id=0) -> tools.Ctx:
    return tools.Ctx(company=db.get_company(company["id"]), agent=agent_of(company, role), kind="task",
                     run_id=run_id, task=task, settings=db.get_settings())


def write(ctx, path, content) -> None:
    out = tools.execute(ctx, "write_file", {"path": path, "content": content})
    assert out.startswith("Saved"), out


def verification(task) -> list[dict]:
    return json.loads(db.get_task(task["id"])["verification"] or "[]")


def make_inst(key, company=None, secrets=None, status="ok", enabled=1, **config_values) -> dict:
    return {"id": 70000 + len(_created) * 10 + len(key), "company_id": company["id"] if company else None, "key": key,
            "enabled": enabled, "config": dict(config_values), "status": status, "detail": "",
            "_secrets": dict(secrets or {})}


def connections(instances: dict | None = None, client=None) -> ExitStack:
    """Patch the framework helpers: instance(key, company), secrets(inst) and client(inst) (Phase 1's approach)."""
    stack = ExitStack()
    reg = dict(instances or {})
    stack.enter_context(mock.patch.object(connectors, "instance", lambda key, company=None: reg.get(key)))
    stack.enter_context(mock.patch.object(connectors, "secrets", lambda inst: dict((inst or {}).get("_secrets") or {})))
    if client is not None:
        stack.enter_context(mock.patch.object(connectors, "client", lambda inst: client, create=True))
    return stack


class FakeBrowserClient:
    """Stands in for the Playwright MCP client (as in tests/test_phase1_conn.py)."""

    def __init__(self, fail_on=None, console="", snapshot=""):
        self.calls = []
        self.fail_on, self.console, self.snapshot = fail_on, console, snapshot

    def call_tool(self, name, args):
        self.calls.append((name, dict(args or {})))
        if name == self.fail_on:
            raise mcp.MCPError("browser crashed")
        if name == "browser_console_messages":
            return {"content": [{"type": "text", "text": self.console}], "isError": False}
        if name == "browser_snapshot":
            return {"content": [{"type": "text", "text": self.snapshot}], "isError": False}
        if name == "browser_take_screenshot":
            return {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()}],
                    "isError": False}
        return {"content": [{"type": "text", "text": f"ran {name}"}], "isError": False}


class PreviewServer:
    """Serves /preview/<slug>/<path> from a company's site/ folder like the dashboard, plus /status/<code>."""

    def __init__(self, company: dict | None = None):
        site = workspace.safe_path(company, "site") if company else None
        prefix = f"/preview/{company['slug']}/" if company else "/preview/"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = urllib.parse.unquote(self.path.split("?")[0])
                if path.startswith("/status/"):
                    body, code = b"<html><title>status</title></html>", int(path.rsplit("/", 1)[1])
                else:
                    target = site / (path[len(prefix):] or "index.html") if site and path.startswith(prefix) else None
                    body, code = (target.read_bytes(), 200) if target and target.is_file() else (b"not found", 404)
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


class FakeDocker:
    """Stands in for subprocess.run when Holdco talks to Docker: `info` answers linux, `image inspect` fails until a
    `pull`, and `run` executes the python script on this PC with the mounted folder as the working directory (or
    answers `sh -c` commands from a script). Every command is recorded."""

    def __init__(self, ostype="linux", have_image=False, shell_exit=0, run_timeout=False):
        self.calls: list[list[str]] = []
        self.ostype, self.have_image, self.shell_exit, self.run_timeout = ostype, have_image, shell_exit, run_timeout

    def __call__(self, cmd, **kw):
        cmd = list(cmd)
        self.calls.append(cmd)
        verb = cmd[1]
        if verb == "info":
            return subprocess.CompletedProcess(cmd, 0, self.ostype.encode() + b"\n", b"")
        if verb == "image":
            return subprocess.CompletedProcess(cmd, 0 if self.have_image else 1, b"", b"No such image")
        if verb == "pull":
            self.have_image = True
            return subprocess.CompletedProcess(cmd, 0, b"Status: Downloaded newer image", b"")
        if verb == "kill":
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if verb == "run":
            if self.run_timeout:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            folder = cmd[cmd.index("-v") + 1].rpartition(":/work")[0]
            if cmd[-3:-1] == ["sh", "-c"]:
                return subprocess.CompletedProcess(cmd, self.shell_exit, b"ran " + cmd[-1].encode(), b"")
            script = Path(folder) / cmd[-1][len("/work/"):]
            proc = subprocess.run([sys.executable, str(script)], cwd=folder, capture_output=True, timeout=120,
                                  stdin=subprocess.DEVNULL)
            return subprocess.CompletedProcess(cmd, proc.returncode, proc.stdout, proc.stderr)
        raise AssertionError(f"unexpected docker command {cmd}")

    def runs(self) -> list[list[str]]:
        return [c for c in self.calls if c[1] == "run"]


def fake_docker(fake: FakeDocker) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(mock.patch.object(sandbox, "WHICH", lambda name: f"/usr/bin/{name}" if name == "docker" else None))
    stack.enter_context(mock.patch.object(sandbox, "RUNNER", fake))
    stack.enter_context(mock.patch.dict(os.environ, {"HOLDCO_SANDBOX": ""}))
    sandbox.reset()
    stack.callback(sandbox.reset)
    return stack


def tearDownModule():
    for cid in _created:
        db.run("UPDATE runs SET status='done', finished_at=? WHERE company_id=? AND status='running'", (db.now(), cid))
        db.update("companies", cid, status="archived")
    sandbox.reset()


class Base(unittest.TestCase):
    def setUp(self):
        db.set_settings({"kill_switch": False})
        self._patches = [mock.patch.object(notify, "send", lambda *a, **kw: True),
                         mock.patch.object(notify, "send_later", lambda *a, **kw: None),
                         mock.patch.object(sandbox, "available", lambda refresh=False: None)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        if _suppressed:
            db.run(f"DELETE FROM suppression WHERE email IN ({','.join('?' * len(_suppressed))})", list(_suppressed))
            _suppressed.clear()

    def suppress(self, address, reason="test"):
        _suppressed.add(verify._addr(address))
        verify.suppress(address, reason)


# =============================================================================================== the gate

class TestGate(Base):
    # ---------------------------------------------------------------- web page
    def test_web_page_browser_and_static(self):
        c = make_company("PageCo")
        task = make_task(c, "engineering", "Build the waitlist page", deliverable="site/index.html")
        ctx = ctx_for(c, "engineering", task)
        write(ctx, "site/index.html", PAGE)
        write(ctx, "site/about.html", ABOUT)
        server = PreviewServer(c)
        self.addCleanup(server.stop)
        pw = make_inst("playwright")
        client = FakeBrowserClient(console="[LOG] ready", snapshot="- Page Title: PawPlan\n")
        with connections({"playwright": pw}, client), mock.patch.object(config, "PORT", server.port):
            # With Playwright: the form without a target fails; finish is refused once, then downgraded to partial.
            first = tools.execute(ctx, "finish", {"summary": "Built the page", "deliverable": "site/index.html"})
            self.assertTrue(first.startswith("Not finished yet: web page: site/index.html: form 1 (waitlist) has no "
                                             "action or data-netlify attribute"), first)
            self.assertIn("browser_check site/index.html shows the full result", first)
            [r] = verification(task)
            self.assertEqual((r["kind"], r["passed"], r["mode"]), ("web page", False, "browser"))
            again = tools.execute(ctx, "finish", {"summary": "Built the page", "deliverable": "site/index.html"})
            self.assertEqual(again, "Session ended.")
            self.assertEqual(ctx.finish_status, "partial")
            self.assertIn("Verification failed: web page: site/index.html: form 1", ctx.finish_summary)
            # The same page with a target passes the browser rule: 200, no console errors, phone screenshot.
            fixed = ctx_for(c, "engineering", task)
            write(fixed, "site/index.html", FIXED)
            self.assertEqual(tools.execute(fixed, "finish", {"summary": "Fixed the form",
                                                             "deliverable": "site/index.html"}), "Session ended.")
            self.assertEqual(fixed.finish_status, "done")
            [r] = verification(task)
            self.assertEqual((r["passed"], r["mode"]), (True, "browser"))
            self.assertIn("HTTP 200", r["detail"])
            self.assertIn("phone screenshot .qa/index-mobile.png", r["detail"])
            self.assertEqual(verify.summary_line(db.get_task(task["id"])), "[gate: web page PASS (browser)]")
            # Console errors fail the browser rule.
            noisy = FakeBrowserClient(console="[ERROR] Uncaught TypeError: plan is undefined @ app.js:3")
            with mock.patch.object(connectors, "client", lambda inst: noisy, create=True):
                problem = verify.finish_problem(ctx_for(c, "engineering", task), "site/index.html", "", "")
            self.assertIn("1 console error(s): [ERROR] Uncaught TypeError: plan is undefined", problem)
        # Playwright absent: the static check counts and is labelled.
        with connections({}), mock.patch.object(config, "PORT", server.port):
            self.assertIsNone(verify.finish_problem(ctx_for(c, "engineering", task), "site/index.html", "", ""))
            [r] = verification(task)
            self.assertEqual((r["passed"], r["mode"]), (True, "static"))
            self.assertIn("0 audit blockers", r["detail"])
            self.assertIn("static check: Playwright is not set up", r["detail"])
            self.assertEqual(verify.summary_line(db.get_task(task["id"])), "[gate: web page PASS (static)]")
            # A blocker fails the static rule (no viewport meta tag).
            write(ctx_for(c, "engineering", task), "site/index.html", FIXED.replace('<meta name="viewport"', "<meta"))
            problem = verify.finish_problem(ctx_for(c, "engineering", task), "site/index.html", "", "")
            self.assertTrue(problem.startswith("web page: site/index.html: 1 audit blocker(s): No viewport meta tag"),
                            problem)
        write(ctx_for(c, "engineering", task), "site/index.html", FIXED)
        # Playwright enabled but failing: the static result does not count.
        with connections({"playwright": pw}, FakeBrowserClient(fail_on="browser_snapshot")), \
                mock.patch.object(config, "PORT", server.port):
            problem = verify.finish_problem(ctx_for(c, "engineering", task), "site/index.html", "", "")
        self.assertIn("browser_check fell back to a static check (Playwright failed: browser crashed), which does not "
                      "count while the Playwright connection is set up", problem)
        self.assertEqual(verification(task)[0]["mode"], "static")
        # A disabled Playwright card is "not enabled": the static result counts again.
        with connections({"playwright": make_inst("playwright", enabled=0)}), \
                mock.patch.object(config, "PORT", server.port):
            self.assertIsNone(verify.finish_problem(ctx_for(c, "engineering", task), "site/index.html", "", ""))
        # Not a site page: no web gate.
        self.assertEqual(verify.run(ctx_for(c, "engineering", task), "docs/plan.md"), [])

    def test_judge_browser_check_lines(self):
        c = make_company("JudgeCo")
        qa = workspace.root(c) / ".qa"
        qa.mkdir(parents=True, exist_ok=True)
        (qa / "index-mobile.png").write_bytes(PNG)
        good = ("Browser check (Playwright) of site/index.html\nGET http://127.0.0.1:1/preview/x/index.html -> 200\n"
                "Title: T\nH1: H\nConsole errors: none\nLinks: 2 checked, 2 ok\nForms: 1 found, 1 with a target "
                "(action)\nScreenshots: .qa/index.png, .qa/index-mobile.png")
        with connections({"playwright": make_inst("playwright")}):
            ok, problems, mode, _ = verify.judge_browser_check(c, good)
            self.assertEqual((ok, problems, mode), (True, [], "browser"))
            ok, problems, _, _ = verify.judge_browser_check(c, good.replace("-> 200", "-> 404").replace(
                "Links: 2 checked, 2 ok", "Links: 2 checked, 1 ok\n- broken: x.html -> 404").replace(
                ".qa/index-mobile.png", "no phone-sized screenshot (browser crashed)"))
        self.assertFalse(ok)
        self.assertEqual(problems[0], "the page did not load: GET http://127.0.0.1:1/preview/x/index.html -> 404")
        self.assertIn("no phone-sized screenshot (browser crashed): run browser_check again with mobile true", problems)
        self.assertIn("1 broken link(s): x.html -> 404", problems)
        with connections({}):
            ok, problems, _, _ = verify.judge_browser_check(c, "Something else entirely")
        self.assertFalse(ok)
        self.assertIn("no result the gate can read", problems[0])

    # ---------------------------------------------------------------- code
    def test_code_tests_in_sandbox_or_policy(self):
        c = make_company("CodeCo")
        task = make_task(c, "engineering", "Write the price calculator", deliverable="code/calc.py")
        ctx = ctx_for(c, "engineering", task)
        write(ctx, "code/__init__.py", "")
        write(ctx, "code/calc.py", CALC_BAD)
        write(ctx, "code/test_calc.py", "import unittest\nfrom code.calc import add\n\n\n"
                                        "class TestAdd(unittest.TestCase):\n    def test_add(self):\n"
                                        "        self.assertEqual(add(1, 2), 3)\n")
        fake = FakeDocker()
        self._patches[-1].stop()   # this test brings its own (fake) Docker
        self._patches.pop()
        with fake_docker(fake):
            problem = verify.finish_problem(ctx, "code/calc.py", "", "")
            self.assertTrue(problem.startswith("code: the tests failed (exit code 1): FAIL: test_add"), problem)
            self.assertIn("AssertionError: -1 != 3", problem)
            [r] = verification(task)
            self.assertEqual((r["kind"], r["passed"], r["mode"]), ("code", False, "docker"))
            run = fake.runs()[0]
            self.assertEqual(run[:2], ["docker", "run"])
            self.assertIn("--network", run)
            self.assertEqual(run[-3], "python:3.12-slim")
            self.assertTrue(any(cmd[1] == "pull" for cmd in fake.calls))   # the first run downloads the image
            write(ctx, "code/calc.py", CALC_GOOD)
            self.assertIsNone(verify.finish_problem(ctx, "code/calc.py", "", ""))
            [r] = verification(task)
            self.assertEqual((r["passed"], r["mode"]), (True, "docker"))
            self.assertIn("python -m unittest discover -s code -t . passed (Ran 1 test", r["detail"])
            self.assertEqual(sum(1 for cmd in fake.calls if cmd[1] == "pull"), 1)
        # No runtime and the run_code policy 'approve': the tests are queued and the task finishes waiting.
        with mock.patch.object(sandbox, "available", lambda refresh=False: None):
            write(ctx, "code/calc.py", CALC_BAD)
            nxt = ctx_for(c, "engineering", task)
            first = verify.finish_problem(nxt, "code/calc.py", "Calculator written", "")
            queued = db.one("SELECT * FROM approvals WHERE company_id=? AND task_id=? AND action='run_code' "
                            "AND status='pending'", (c["id"], task["id"]))
            self.assertIsNotNone(queued)
            self.assertTrue(first.startswith(f"code: tests need to run: approve request #{queued['id']}."), first)
            self.assertIn("finish with status 'waiting'", first)
            self.assertEqual(verify.waiting_request(nxt), queued["id"])
            self.assertEqual(verification(task)[0]["mode"], "waiting")
            self.assertEqual(json.loads(queued["payload"])["code"], verify.TEST_SCRIPT)
            self.assertEqual(tools.execute(nxt, "finish", {"summary": "Waiting for the test run", "status": "waiting"}),
                             "Session ended.")
            self.assertEqual(nxt.finish_status, "waiting")
            self.assertIn(f"Waiting on the owner: #{queued['id']}", nxt.finish_summary)
            # Asking again while it waits does not queue a second request.
            self.assertIn(f"approve request #{queued['id']}",
                          verify.finish_problem(ctx_for(c, "engineering", task), "code/calc.py", "", ""))
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM approvals WHERE task_id=? AND action='run_code'",
                                       (task["id"],)), 1)
            # The owner approves: the tests run on this PC and fail; the next attempt reads the stored result.
            decided = actions.decide(queued["id"], "approve")
            self.assertEqual(decided["status"], "done")
            row = db.one("SELECT * FROM approvals WHERE id=?", (queued["id"],))
            self.assertTrue(row["result"].startswith("Sandbox: none (ran on this PC)\nExit code 1"), row["result"])
            retry = ctx_for(c, "engineering", task)
            problem = verify.finish_problem(retry, "code/calc.py", "", "")
            self.assertTrue(problem.startswith("code: the tests failed (exit code 1)"), problem)
            self.assertEqual(verification(task)[0]["mode"], f"approved #{queued['id']}")
            # The agent fixes the code in this session: the gate queues a new run, and its approval passes.
            write(retry, "code/calc.py", CALC_GOOD)
            problem = verify.finish_problem(retry, "code/calc.py", "", "")
            second = db.one("SELECT * FROM approvals WHERE task_id=? AND action='run_code' AND status='pending'",
                            (task["id"],))
            self.assertIn(f"tests need to run: approve request #{second['id']}", problem)
            actions.decide(second["id"], "approve")
            self.assertIsNone(verify.finish_problem(ctx_for(c, "engineering", task), "code/calc.py", "", ""))
            self.assertEqual(verification(task)[0]["mode"], f"approved #{second['id']}")
            # Policy 'auto': the gate runs them at once on this PC (and the audit log shows it).
            other = make_task(c, "engineering", "Write the tax helper", deliverable="code/")
            set_settings(c, policies={"run_code": "auto"})
            auto = ctx_for(c, "engineering", other)
            self.assertIsNone(verify.finish_problem(auto, "code/", "", ""))
            [r] = verification(other)
            self.assertEqual((r["passed"], r["mode"]), (True, "this PC"))
            self.assertTrue(db.one("SELECT id FROM audit WHERE company_id=? AND action='run_code' AND "
                                   "approved_by='policy:auto'", (c["id"],)))
            # Policy 'off': the tests cannot run, and the gate says what to do.
            set_settings(c, policies={"run_code": "off"})
            problem = verify.finish_problem(ctx_for(c, "engineering", other), "code/", "", "")
            self.assertIn("the owner has turned off running code for this company", problem)
            # No tests at all: the default command finds none.
            bare = make_company("BareCo")
            set_settings(bare, policies={"run_code": "auto"})
            t3 = make_task(bare, "engineering", "Write the script", deliverable="code/tool.py")
            c3 = ctx_for(bare, "engineering", t3)
            write(c3, "code/tool.py", "print('hi')\n")
            write(c3, "code/__init__.py", "")
            self.assertIn("no tests ran", verify.finish_problem(c3, "code/tool.py", "", ""))
            # A test file written in this task makes the code gate apply even when the deliverable is elsewhere.
            t4 = make_task(bare, "engineering", "Document the script", deliverable="docs/tool.md")
            c4 = ctx_for(bare, "engineering", t4)
            self.assertEqual(verify.run(c4, "docs/tool.md"), [])
            write(c4, "code/test_tool.py", "import unittest\n\n\nclass T(unittest.TestCase):\n"
                                           "    def test_ok(self):\n        self.assertTrue(True)\n")
            self.assertEqual([r["kind"] for r in verify.run(c4, "docs/tool.md")], ["code"])

    # ---------------------------------------------------------------- payment link
    def test_payment_link(self):
        fake = FakeStripe().start()
        self.addCleanup(fake.stop)
        c = make_company("PayCo")
        task = make_task(c, "engineering", "Create the Starter payment link")
        ctx = ctx_for(c, "engineering", task)
        inst = make_inst("stripe", c, {"api_key": STRIPE_KEY}, api_base=fake.url)
        with connections({"stripe": inst}):
            made = conn_stripe.create_payment_link(inst, c, "Starter plan", 19, recurring_interval="month")
            url = made.split()[3]
            self.assertIsNone(verify.finish_problem(ctx, url, "Starter plan payment link created", ""))
            [r] = verification(task)
            self.assertEqual((r["kind"], r["passed"], r["mode"]), ("payment link", True, "test mode"))
            self.assertIn("(plink_1) is active", r["detail"])
            # An id the fake does not know fails; so does a link turned off.
            unknown = url.replace("plink_1", "plink_99")
            problem = verify.finish_problem(ctx, "", f"Link: {unknown}.", "")
            self.assertEqual(problem, f"payment link: {unknown} is not a payment link in this Stripe account (test "
                                      f"mode). Hand off the URL that stripe__create_payment_link returned")
            fake.links["plink_1"]["active"] = False
            self.assertIn("is turned off in Stripe (active: false)", verify.finish_problem(ctx, "", "", url))
            self.assertEqual(conn_stripe.find_payment_link(inst, "plink_1")["id"], "plink_1")
            self.assertIsNone(conn_stripe.find_payment_link(inst, "plink_42"))
        # No Stripe connector: the exact text.
        with connections({}):
            problem = verify.finish_problem(ctx, "", "Pay at https://buy.stripe.com/test_eVa5kE", "")
        self.assertEqual(problem, "payment link: no Stripe connector is set up")
        # No payment link in the handoff: no payment gate.
        with connections({}):
            self.assertEqual(verify.run(ctx, "", "Wrote the copy", ""), [])

    # ---------------------------------------------------------------- outreach
    def test_outreach_rules(self):
        c = make_company("MailCo")
        task = make_task(c, "sales", "Prepare intro notes for Jo")
        run_id = start_run(c, "sales", task)
        ctx = ctx_for(c, "sales", task, run_id)
        good_body = ("Hi Ann,\n\nMailCo plans dog meals each week. Worth a look?\n\n--\nMailCo\n1 Main St, Springfield\n"
                     "Reply 'stop' and we won't email you again.")
        ann = "ann@shop-verify.com"
        self.assertEqual(verify.outreach_check(ctx, {"to": ann, "subject": "Hi", "body": good_body}), [])
        # Each failing case says what is wrong.
        cases = {
            "no opt-out line": {"to": ann, "subject": "Hi", "body": good_body.replace(
                "Reply 'stop' and we won't email you again.", "")},
            "the email does not name the sender": {"to": ann, "subject": "Hi", "body": good_body.replace(
                "MailCo", "We")},
            "more than one link": {"to": ann, "subject": "Hi", "body": good_body.replace(
                "Worth a look?", "See https://a.example.org and https://b.example.org. Worth a look?")},
            "it looks like a testimonial": {"to": ann, "subject": "Hi", "body": good_body.replace(
                "Worth a look?", "\"Best meal plans our dog ever had\" - Sarah K. Worth a look?")},
            "compliance claims (gdpr compliant)": {"to": ann, "subject": "Hi", "body": good_body.replace(
                "Worth a look?", "We are GDPR compliant. Worth a look?")},
        }
        for start, payload in cases.items():
            with self.subTest(start):
                problems = verify.outreach_check(ctx, payload)
                self.assertTrue(problems and problems[0].startswith(start), problems)
        self.assertEqual(verify.outreach_check(ctx, cases["no opt-out line"])[0],
                         "no opt-out line: end the email with a line such as \"Reply 'stop' and we won't email you "
                         "again.\"")
        # The recipient is on the do-not-email list.
        bob = "bob@suppressed-verify.com"
        self.suppress(bob, "unsubscribed (contact #1)")
        self.assertEqual(verify.outreach_check(ctx, {"to": bob.upper(), "subject": "Hi", "body": good_body}),
                         [f"recipient is on the do-not-email list: {bob} (unsubscribed (contact #1)); never email "
                          "them again"])
        # An EU contact without recorded consent; with consent the same email passes.
        jo = "jo@hundeplan-verify.de"
        cid_row = db.run("INSERT INTO contacts(company_id, name, email, consent, created_at) VALUES(?,?,?,?,?)",
                         (c["id"], "Jo", jo, "", db.now()))
        eu = verify.outreach_check(ctx, {"to": jo, "subject": "Hi", "body": good_body})
        self.assertEqual(len(eu), 1)
        self.assertTrue(eu[0].startswith(f"EU contact without recorded consent: {jo} is a .de address (Germany)"), eu)
        db.update("contacts", cid_row, consent="opted in at the Berlin dog fair, 2026-10-01")
        self.assertEqual(verify.outreach_check(ctx, {"to": jo, "subject": "Hi", "body": good_body}), [])
        self.assertIn("eu", verify.EU_TLDS)
        # A reply to someone who wrote first is not commercial: no opt-out needed, but it must name the sender.
        rae = "rae@customer-verify.com"
        db.run("INSERT INTO emails(company_id, direction, from_addr, to_addr, subject, body, status, created_at) "
               "VALUES(?,?,?,?,?,?,?,?)", (c["id"], "in", f"Rae <{rae}>", "hi@mailco.example", "Question", "Hello?",
                                           "new", db.now()))
        self.assertEqual(verify.outreach_check(ctx, {"to": rae, "subject": "Re: Question",
                                                     "body": "Hi Rae, yes we do. MailCo"}), [])
        self.assertTrue(verify.outreach_check(ctx, {"to": rae, "subject": "Re", "body": "Hi Rae, yes."})[0]
                        .startswith("the email does not name the sender"))
        # Over the daily cap (sent today plus queued).
        set_settings(c, daily_email_limit=1)
        db.run("INSERT INTO approvals(company_id, agent_id, task_id, action, summary, payload, status, created_at) "
               "VALUES(?,?,?,?,?,?,?,?)", (c["id"], agent_of(c, "sales")["id"], None, "send_email", "x",
                                           json.dumps({"to": "z@z-verify.com"}), "pending", db.now()))
        self.assertEqual(verify.outreach_check(ctx_for(c, "sales", task, run_id), {"to": ann, "subject": "Hi",
                                                                                   "body": good_body}),
                         ["daily email limit reached: 1 sent or queued today and the limit is 1; send the rest "
                          "tomorrow"])
        self.assertEqual(verify.outreach_check(ctx_for(c, "sales", task, run_id), {"to": ann, "body": good_body},
                                               queued=True), [])   # at finish the email itself is among them

    def test_outreach_on_both_paths(self):
        """send_email refuses each failing case and queues nothing, even when the policy runs emails at once; the
        finish-time gate fails a run whose queued email became non-compliant afterwards."""
        c = make_company("SendCo", policies={"send_email": "auto"})
        task = make_task(c, "sales", "Prepare intro notes for the dog shops")
        run_id = start_run(c, "sales", task, started="2020-01-01T00:00:00Z")
        ctx = ctx_for(c, "sales", task, run_id)
        db.run("INSERT OR REPLACE INTO trust(company_id, action, level, streak, cap_per_day, updated_at) "
               "VALUES(?,?,?,?,?,?)", (c["id"], "send_email", 2, 0, 0, db.now()))   # trust level 2 skips nothing
        jo = "jo@shop-eu-verify.de"
        db.run("INSERT INTO contacts(company_id, name, email, consent, created_at) VALUES(?,?,?,?,?)",
               (c["id"], "Jo", jo, "", db.now()))
        bob = "bob@gone-verify.com"
        self.suppress(bob)
        for known in ("dee@shop-d-verify.com", "eve@shop-e-verify.com"):   # not a first contact (trust ladder)
            db.run("INSERT INTO contacts(company_id, name, email, consent, last_touch, created_at) VALUES(?,?,?,?,?,?)",
                   (c["id"], known.split("@")[0].title(), known, "", "2026-10-01", db.now()))
        cases = [
            # "stop" in the body keeps outreach_guard from adding its opt-out line; review_text finds none
            ({"to": "ann@shop-a-verify.com", "subject": "Hi", "body": "Hi Ann, SendCo helps stop leaks. Worth a look?"},
             "ERROR: email not sent: no opt-out line"),
            ({"to": jo, "subject": "Hi", "body": "Hi Jo, SendCo plans meals. Worth a look?"},
             "ERROR: email not sent: EU contact without recorded consent"),
            ({"to": "cy@shop-c-verify.com", "subject": "Hi",
              "body": "Hi, SendCo is GDPR compliant. Worth a look? https://a.example.org https://b.example.org"},
             "ERROR: email not sent: compliance claims (gdpr compliant): remove them unless the company has evidence; "
             "more than one link: keep one link or none"),
            ({"to": bob, "subject": "Hi", "body": "Hi Bob, SendCo plans meals. Worth a look?"},
             "ERROR: email not queued: bob@gone-verify.com asked not to be emailed again"),   # truth's guard first
        ]
        for payload, start in cases:
            with self.subTest(payload["to"]):
                out = tools.execute(ctx, "send_email", payload)
                self.assertTrue(out.startswith(start), out)
                self.assertFalse(db.q("SELECT id FROM approvals WHERE company_id=? AND action='send_email'",
                                      (c["id"],)))
                self.assertFalse(db.q("SELECT id FROM audit WHERE company_id=? AND action='send_email'", (c["id"],)))
        # A compliant email goes through the policy (auto: sent at once; no mailbox here, so it fails to send).
        ok = tools.execute(ctx, "send_email", {"to": "dee@shop-d-verify.com", "subject": "Hi",
                                               "body": "Hi Dee, SendCo plans dog meals. Worth a look?"})
        self.assertFalse(ok.startswith("ERROR"), ok)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM audit WHERE company_id=? AND action='send_email'", (c["id"],)), 1)
        # Under 'approve' the next one is queued; then the recipient unsubscribes before the run finishes.
        set_settings(c, policies={"send_email": "approve"})
        db.run("DELETE FROM trust WHERE company_id=?", (c["id"],))
        ctx = ctx_for(c, "sales", task, run_id)
        queued = tools.execute(ctx, "send_email", {"to": "eve@shop-e-verify.com", "subject": "Hi",
                                                   "body": "Hi Eve, SendCo plans dog meals. Worth a look?"})
        self.assertTrue(queued.startswith("Queued for the owner's approval as request #"), queued)
        aid = int(queued.split("#")[1].split(".")[0])
        self.assertIsNone(verify.finish_problem(ctx, "", "", ""))
        [r] = verification(task)
        self.assertEqual((r["kind"], r["passed"]), ("outreach", True))
        self.assertTrue(r["detail"].startswith("2 email(s) checked"), r["detail"])
        self.suppress("eve@shop-e-verify.com", "replied stop")
        problem = verify.finish_problem(ctx, "", "", "")
        self.assertTrue(problem.startswith(f"outreach: recipient is on the do-not-email list in request #{aid} to "
                                           "eve@shop-e-verify.com: eve@shop-e-verify.com (replied stop)"), problem)
        self.assertIn("finish with status 'partial' and say which request the owner should reject or fix", problem)
        line = verify.summary_line(db.get_task(task["id"]))
        self.assertTrue(line.startswith(f"[gate: outreach FAIL: recipient is on the do-not-email list in request "
                                        f"#{aid}"), line)
        self.assertTrue(line.endswith("...]") and len(line) <= len("[gate: outreach FAIL: ]") + 90, line)
        # The email the auto policy sent counts too.
        self.suppress("dee@shop-d-verify.com")
        self.assertIn("in the email sent to dee@shop-d-verify.com", verify.finish_problem(ctx, "", "", ""))
        # An email queued before this run started is another run's business.
        late = ctx_for(c, "sales", task, start_run(c, "sales", task, started="2999-01-01T00:00:00Z"))
        self.assertEqual(verify.run(late), [])

    # ---------------------------------------------------------------- research
    def test_research_citations(self):
        c = make_company("ResearchCo")
        task = make_task(c, "research", "Scan competitor pricing", deliverable="research/competitors.md")
        ctx = ctx_for(c, "research", task)
        write(ctx, "research/competitors.md", "# Competitors\n\n- Ollie sells fresh dog food "
                                              "(https://www.ollie-verify.com/pricing/#plans).\n- Spot and Tango "
                                              "(<https://spot-verify.com/plans>)\n")
        problem = verify.finish_problem(ctx, "research/competitors.md", "", "")
        self.assertEqual(problem, "research: research/competitors.md cites 2 URL(s) this task never fetched: "
                                  "https://www.ollie-verify.com/pricing/#plans, https://spot-verify.com/plans. Fetch "
                                  "each with fetch_url, or remove it from the file")
        pages = {"https://ollie-verify.com/pricing": "Title: Ollie pricing\n\nPlans from $9",
                 "https://spot-verify.com/plans/": "Title: Spot plans\n\nPlans"}
        with mock.patch.object(web, "fetch_page", lambda url, include_links=False, **kw: pages[url]):
            for url in pages:
                tools.execute(ctx, "fetch_url", {"url": url})
        # Fragments, a trailing slash, www. and trailing punctuation do not make a different URL.
        self.assertIsNone(verify.finish_problem(ctx, "research/competitors.md", "", ""))
        [r] = verification(task)
        self.assertEqual((r["kind"], r["passed"], r["detail"]), ("research", True,
                                                                  "2 cited URL(s), all fetched in this task"))
        # A page another task fetched does not count for this one.
        other = make_task(c, "research", "Scan forums", deliverable="research/forums.md")
        octx = ctx_for(c, "research", other)
        write(octx, "research/forums.md", "# Forums\n\nOwners ask about portions: https://spot-verify.com/plans\n")
        self.assertIn("cites 1 URL(s) this task never fetched", verify.finish_problem(octx, "research/forums.md", "",
                                                                                     ""))
        # A file with no URL fails.
        write(octx, "research/forums.md", "# Forums\n\nOwners ask about portions.\n")
        self.assertEqual(verify.finish_problem(octx, "research/forums.md", "", ""),
                         "research: research/forums.md cites no source URL. Fetch the pages you used with fetch_url "
                         "and cite each page's URL next to what it supports")

    # ---------------------------------------------------------------- checks lines
    def test_checks_lines(self):
        self.assertEqual(verify._check_lines("- browser_check site/index.html\n2. `audit_page site/a.html`\r\n"
                                             "review_text: docs/e.md cold_email\n\n* check_contrast #777 #fff"),
                         [("browser_check", "site/index.html"), ("audit_page", "site/a.html"),
                          ("review_text", "docs/e.md cold_email"), ("check_contrast", "#777 #fff")])
        c = make_company("ChecksCo")
        server = PreviewServer(c)
        self.addCleanup(server.stop)
        checks_text = "\n".join([
            "browser_check site/index.html",
            "audit_page site/index.html",
            "review_text docs/email.md cold_email",
            "check_contrast #555555 #ffffff",
            f"fetch_url http://127.0.0.1:{server.port}/status/200",
        ])
        task = make_task(c, "qa", "Review the landing page", deliverable="docs/review.md", checks=checks_text)
        ctx = ctx_for(c, "qa", task)
        write(ctx, "site/index.html", FIXED)
        write(ctx, "site/about.html", ABOUT)
        write(ctx, "docs/email.md", "Hi Ann, ChecksCo plans dog meals. Worth a look? Not interested? Reply 'no' and "
                                    "I won't follow up.")
        with connections({}), mock.patch.object(config, "PORT", server.port), \
                mock.patch.object(web, "ALLOW_LOCAL", ["127.0.0.1:*"]):
            results = verify.run(ctx, "docs/review.md")
            self.assertEqual([(r["kind"], r["passed"]) for r in results],
                             [("web page", True), ("audit_page", True), ("review_text", True),
                              ("check_contrast", True), ("fetch_url", True)])
            self.assertEqual(results[0]["mode"], "static")
            self.assertEqual(results[3]["detail"], "#555555 on #ffffff is 7.46:1")
            self.assertEqual(results[4]["detail"], f"http://127.0.0.1:{server.port}/status/200 answered HTTP 200")
            # The failing form of each.
            write(ctx, "site/index.html", FIXED.replace('<meta name="viewport"', "<meta"))
            write(ctx, "docs/email.md", "Hi Ann, we are GDPR compliant. Worth a look? https://a.example.org "
                                        "https://b.example.org")
            db.update("tasks", task["id"], checks=checks_text.replace("#555555", "#777777").replace("/200", "/404"))
            results = verify.run(ctx, "docs/review.md")
            self.assertEqual([r["passed"] for r in results], [False] * 5)
            self.assertIn("1 audit blocker(s): No viewport meta tag", results[0]["detail"])
            self.assertTrue(results[1]["detail"].startswith("site/index.html has 1 audit blocker(s): No viewport"))
            self.assertIn("Compliance claims need evidence: gdpr compliant", results[2]["detail"])
            self.assertIn("No opt-out line", results[2]["detail"])
            self.assertIn("More than one link", results[2]["detail"])
            self.assertEqual(results[3]["detail"], "#777777 on #ffffff is 4.48:1, below 4.5:1 for normal text: darken "
                                                   "the text or lighten the background")
            self.assertEqual(results[4]["detail"], f"http://127.0.0.1:{server.port}/status/404 answered HTTP 404")
            # finish_problem names the first failure and the other failing kinds.
            problem = verify.finish_problem(ctx, "docs/review.md", "", "")
            self.assertTrue(problem.startswith("web page: site/index.html: 1 audit blocker(s)"), problem)
            self.assertTrue(problem.endswith("(also failing: audit_page, review_text, check_contrast, fetch_url)"),
                            problem)
        # A browser_check line for the deliverable itself runs once.
        db.update("tasks", task["id"], checks="browser_check index.html")
        write(ctx, "site/index.html", FIXED)
        with connections({}), mock.patch.object(config, "PORT", server.port):
            self.assertEqual([r["kind"] for r in verify.run(ctx, "site/index.html")], ["web page"])
        # run_tests belongs to the code kind and runs in the sandbox when there is one.
        db.update("tasks", task["id"], checks="run_tests pytest -q code")
        fake = FakeDocker(have_image=True, shell_exit=1)
        self._patches[-1].stop()
        self._patches.pop()
        with fake_docker(fake):
            [r] = verify.run(ctx, "docs/review.md")
            self.assertEqual((r["kind"], r["passed"], r["mode"]), ("code", False, "docker"))
            self.assertEqual(fake.runs()[0][-3:], ["sh", "-c", "pytest -q code"])
            self.assertTrue(r["detail"].startswith("the tests failed (exit code 1)"), r["detail"])
            # The owner's "off" for running code holds even when a container is available.
            set_settings(c, policies={"run_code": "off"})
            [r] = verify.run(ctx_for(c, "qa", task), "docs/review.md")
            self.assertEqual(len(fake.runs()), 1)
        self.assertEqual(r["detail"], "the owner has turned off running code for this company, so the tests (pytest -q "
                                      "code) cannot run. Finish with status 'partial' and say the tests have not run")

    # ---------------------------------------------------------------- summary and storage
    def test_summary_line(self):
        task = {"verification": json.dumps([
            {"kind": "web page", "passed": True, "detail": "site/index.html: HTTP 200", "mode": "browser"},
            {"kind": "outreach", "passed": False, "detail": "no opt-out line in request #3 to a@b.com: end the email "
                                                            "with an opt-out line; more. And more", "mode": ""},
            {"kind": "research", "passed": True, "detail": "not checked: KeyError: 'x'", "mode": "not checked"}])}
        self.assertEqual(verify.summary_line(task), "[gate: web page PASS (browser); outreach FAIL: no opt-out line in "
                                                    "request #3 to a@b.com: end the email with an opt-out line; "
                                                    "research not checked]")
        self.assertEqual(verify.summary_line({"verification": ""}), "")
        self.assertEqual(verify.summary_line({"verification": "[]"}), "")
        self.assertEqual(verify.summary_line({"verification": "not json"}), "")
        # Without the column in the row it is read from the database; a task the gate never saw gives nothing.
        c = make_company("SummaryCo")
        t = make_task(c, "ceo", "Plan")
        self.assertEqual(verify.summary_line({"id": t["id"]}), "")
        db.update("tasks", t["id"], verification=task["verification"])
        self.assertTrue(verify.summary_line({"id": t["id"]}).startswith("[gate: web page PASS (browser)"))
        # A check that crashes is recorded as not checked and never stops the work.
        ctx = ctx_for(c, "ceo", t)
        with mock.patch.object(verify, "_research_kind", side_effect=RuntimeError("boom")):
            self.assertIsNone(verify.finish_problem(ctx, "research/x.md", "", ""))
        self.assertEqual(verification(t), [{"kind": "research", "passed": True, "detail": "not checked: RuntimeError: "
                                                                                          "boom", "mode": "not checked"}])
        self.assertIsNone(verify.finish_problem(ctx_for(c, "ceo", None), "", "", ""))   # no task, no gate

    # ---------------------------------------------------------------- suppression
    def test_suppression_list(self):
        c = make_company("StopCo")
        other = make_company("OtherStopCo")
        self.assertFalse(verify.is_suppressed("Kim <kim@stop-verify.com>"))
        self.suppress("Kim <KIM@stop-verify.com>", "owner pressed Suppress")
        self.assertTrue(verify.is_suppressed("kim@stop-verify.com"))
        self.assertTrue(truth.suppressed("kim@stop-verify.com"))   # Phase 0.5's guard reads the table too
        self.assertEqual(db.one("SELECT reason FROM suppression WHERE email=?", ("kim@stop-verify.com",))["reason"],
                         "owner pressed Suppress")
        verify.suppress("not an address")
        self.assertIsNone(db.one("SELECT * FROM suppression WHERE email='not an address'"))
        # update_contact(unsubscribed=true) adds the address, so no company emails it again.
        lee = "lee@stop-verify.com"
        _suppressed.add(lee)
        cid_row = db.run("INSERT INTO contacts(company_id, name, email, created_at) VALUES(?,?,?,?)",
                         (c["id"], "Lee", lee, db.now()))
        out = brain.update_contact(ctx_for(c, "sales"), cid_row, unsubscribed=True)
        self.assertIn("They are unsubscribed", out)
        self.assertEqual(db.one("SELECT reason, company_id FROM suppression WHERE email=?", (lee,)),
                         {"reason": f"unsubscribed (contact #{cid_row})", "company_id": c["id"]})
        problems = verify.outreach_check(ctx_for(other, "sales"), {"to": lee, "subject": "Hi", "body": "OtherStopCo"})
        self.assertTrue(problems[0].startswith(f"recipient is on the do-not-email list: {lee}"), problems)
        # A contact marked unsubscribed in any company counts, even without a table row.
        db.run("INSERT INTO contacts(company_id, name, email, unsubscribed, created_at) VALUES(?,?,?,?,?)",
               (other["id"], "Max", "max@stop-verify.com", 1, db.now()))
        self.assertTrue(verify.is_suppressed("MAX@stop-verify.com"))
        self.assertFalse(truth.suppressed("max@stop-verify.com"))


# =============================================================================================== the sandbox

class TestSandbox(Base):
    def test_command_line(self):
        cmd = sandbox.command("docker", 7, "/home/me/holdco/data/companies/acme", 1700000000000, "python",
                              windows=False)
        self.assertEqual(cmd, ["docker", "run", "--rm", "--name", "holdco-7-1700000000000",
                               "-v", "/home/me/holdco/data/companies/acme:/work", "-w", "/work",
                               "--network", "none",
                               "--memory", "1g", "--cpus", "1", "--pids-limit", "256",
                               "--read-only", "--tmpfs", "/tmp",
                               "python:3.12-slim", "python", "/work/.runs/run_1700000000000.py"])
        cmd = sandbox.command("podman", 7, r"c:\Users\Me\Holdco\data\companies\acme", 5, "shell", "ls -la | wc -l",
                              network=True, windows=True)
        self.assertEqual(cmd, ["podman", "run", "--rm", "--name", "holdco-7-5",
                               "-v", "C:/Users/Me/Holdco/data/companies/acme:/work", "-w", "/work",
                               "--memory", "1g", "--cpus", "1", "--pids-limit", "256",
                               "--read-only", "--tmpfs", "/tmp",
                               "python:3.12-slim", "sh", "-c", "ls -la | wc -l"])
        self.assertEqual(sandbox.host_path("/srv/acme", windows=True), "/srv/acme")
        self.assertEqual(sandbox.host_path(r"D:\data\acme", windows=False), r"D:\data\acme")

    def test_available_is_cached_and_checks_the_engine(self):
        self._patches[-1].stop()   # the real available(), with fake binaries
        self._patches.pop()
        fake = FakeDocker()
        with fake_docker(fake):
            self.assertEqual(sandbox.available(), "docker")
            self.assertEqual(sandbox.available(), "docker")
            self.assertEqual([cmd[1:] for cmd in fake.calls], [["info", "--format", "{{.OSType}}"]])   # cached
            with mock.patch.dict(os.environ, {"HOLDCO_SANDBOX": "off"}):
                self.assertIsNone(sandbox.available())
            fake.ostype = "windows"   # Docker Desktop on Windows containers cannot run python:3.12-slim
            self.assertIsNone(sandbox.available(refresh=True))
        podman = FakeDocker()
        with fake_docker(podman), mock.patch.object(sandbox, "WHICH", lambda n: "/bin/podman" if n == "podman" else None):
            self.assertEqual(sandbox.available(), "podman")
            self.assertEqual(podman.calls[0], ["/bin/podman", "info", "--format", "{{.Host.OS}}"])
        broken = FakeDocker()
        with fake_docker(broken), mock.patch.object(sandbox, "RUNNER", side_effect=OSError("no daemon")):
            self.assertIsNone(sandbox.available())
        with fake_docker(FakeDocker()), mock.patch.object(sandbox, "WHICH", lambda n: None):
            self.assertIsNone(sandbox.available())

    def test_run_and_run_code(self):
        self._patches[-1].stop()
        self._patches.pop()
        c = make_company("BoxCo", members=("ceo",))
        fake = FakeDocker()
        with fake_docker(fake):
            out = sandbox.run(c, "python", "print(6 * 7)", 30)
            self.assertTrue(out.startswith("Sandbox: docker\nDownloaded the python:3.12-slim image first (a one-time "
                                           "step that can take a minute).\nExit code 0\n--- stdout ---\n42"), out)
            run = fake.runs()[0]
            ms = run[run.index("--name") + 1].rsplit("-", 1)[1]
            self.assertEqual(run[-2:], ["python", f"/work/.runs/run_{ms}.py"])
            self.assertEqual((workspace.root(c) / ".runs" / f"run_{ms}.py").read_text(encoding="utf-8"),
                             "print(6 * 7)")
            self.assertEqual(run[run.index("-v") + 1], f"{sandbox.host_path(os.path.abspath(workspace.root(c)))}:/work")
            again = sandbox.run(c, "shell", "echo hi", 30, network=True)
            self.assertTrue(again.startswith("Sandbox: docker (network allowed)\nExit code 0"), again)
            self.assertNotIn("--network", fake.runs()[1])
            self.assertTrue(actions.run_code(c, "python", "print(1)").startswith("Sandbox: docker\nExit code 0"))
        # A run past its timeout: the container is killed by its name.
        slow = FakeDocker(have_image=True, run_timeout=True)
        with fake_docker(slow):
            out = sandbox.run(c, "python", "import time; time.sleep(99)", 5)
        name = slow.runs()[0][slow.runs()[0].index("--name") + 1]
        self.assertEqual(out, "Sandbox: docker\nTimed out after 5s; the container was stopped.")
        self.assertIn(["docker", "kill", name], slow.calls)
        # A failed first download: nothing runs unsandboxed.
        nopull = FakeDocker()
        with fake_docker(nopull):
            def failing(cmd, **kw):
                if cmd[1] == "pull":
                    return subprocess.CompletedProcess(cmd, 1, b"", b"Get https://registry: no route to host")
                return nopull(cmd, **kw)
            with mock.patch.object(sandbox, "RUNNER", failing):
                out = sandbox.run(c, "python", "print(1)", 5)
        self.assertTrue(out.startswith("Sandbox: docker\nDownloading python:3.12-slim failed: Get https://registry: "
                                       "no route to host Nothing ran."), out)
        self.assertEqual(nopull.runs(), [])
        # No runtime: today's subprocess path, labelled.
        with mock.patch.object(sandbox, "available", lambda refresh=False: None):
            out = actions.run_code(c, "python", "print(2 + 2)")
        self.assertTrue(out.startswith("Sandbox: none (ran on this PC)\nExit code 0\n--- stdout ---\n4"), out)
        # The approval payload's network flag reaches the sandbox.
        seen = []
        with mock.patch.object(sandbox, "run", lambda company, lang, code, timeout, network=False:
                               seen.append(network) or "Sandbox: docker\nExit code 0\n"):
            actions.execute(c, "run_code", {"language": "python", "code": "print(1)", "network": True})
            actions.execute(c, "run_code", {"language": "python", "code": "print(1)", "network": "true"})
            actions.execute(c, "run_code", {"language": "python", "code": "print(1)"})
        self.assertEqual(seen, [True, True, False])

    def test_doctor_row(self):
        row = doctor._check_sandbox()
        self.assertEqual((row["key"], row["label"], row["status"]), ("sandbox", "Code sandbox", "warn"))
        self.assertEqual(row["detail"], "Code sandbox: not available. Install Docker Desktop or Podman to isolate "
                                        "agents' code.")
        self.assertEqual(row["fix"], {"kind": "link", "label": "Get Docker Desktop", "href": doctor.SANDBOX_DOCS})
        with mock.patch.object(sandbox, "available", lambda refresh=False: "docker"):
            row = doctor._check_sandbox()
        self.assertEqual((row["status"], row["fix"]), ("ok", None))
        self.assertTrue(row["detail"].startswith("Code sandbox: docker."))


if __name__ == "__main__":
    unittest.main()
