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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-phase3-bench-")

from holdco import bench, browser, config, connectors, db, llm, notify, tools, vault, web  # noqa: E402
from holdco.bench import mocks, runner, scenarios  # noqa: E402
from holdco.routing import ROUTES, HTTPError  # noqa: E402
from tests.fake_ollama import FakeModelServer  # noqa: E402

db.init()

MODEL = "gpt-oss:20b"

# Scenarios that cannot pass until another builder's part is merged; both CI tests leave them out for now. The
# integration step empties this set.
PENDING = {
    "taint_blocks_publish",   # B4: taint.after_tool marks the session tainted after fetch_url, and trust.gate_policy
                              # then queues the auto social_post with _taint instead of running it
    "tool_call_repair",       # B2: tools._clean_args answers priority "high" with a repair message; without it the
                              # lazy model's second "high" passes silently and the scenario cannot fail
}


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
        failed = _failed(row)
        self.assertEqual({k: v for k, v in failed.items() if k not in PENDING}, {})
        self.assertEqual(row["passed"], len(scenarios.SCENARIOS) - len(set(failed) & PENDING))
        # The bench_runs row with all four scores, newest first in the API's list.
        stored = runner.get_run(row["id"])
        for key in ("pass_rate", "tool_validity", "tokens_per_second", "steps_avg"):
            self.assertIsNotNone(stored[key], key)
        self.assertGreater(stored["tool_validity"], 0.9)
        self.assertTrue(stored["finished_at"])
        self.assertEqual(len(stored["results"]), 33)
        self.assertEqual(runner.list_runs(1)[0]["id"], row["id"])
        self.assertIn("approximate", stored["notes"])   # the fake model reports no eval durations
        self.assert_clean()

    def test_lazy_model_fails_the_right_ones(self):
        row = self.run_bench(self.lazy)
        failed = set(_failed(row)) - PENDING
        expected = {s.name for s in scenarios.SCENARIOS if s.lazy_fails} - PENDING
        self.assertEqual(failed, expected, f"failed only in lazy mode: {sorted(failed - expected)}; passed although "
                                           f"lazy: {sorted(expected - failed)}")
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
