"""Phase 0: Doctor (health checks, founding preflight), backups, and the SMTP/IMAP login helpers.

No network: the model server is tests/fake_ollama.FakeModelServer, connector endpoints are tiny local stubs
or closed ports, and web.search_health is patched where its outcome matters.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-doctor-"))

from holdco import agents, backup, bus, config, db, doctor, emailer, roles, server, web, workspace  # noqa: E402
from tests.fake_ollama import FakeModelServer  # noqa: E402

db.init()


def make_company(name="DocCo", members=("ceo", "engineering"), status="active", settings=None) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "An idea", status, "building", json.dumps(settings or {}), db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StubHTTP:
    """A local HTTP server that answers every request with one JSON body (Telegram getMe, Netlify user...)."""

    def __init__(self, payload: dict, code: int = 200):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                b = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.paths: list[str] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Stub:
    """Stands in for the request handler that route functions receive."""
    query: dict = {}


class SettingsGuard:
    """Snapshot the stored settings rows and put them back, so other test modules see what they expect."""

    def __enter__(self):
        self.rows = db.q("SELECT key, value FROM settings")
        return self

    def __exit__(self, *a):
        db.run("DELETE FROM settings")
        for r in self.rows:
            db.run("INSERT INTO settings(key, value) VALUES(?, ?)", (r["key"], r["value"]))


class DoctorBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = FakeModelServer(delay=0).start()

    @classmethod
    def tearDownClass(cls):
        cls.model.stop()

    def setUp(self):
        doctor.LAST[:] = []
        doctor._ok_since.clear()
        doctor._last_login.clear()
        self._note = patch.object(doctor, "_gpu_memory_note", return_value="")   # nvidia-smi never decides a test
        self._note.start()
        # A netlify token left in the shared test database by another module must never reach the internet.
        self._netlify = patch.object(doctor, "NETLIFY_API", f"http://127.0.0.1:{closed_port()}")
        self._netlify.start()

    def tearDown(self):
        self._note.stop()
        self._netlify.stop()
        doctor.LAST[:] = []

    def settings(self, **over) -> dict:
        s = db.get_settings()
        s.update({"llm_backend": "ollama", "llm_base_url": self.model.url, "default_model": "gpt-oss:20b"})
        s.update(over)
        return s

    @staticmethod
    def by_key(rows):
        return {r["key"]: r for r in rows}


class TestDoctorModel(DoctorBase):
    def test_reachable_and_installed(self):
        rows = self.by_key(doctor._check_model(self.settings(), False))
        self.assertEqual(rows["model_server"]["status"], "ok")
        self.assertIn(self.model.url, rows["model_server"]["detail"])
        self.assertEqual(rows["model"]["status"], "ok")
        self.assertIsNone(rows["model"]["fix"])
        gpu = doctor._check_gpu(self.settings())
        self.assertEqual(gpu["status"], "ok")
        self.assertIn("Fully on the GPU", gpu["detail"])

    def test_missing_model(self):
        rows = self.by_key(doctor._check_model(self.settings(default_model="nope:7b"), False))
        self.assertEqual(rows["model_server"]["status"], "ok")
        self.assertEqual(rows["model"]["status"], "bad")
        self.assertIn("nope:7b is not installed", rows["model"]["detail"])
        self.assertEqual(rows["model"]["fix"], {"kind": "text", "label": "ollama pull nope:7b"})
        # a setting without a tag matches the ":latest" entry Ollama lists
        self.assertTrue(doctor._installed("gpt-oss", ["gpt-oss:latest"]))
        self.assertFalse(doctor._installed("gpt-oss", ["gpt-oss:20b"]))

    def test_unreachable(self):
        s = self.settings(llm_base_url=f"http://127.0.0.1:{closed_port()}")
        rows = self.by_key(doctor._check_model(s, False))
        self.assertEqual(rows["model_server"]["status"], "bad")
        self.assertEqual(rows["model_server"]["fix"], {"kind": "link", "label": "Model settings", "href": "#/settings"})
        self.assertEqual(rows["model"]["status"], "warn")       # cannot know, so not red twice
        gpu = doctor._check_gpu(s)
        self.assertEqual(gpu["status"], "ok")                    # unknown = ok with a note
        self.assertIn("Could not check", gpu["detail"])

    def test_spilling_into_ram(self):
        saved = self.model.ps
        self.model.ps = [{"name": "gpt-oss:20b", "size": 14_000_000_000, "size_vram": 9_000_000_000}]
        try:
            gpu = doctor._check_gpu(self.settings())
        finally:
            self.model.ps = saved
        self.assertEqual(gpu["status"], "warn")
        self.assertIn("spilling into RAM: 64% on the GPU", gpu["detail"])
        self.assertEqual(gpu["fix"]["kind"], "text")
        self.assertIn("RTX 5090", gpu["fix"]["label"])

    def test_nothing_loaded_and_other_backend(self):
        saved = self.model.ps
        self.model.ps = []
        try:
            gpu = doctor._check_gpu(self.settings())
        finally:
            self.model.ps = saved
        self.assertEqual(gpu["status"], "ok")
        self.assertIn("No model is loaded", gpu["detail"])
        gpu = doctor._check_gpu(self.settings(llm_backend="openai"))
        self.assertEqual((gpu["status"], gpu["fix"]), ("ok", None))
        self.assertIn("Not applicable", gpu["detail"])

    def test_nvidia_smi_missing_never_fails(self):
        self._note.stop()
        try:
            with patch("shutil.which", return_value=None):
                self.assertEqual(doctor._gpu_memory_note(), "")
            with patch("shutil.which", return_value="/no/such/nvidia-smi"):
                self.assertEqual(doctor._gpu_memory_note(), "")   # binary vanished: still empty, never raises
        finally:
            self._note.start()

    def test_nvidia_smi_runs_hidden_with_a_timeout(self):
        self._note.stop()
        try:
            calls = []

            def fake_run(cmd, **kw):
                calls.append((cmd, kw))
                return types.SimpleNamespace(stdout="2048, 32768\n", stderr="", returncode=0)

            with patch("shutil.which", return_value="C:/nvidia-smi.exe"), patch("subprocess.run", fake_run):
                self.assertEqual(doctor._gpu_memory_note(), "; GPU memory 2.0 of 32.0 GB in use (nvidia-smi)")
            cmd, kw = calls[0]
            self.assertEqual(cmd[0], "C:/nvidia-smi.exe")
            self.assertEqual(kw["timeout"], 5)
            # On Windows the hidden autostart must not flash a console; elsewhere the flag is 0 and accepted.
            self.assertEqual(kw["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with patch("shutil.which", return_value="C:/nvidia-smi.exe"), \
                    patch("subprocess.run", side_effect=subprocess.TimeoutExpired("nvidia-smi", 5)):
                self.assertEqual(doctor._gpu_memory_note(), "")   # a hung tool never fails the check
        finally:
            self._note.start()

    def test_full_run_rows_cache_and_routes(self):
        with SettingsGuard():
            db.set_settings({"llm_base_url": self.model.url, "default_model": "gpt-oss:20b"})
            with patch.object(doctor, "NETLIFY_API", self.model.url):
                rows = doctor.run()
        keys = [r["key"] for r in rows]
        for k in ("model_server", "model", "gpu_fit", "search", "disk", "backup", "errors_24h"):
            self.assertIn(k, keys)
        self.assertEqual(keys[:4], ["model_server", "model", "gpu_fit", "search"])
        self.assertEqual(keys[-3:], ["disk", "backup", "errors_24h"])
        for r in rows:
            # Phase 0 fix: every row carries its run number; connector rows also carry their company id.
            self.assertEqual(set(r) - {"company_id"}, {"key", "label", "status", "detail", "fix", "checked_at", "run"})
            self.assertEqual(r["run"], rows[0]["run"])
            self.assertEqual("company_id" in r, r["key"].startswith("connector:"), r["key"])
            self.assertIn(r["status"], ("ok", "warn", "bad"))
            self.assertIsNotNone(db.parse_time(r["checked_at"]))
            if r["fix"] is not None:
                self.assertIn(r["fix"]["kind"], ("link", "action", "text"))
                self.assertIn("label", r["fix"])
        self.assertEqual(doctor.LAST, rows)
        summ = doctor.summary()
        self.assertEqual(summ["ok"] + summ["warn"] + summ["bad"], len(rows))
        self.assertEqual(summ["checked_at"], rows[0]["checked_at"])
        self.assertEqual(summ["run"], rows[0]["run"])
        self.assertEqual(doctor.row_status("model_server"), "ok")
        self.assertIsNone(doctor.row_status("nope"))
        self.assertEqual(doctor.search_status()["key"], "search")
        out = doctor.api_doctor(Stub(), None, None)
        self.assertEqual((out["checks"], out["summary"]), (rows, summ))
        with SettingsGuard():
            db.set_settings({"llm_base_url": self.model.url})
            with patch.object(doctor, "NETLIFY_API", self.model.url):
                out = doctor.api_doctor_run(Stub(), None, {"deep": False})
        self.assertEqual(out["summary"]["ok"], doctor.summary(out["checks"])["ok"])

    def test_deep_run_pings_tools_and_live_search(self):
        with patch.object(web, "live_search_test", create=True, return_value=(True, "duckduckgo answered in 0.3 s")):
            rows = self.by_key(doctor._check_model(self.settings(), True))
            self.assertEqual(rows["model"]["status"], "ok")
            self.assertIn("can call tools", rows["model"]["detail"])
            srow = doctor._check_search(self.settings(), True)
        self.assertEqual((srow["status"], srow["detail"]), ("ok", "duckduckgo answered in 0.3 s"))
        with patch.object(web, "live_search_test", create=True, return_value=(False, "All search providers failed")):
            srow = doctor._check_search(self.settings(), True)
        self.assertEqual(srow["status"], "bad")
        self.assertEqual(srow["fix"]["href"], "#/settings")
        with patch("holdco.llm.tool_ping", return_value=(True, "x replied, but didn't call the test tool.")):
            self.assertEqual(self.by_key(doctor._check_model(self.settings(), True))["model"]["status"], "warn")
        with patch("holdco.llm.tool_ping", return_value=(False, "HTTP 500 from model server")):
            self.assertEqual(self.by_key(doctor._check_model(self.settings(), True))["model"]["status"], "bad")

    def test_api_llm_test_uses_tool_ping(self):
        with SettingsGuard():
            db.set_settings({"llm_base_url": self.model.url})
            r = server.api_llm_test(Stub(), None, {})
            self.assertTrue(r["ok"])
            self.assertIn("can call tools", r["message"])
            self.assertIn("json mode", server.api_llm_test(Stub(), None, {"model": "notools:latest"})["message"])
            db.set_settings({"llm_base_url": f"http://127.0.0.1:{closed_port()}"})
            self.assertFalse(server.api_llm_test(Stub(), None, {})["ok"])


class TestDoctorRules(DoctorBase):
    GB = 1024 ** 3

    def test_disk_thresholds(self):
        with patch.object(doctor, "_disk_free_bytes", return_value=(50 * self.GB, 500 * self.GB)):
            r = doctor._check_disk()
            self.assertEqual((r["status"], r["fix"]), ("ok", None))
            self.assertIn("50.0 GB free of 500 GB", r["detail"])
        with patch.object(doctor, "_disk_free_bytes", return_value=(3 * self.GB, 500 * self.GB)):
            r = doctor._check_disk()
            self.assertEqual((r["status"], r["fix"]["kind"]), ("warn", "text"))
        with patch.object(doctor, "_disk_free_bytes", return_value=(int(0.5 * self.GB), 500 * self.GB)):
            self.assertEqual(doctor._check_disk()["status"], "bad")

    def test_backup_rules(self):
        with patch.object(backup, "BACKUP_DIR", Path(tempfile.mkdtemp(prefix="holdco-bk-"))):
            r = doctor._check_backup()
            self.assertEqual(r["status"], "bad")
            self.assertEqual(r["fix"], {"kind": "action", "label": "Back up now", "action": "backup"})
            backup.run_backup()
            r = doctor._check_backup()
            self.assertEqual(r["status"], "ok")
            self.assertIn("Last backup 0 min ago", r["detail"])
        def old(hours):
            when = db.parse_time(db.now()) - datetime.timedelta(hours=hours)
            return {"path": "/x/holdco-x.zip", "name": "holdco-x.zip", "size": 1000,
                    "created_at": when.strftime("%Y-%m-%dT%H:%M:%SZ")}
        with patch.object(backup, "last_backup", return_value=old(30)):
            self.assertEqual(doctor._check_backup()["status"], "warn")
        with patch.object(backup, "last_backup", return_value=old(25)):
            self.assertEqual(doctor._check_backup()["status"], "ok")
        with patch.object(backup, "last_backup", return_value=old(80)):
            self.assertEqual(doctor._check_backup()["status"], "bad")

    def test_errors_24h(self):
        db.run("DELETE FROM events WHERE kind='error'")
        r = doctor._check_errors()
        self.assertEqual((r["status"], r["fix"]), ("ok", None))
        bus.emit(None, "error", "Search failed: blocked")
        r = doctor._check_errors()
        self.assertEqual(r["status"], "warn")
        self.assertIn("1 error in the last 24 h: 1x Search failed: blocked", r["detail"])
        self.assertEqual(r["fix"], {"kind": "link", "label": "Open the live feed", "href": "#/"})
        for i in range(9):
            bus.emit(None, "error", "Model server dropped the connection" if i < 6 else f"Other {i}")
        r = doctor._check_errors()
        self.assertEqual(r["status"], "bad")
        self.assertTrue(r["detail"].startswith("10 errors in the last 24 h: 6x Model server dropped the connection"))
        # an old error does not count
        db.run("DELETE FROM events WHERE kind='error'")
        db.run("INSERT INTO events(company_id, kind, message, created_at) VALUES(NULL, 'error', 'old', ?)",
               ("2000-01-01T00:00:00Z",))
        self.assertEqual(doctor._check_errors()["status"], "ok")
        db.run("DELETE FROM events WHERE kind='error'")

    def test_search_rules_follow_provider_health(self):
        def health(rows):
            return patch.object(web, "search_health", create=True, return_value=rows)

        def prov(name, status, configured=True, detail=""):
            return {"provider": name, "configured": configured, "status": status, "detail": detail}

        s = self.settings()
        with health([prov("duckduckgo", "ok", detail="ok 2 min ago"), prov("brave", "unknown", False, "no key")]):
            r = doctor._check_search(s, False)
            self.assertEqual((r["status"], r["fix"]), ("ok", None))
            self.assertIn("duckduckgo: ok 2 min ago", r["detail"])
        with health([prov("duckduckgo", "warn", detail="cooling down"), prov("ddgs", "unknown")]):
            self.assertEqual(doctor._check_search(s, False)["status"], "warn")
        with health([prov("duckduckgo", "bad", detail="blocked (HTTP 202 challenge)"), prov("brave", "bad", False)]):
            r = doctor._check_search(s, False)
            self.assertEqual(r["status"], "bad")
            self.assertIn("Every search provider failed", r["detail"])
            self.assertEqual(r["fix"]["href"], "#/settings")
        with health([prov("duckduckgo", "unknown"), prov("searxng", "unknown", False)]):
            self.assertEqual(doctor._check_search(s, False)["status"], "ok")
        with patch.object(web, "search_health", create=True, side_effect=RuntimeError("boom")):
            self.assertEqual(doctor._check_search(s, False)["status"], "bad")

    def test_slow_and_crashing_checks_become_rows(self):
        def slow():
            time.sleep(2)
            return doctor._row("x", "X", "ok", "late")

        def crash():
            raise RuntimeError("kaboom")

        rows = doctor._run_jobs([doctor._Job([("x", "X", None)], slow, timeout=0.2),
                                 doctor._Job([("a", "A", None), ("b", "B", {"kind": "text", "label": "t"})], crash)])
        self.assertEqual([r["key"] for r in rows], ["x", "a", "b"])
        self.assertEqual(rows[0]["status"], "bad")
        self.assertIn("Timed out", rows[0]["detail"])
        self.assertEqual(rows[1]["status"], "bad")
        self.assertIn("kaboom", rows[2]["detail"])
        self.assertEqual(rows[2]["fix"], {"kind": "text", "label": "t"})

    def test_run_never_raises(self):
        with patch.object(db, "get_settings", side_effect=RuntimeError("db gone")):
            rows = doctor.run()
        self.assertEqual(rows[0]["key"], "doctor")
        self.assertEqual(rows[0]["status"], "bad")
        self.assertEqual(doctor.LAST, rows)


class TestConnectors(DoctorBase):
    def test_rows_only_for_configured_connectors(self):
        port = closed_port()
        a = make_company("MailCo", settings={"smtp_host": "127.0.0.1", "smtp_port": port, "smtp_user": "u@x.test",
                                              "smtp_password": "hunter2secret", "webhook_url": "not a url"})
        b = make_company("PlainCo")
        z = make_company("GoneCo", status="archived", settings={"webhook_url": "https://hooks.example.com/x"})
        s = self.settings()
        rows = self.by_key(doctor._run_jobs(doctor._connector_jobs(s, False)))
        self.assertIn(f"connector:smtp:{a['id']}", rows)
        self.assertNotIn(f"connector:imap:{a['id']}", rows)
        self.assertFalse([k for k in rows if k.endswith(f":{b['id']}")])
        self.assertFalse([k for k in rows if k.endswith(f":{z['id']}")])
        smtp = rows[f"connector:smtp:{a['id']}"]
        self.assertEqual(smtp["status"], "bad")
        self.assertEqual(smtp["label"], "Email sending (MailCo)")
        self.assertEqual(smtp["company_id"], a["id"])   # Phase 0 fix: nobody has to parse the key for this
        self.assertEqual(smtp["fix"], {"kind": "link", "label": "Company settings", "href": f"#/c/{a['id']}/settings"})
        self.assertNotIn("hunter2secret", json.dumps(list(rows.values())))
        hook = rows[f"connector:webhook:{a['id']}"]
        self.assertEqual(hook["company_id"], a["id"])
        self.assertEqual(hook["status"], "bad")
        self.assertIn("malformed", hook["detail"])
        db.update("companies", a["id"], settings=json.dumps({"webhook_url": "https://hooks.zapier.com/abc"}))
        rows = self.by_key(doctor._run_jobs(doctor._connector_jobs(s, False)))
        self.assertEqual(rows[f"connector:webhook:{a['id']}"]["status"], "ok")
        self.assertIn("hooks.zapier.com", rows[f"connector:webhook:{a['id']}"]["detail"])
        self.assertNotIn(f"connector:smtp:{a['id']}", rows)

    def test_imap_and_netlify_rows(self):
        port = closed_port()
        c = make_company("ImapCo", settings={"imap_host": "127.0.0.1", "imap_port": port, "imap_user": "u",
                                              "imap_password": "pw-secret-9", "netlify_token": "nfp_SECRET_TOKEN"})
        stub = StubHTTP({"email": "owner@example.com"})
        try:
            with patch.object(doctor, "NETLIFY_API", stub.url):
                rows = self.by_key(doctor._run_jobs(doctor._connector_jobs(self.settings(), False)))
        finally:
            stub.stop()
        self.assertEqual(rows[f"connector:imap:{c['id']}"]["status"], "bad")
        self.assertEqual(rows[f"connector:imap:{c['id']}"]["label"], "Email receiving (ImapCo)")
        net = rows[f"connector:netlify:{c['id']}"]
        self.assertEqual(net["status"], "ok")
        self.assertIn("owner@example.com", net["detail"])
        self.assertEqual(stub.paths, ["/api/v1/user"])
        dumped = json.dumps(list(rows.values()))
        self.assertNotIn("pw-secret-9", dumped)
        self.assertNotIn("SECRET_TOKEN", dumped)
        bad = StubHTTP({"message": "nope"}, code=401)
        try:
            with patch.object(doctor, "NETLIFY_API", bad.url):
                r = doctor._netlify_row("k", "L", {"kind": "text", "label": "x"}, "nfp_SECRET_TOKEN")
        finally:
            bad.stop()
        self.assertEqual(r["status"], "bad")
        self.assertIn("rejected the token (HTTP 401)", r["detail"])
        r = doctor._netlify_row("k", "L", None, "nfp_SECRET_TOKEN")   # setUp points NETLIFY_API at a closed port
        self.assertEqual(r["status"], "bad")
        self.assertNotIn("SECRET_TOKEN", r["detail"])

    def test_telegram_and_ntfy_rows(self):
        good = StubHTTP({"ok": True, "result": {"username": "holdco_bot"}})
        try:
            s = self.settings(telegram_bot_token="123:SECRET-TOKEN", telegram_chat_id="", telegram_api_base=good.url)
            r = doctor._telegram_row(s)
            self.assertEqual(r["status"], "warn")
            self.assertIn("@holdco_bot", r["detail"])
            self.assertIn("no chat id", r["detail"])
            s["telegram_chat_id"] = "42"
            r = doctor._telegram_row(s)
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["key"], "connector:telegram")
            self.assertNotIn("SECRET-TOKEN", r["detail"])
        finally:
            good.stop()
        s = self.settings(telegram_bot_token="123:SECRET-TOKEN", telegram_chat_id="42",
                          telegram_api_base=self.model.url)
        r = doctor._telegram_row(s)            # the fake model server answers 404 to unknown paths
        self.assertEqual(r["status"], "bad")
        self.assertIn("HTTP 404", r["detail"])
        self.assertNotIn("SECRET-TOKEN", r["detail"])
        s = self.settings(telegram_bot_token="", ntfy_topic="holdco-alerts", ntfy_server="https://ntfy.sh")
        rows = self.by_key(doctor._run_jobs([j for j in doctor._connector_jobs(s, False)
                                             if j.metas[0][0].startswith("connector:")]))
        self.assertNotIn("connector:telegram", rows)
        self.assertEqual(rows["connector:ntfy"]["status"], "ok")
        self.assertIsNone(rows["connector:ntfy"]["company_id"])   # global connector: the field is there, as None
        self.assertEqual(doctor._ntfy_row(self.settings(ntfy_topic="t", ntfy_server="ntfy.sh"))["status"], "bad")
        # Scrubbing (Phase 0 fix): a malformed api base makes urllib quote the whole URL, token included, and a
        # token pasted with a newline is refused by http.client with the URL in the message. Neither reaches a row.
        s = self.settings(telegram_bot_token="123:SECRET-TOKEN", telegram_chat_id="42",
                          telegram_api_base="api.telegram.org")
        r = doctor._telegram_row(s)
        self.assertEqual(r["status"], "bad")
        self.assertNotIn("SECRET-TOKEN", json.dumps(r))
        self.assertIn("[token]", r["detail"])
        good = StubHTTP({"ok": True, "result": {"username": "holdco_bot"}})
        try:
            s = self.settings(telegram_bot_token="123:SECRET-TOKEN\n", telegram_chat_id="42",
                              telegram_api_base=good.url)
            r = doctor._telegram_row(s)
        finally:
            good.stop()
        self.assertEqual(r["status"], "bad")
        self.assertNotIn("SECRET-TOKEN", json.dumps(r))
        self.assertIn("[token]", r["detail"])

    def test_crash_and_timeout_rows_carry_company_id_and_no_token(self):
        secret = "123:SECRET-TOKEN"

        def crash():
            raise RuntimeError(f"boom at https://api.telegram.org/bot{secret}/getMe")

        def slow():
            time.sleep(2)
            return doctor._row("connector:imap:7", "Email receiving (X)", "ok", "late")

        with SettingsGuard():
            db.set_settings({"telegram_bot_token": secret})
            rows = self.by_key(doctor._run_jobs([
                doctor._Job([("connector:smtp:7", "Email sending (X)", None)], crash, company_id=7),
                doctor._Job([("connector:imap:7", "Email receiving (X)", None)], slow, timeout=0.2, company_id=7),
                doctor._Job([("connector:telegram", "Telegram bot", None)], crash),
                doctor._Job([("disk", "Disk space", None)], crash)]))
        self.assertEqual(rows["connector:smtp:7"]["company_id"], 7)
        self.assertEqual(rows["connector:imap:7"]["company_id"], 7)
        self.assertIn("Timed out", rows["connector:imap:7"]["detail"])
        self.assertIsNone(rows["connector:telegram"]["company_id"])
        self.assertNotIn("company_id", rows["disk"])
        self.assertNotIn("SECRET-TOKEN", json.dumps(list(rows.values())))
        self.assertEqual(rows["connector:smtp:7"]["detail"], "Check failed: boom at [token]/getMe")
        with SettingsGuard():   # no token in the settings at all: the URL shape alone still hides it
            db.set_settings({"telegram_bot_token": ""})
            rows = self.by_key(doctor._run_jobs([doctor._Job([("disk", "Disk space", None)], crash)]))
        self.assertEqual(rows["disk"]["detail"], "Check failed: boom at [token]/getMe")

    def test_stable_login_is_rechecked_every_6_hours(self):
        key, label, fix = "connector:smtp:999", "Email sending (X)", {"kind": "link", "label": "x", "href": "#"}
        calls = []

        def login():
            calls.append(1)
            raise emailer.EmailError("SMTP login failed: connection refused")

        now = time.time()
        ok_row = doctor._row(key, label, "ok", "SMTP login ok as u at h:587.")
        # Fine for a day, tested an hour ago: the cached row is reused and the server is not touched.
        doctor._ok_since[key] = now - 25 * 3600
        doctor._last_login[key] = (now - 3600, ok_row)
        r = doctor._login_row(key, label, fix, login, False)
        self.assertEqual((r["status"], calls), ("ok", []))
        self.assertIn("retested every 6 h", r["detail"])
        # a forced (or deep) run always tests for real
        r = doctor._login_row(key, label, fix, login, True)
        self.assertEqual((r["status"], len(calls)), ("bad", 1))
        self.assertNotIn(key, doctor._ok_since)                      # the streak is broken
        # Fine for a day but last tested 7 hours ago: tested again.
        doctor._ok_since[key] = now - 25 * 3600
        doctor._last_login[key] = (now - 7 * 3600, ok_row)
        r = doctor._login_row(key, label, fix, login, False)
        self.assertEqual((r["status"], len(calls)), ("bad", 2))
        # Fine for only two hours: tested every run.
        doctor._ok_since[key] = now - 2 * 3600
        doctor._last_login[key] = (now - 60, ok_row)
        doctor._login_row(key, label, fix, login, False)
        self.assertEqual(len(calls), 3)
        # A passing login starts the streak once and keeps its start on later passes.
        doctor._ok_since.pop(key, None)
        doctor._login_row(key, label, fix, lambda: "ok", False)
        first = doctor._ok_since[key]
        doctor._login_row(key, label, fix, lambda: "ok", False)
        self.assertEqual(doctor._ok_since[key], first)

    def test_run_force_rechecks_a_stable_login(self):
        c = make_company("StableCo", settings={"smtp_host": "127.0.0.1", "smtp_port": closed_port(),
                                                "smtp_user": "u", "smtp_password": "pw"})
        key = f"connector:smtp:{c['id']}"
        now = time.time()
        doctor._ok_since[key] = now - 25 * 3600
        doctor._last_login[key] = (now - 3600, doctor._row(key, "Email sending (StableCo)", "ok", "fine"))
        with SettingsGuard(), patch.object(emailer, "test_smtp", side_effect=emailer.EmailError("refused")) as t:
            db.set_settings({"llm_base_url": self.model.url})
            rows = self.by_key(doctor.run())                 # the hourly refresh: reused, no login
            self.assertEqual(rows[key]["status"], "ok")
            self.assertEqual(t.call_count, 0)
            rows = self.by_key(doctor.api_doctor_run(Stub(), None, {})["checks"])   # "Run checks": for real
            self.assertEqual(rows[key]["status"], "bad")
            self.assertEqual(t.call_count, 1)
        db.update("companies", c["id"], status="archived")

    def test_emailer_login_helpers(self):
        port = closed_port()
        cs = {"smtp_host": "127.0.0.1", "smtp_port": port, "smtp_user": "u", "smtp_password": "secret-pw",
              "imap_host": "127.0.0.1", "imap_port": port, "imap_user": "u", "imap_password": "secret-pw"}
        with self.assertRaises(emailer.EmailError) as cm:
            emailer.test_smtp(cs)
        self.assertIn("SMTP login as u at 127.0.0.1", str(cm.exception))
        self.assertNotIn("secret-pw", str(cm.exception))
        with self.assertRaises(emailer.EmailError) as cm:
            emailer.test_imap(cs)
        self.assertIn("IMAP login as u", str(cm.exception))
        self.assertNotIn("secret-pw", str(cm.exception))
        with self.assertRaises(emailer.EmailError):
            emailer.test_smtp({})
        with self.assertRaises(emailer.EmailError):
            emailer.test_imap({})
        self.assertIs(emailer.check_smtp, emailer.test_smtp)
        # port 465 takes the SSL path; a closed port still fails cleanly
        with self.assertRaises(emailer.EmailError):
            emailer.test_smtp({**cs, "smtp_port": 465})


class TestPreflight(DoctorBase):
    def test_blocks_on_bad_model_url(self):
        with SettingsGuard():
            db.set_settings({"llm_base_url": f"http://127.0.0.1:{closed_port()}"})
            pf = doctor.preflight()
        self.assertFalse(pf["ok"])
        self.assertEqual([r["key"] for r in pf["checks"]], ["model_server", "model"])
        self.assertEqual(pf["checks"][0]["status"], "bad")
        self.assertEqual([r["key"] for r in pf["soft"]], ["search"])
        self.assertEqual(doctor.LAST, [])    # an empty cache stays empty so the full set still runs soon

    def test_soft_search_never_blocks(self):
        bad = [{"provider": "duckduckgo", "configured": True, "status": "bad", "detail": "blocked"}]
        with SettingsGuard(), patch.object(web, "search_health", create=True, return_value=bad):
            db.set_settings({"llm_base_url": self.model.url, "default_model": "gpt-oss:20b"})
            pf = doctor.preflight()
        self.assertTrue(pf["ok"])
        self.assertEqual(pf["soft"][0]["status"], "bad")
        self.assertEqual([r["status"] for r in pf["checks"]], ["ok", "ok"])

    def test_cached_search_row_is_reused_within_an_hour(self):
        doctor.LAST[:] = [doctor._row("model_server", "Model server", "ok", "x"),
                          doctor._row("search", "Web search", "warn", "cooling")]
        with SettingsGuard(), patch.object(web, "search_health", create=True, side_effect=AssertionError("no")):
            db.set_settings({"llm_base_url": self.model.url, "default_model": "gpt-oss:20b"})
            pf = doctor.preflight()
        self.assertEqual(pf["soft"][0]["detail"], "cooling")
        # the fresh hard rows were merged into the cache, the search row kept
        self.assertEqual([r["key"] for r in doctor.LAST], ["model_server", "search", "model"])
        self.assertIn(self.model.url, doctor.LAST[0]["detail"])
        stale = doctor._row("search", "Web search", "bad", "old")
        stale["checked_at"] = "2000-01-01T00:00:00Z"
        doctor.LAST[:] = [stale]
        rows = [{"provider": "duckduckgo", "configured": True, "status": "ok", "detail": "fine"}]
        with SettingsGuard(), patch.object(web, "search_health", create=True, return_value=rows):
            db.set_settings({"llm_base_url": self.model.url, "default_model": "gpt-oss:20b"})
            pf = doctor.preflight()
        self.assertEqual(pf["soft"][0]["status"], "ok")

    def test_merge_keeps_other_rows_and_run_numbers_tell_runs_apart(self):
        with SettingsGuard(), patch.object(doctor, "NETLIFY_API", self.model.url):
            db.set_settings({"llm_base_url": self.model.url, "default_model": "gpt-oss:20b"})
            first = doctor.run()
            second = doctor.run()
            self.assertEqual({r["run"] for r in first}, {first[0]["run"]})   # one number per full run
            self.assertEqual(second[0]["run"], first[0]["run"] + 1)
            self.assertEqual(doctor.summary()["run"], second[0]["run"])
            # Age every row a preflight does not touch, then found: those rows keep their stamp and number.
            old = "2000-01-01T00:00:00Z"
            for r in doctor.LAST:
                if r["key"] not in ("model_server", "model", "search"):
                    r["checked_at"] = old
            search_at = doctor.search_status()["checked_at"]
            pf = doctor.preflight()
        self.assertTrue(pf["ok"])
        rows = self.by_key(doctor.LAST)
        self.assertEqual(rows["disk"]["checked_at"], old)
        self.assertEqual(rows["search"]["checked_at"], search_at)   # cached: younger than an hour
        self.assertNotEqual(rows["model_server"]["checked_at"], old)
        self.assertEqual({r["run"] for r in doctor.LAST}, {second[0]["run"]})   # a preflight is not a new run
        self.assertEqual(pf["checks"][0]["run"], second[0]["run"])
        self.assertEqual(doctor.summary()["run"], second[0]["run"])
        self.assertEqual(doctor._row("x", "X", "ok", "y")["run"], 0)   # outside a full run: 0 until merged

    def test_company_create_route_answers_409(self):
        with SettingsGuard():
            db.set_settings({"llm_base_url": f"http://127.0.0.1:{closed_port()}"})
            with self.assertRaises(server.PreflightError) as cm:
                server.api_company_create(Stub(), None, {"idea": "A long enough idea for a company"})
        self.assertEqual(cm.exception.status, 409)
        self.assertTrue(cm.exception.message.startswith("Fix this before founding: Model server:"))
        self.assertEqual([r["key"] for r in cm.exception.checks], ["model_server", "model"])


class TestFoundingPrompt(DoctorBase):
    def test_search_down_paragraph(self):
        c = make_company("PromptCo")
        doctor.LAST[:] = [doctor._row("search", "Web search", "bad", "Every search provider failed")]
        p = agents.founding_prompt(c, "")
        self.assertIn("Web search is down right now", p)
        self.assertIn("report_to_owner", p)
        self.assertIn("research/market.md", p)
        self.assertIn("1. Research the market", p)      # the rest of the prompt is unchanged
        doctor.LAST[:] = [doctor._row("search", "Web search", "warn", "cooling")]
        self.assertNotIn("Web search is down", agents.founding_prompt(c, ""))
        doctor.LAST[:] = []
        self.assertNotIn("Web search is down", agents.founding_prompt(c, ""))
        self.assertTrue(doctor.search_down() is False)


class TestRefresh(DoctorBase):
    def test_refresh_runs_once_in_background(self):
        started, release, calls = threading.Event(), threading.Event(), []

        def fake_run(deep=False):
            calls.append(deep)
            started.set()
            release.wait(5)
            return []

        with patch.object(doctor, "run", fake_run):
            doctor.refresh_if_stale()
            self.assertTrue(started.wait(2))
            doctor.refresh_if_stale()          # one refresh in flight: nothing new is started
            doctor.refresh_if_stale()
            self.assertEqual(calls, [False])
            self.assertTrue(doctor._refreshing)
            release.set()
            deadline = time.time() + 5
            while doctor._refreshing and time.time() < deadline:
                time.sleep(0.02)
            self.assertFalse(doctor._refreshing)
            doctor.LAST[:] = [doctor._row("disk", "Disk space", "ok", "fine")]
            doctor.refresh_if_stale()          # fresh cache: no run
            time.sleep(0.05)
            self.assertEqual(calls, [False])
            doctor.LAST[0]["checked_at"] = "2000-01-01T00:00:00Z"
            started.clear()
            doctor.refresh_if_stale(max_age_minutes=60)   # stale cache: runs again
            self.assertTrue(started.wait(2))
            self.assertEqual(calls, [False, False])
        deadline = time.time() + 5
        while doctor._refreshing and time.time() < deadline:
            time.sleep(0.02)

    def test_merge_does_not_postpone_the_refresh(self):
        calls = []
        with patch.object(doctor, "run", lambda deep=False: calls.append(deep) or []):
            fresh = doctor._row("model_server", "Model server", "ok", "fine")
            stale = doctor._row("disk", "Disk space", "ok", "fine")
            stale["checked_at"] = "2000-01-01T00:00:00Z"
            doctor.LAST[:] = [fresh, stale]   # what a preflight merge leaves behind: first row fresh, rest old
            doctor.refresh_if_stale()
            deadline = time.time() + 5
            while doctor._refreshing and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(calls, [False])   # the oldest row decides, not the first one
            doctor.LAST[:] = [fresh, doctor._row("disk", "Disk space", "ok", "fine")]
            doctor.refresh_if_stale()
            time.sleep(0.05)
            self.assertEqual(calls, [False])


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="holdco-backups-"))
        self._p = patch.object(backup, "BACKUP_DIR", self.dir)
        self._p.start()
        backup._thread = None
        backup._last_check = 0.0

    def tearDown(self):
        self._p.stop()

    def test_zip_contents_and_skips(self):
        c = make_company("ZipCo")
        root = workspace.root(c)
        (root / "docs" / "plan.md").write_text("# Plan\n", encoding="utf-8")
        (root / ".runs").mkdir(exist_ok=True)
        (root / ".runs" / "run1.json").write_text("{}", encoding="utf-8")
        (root / "site" / "node_modules").mkdir(parents=True, exist_ok=True)
        (root / "site" / "node_modules" / "x.js").write_text("", encoding="utf-8")
        vault = config.DATA_DIR / "vault.json"
        had_vault = vault.exists()
        if not had_vault:
            vault.write_text("{}", encoding="utf-8")
        try:
            p = backup.run_backup()
        finally:
            if not had_vault:
                vault.unlink()
        self.assertEqual(p.parent, self.dir)
        self.assertRegex(p.name, r"^holdco-\d{8}-\d{6}\.zip$")
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            self.assertIn("holdco.db", names)
            self.assertIn(f"companies/{c['slug']}/docs/plan.md", names)
            self.assertIn("vault.json", names)
            self.assertFalse([n for n in names if ".runs/" in n or "node_modules" in n], names)
            self.assertFalse([n for n in names if n.endswith(".part")])
            out = Path(tempfile.mkdtemp()) / "holdco.db"
            out.write_bytes(z.read("holdco.db"))
        con = sqlite3.connect(str(out))
        try:
            self.assertIn(c["name"], [r[0] for r in con.execute("SELECT name FROM companies")])
        finally:
            con.close()
        ev = db.one("SELECT * FROM events WHERE kind='info' AND message LIKE 'Backup written:%' ORDER BY id DESC")
        self.assertIn(p.name, ev["message"])
        self.assertFalse(list(self.dir.glob("*.part")))

    def test_symlinks_and_special_files_are_skipped(self):
        c = make_company("LinkCo")
        root = workspace.root(c)
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "real.md").write_text("keep me\n", encoding="utf-8")
        outside = Path(tempfile.mkdtemp(prefix="holdco-outside-"))
        (outside / "id_ed25519").write_text("PRIVATE KEY MATERIAL\n", encoding="utf-8")
        links = [root / "docs" / "keys", root / "docs" / "elsewhere"]
        try:
            os.symlink(outside / "id_ed25519", links[0])            # what a run_code task could leave behind
            os.symlink(outside, links[1], target_is_directory=True)
        except (OSError, NotImplementedError) as e:   # Windows without developer mode needs a privilege for this
            self.skipTest(f"symlinks are not available here: {e}")
        pipe = root / "docs" / "pipe"
        try:
            p = backup.run_backup()
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
                self.assertIn(f"companies/{c['slug']}/docs/real.md", names)
                self.assertFalse([n for n in names if "/keys" in n or "/elsewhere" in n], names)
                for n in names:
                    if n != "holdco.db":
                        self.assertNotIn(b"PRIVATE KEY MATERIAL", z.read(n), n)
            ev = db.one("SELECT * FROM events WHERE kind='info' AND message LIKE 'Backup written:%' "
                        "ORDER BY id DESC")
            self.assertIn(p.name, ev["message"])
            self.assertIn("skipped 2 links or special file(s)", ev["message"])
            self.assertEqual(json.loads(ev["data"])["skipped"], 2)
            # A pipe (POSIX only) is a special file: the file walk leaves it out, so zipping never waits on it.
            if hasattr(os, "mkfifo"):
                os.mkfifo(pipe)
                skipped: list = []
                files = [arc for _, arc in backup._company_files(skipped)]
                self.assertNotIn(f"companies/{c['slug']}/docs/pipe", files)
                self.assertIn(f"companies/{c['slug']}/docs/real.md", files)
                self.assertEqual(sorted(skipped), sorted(links + [pipe]))
        finally:
            for x in links + [pipe]:
                try:
                    x.unlink()
                except OSError:
                    pass

    def test_retention_last_backup_and_routes(self):
        with SettingsGuard():
            db.set_settings({"backup_keep": 2})
            paths = [backup.run_backup() for _ in range(3)]
        self.assertEqual(len(set(paths)), 3)                      # same second: distinct names
        left = sorted(p.name for p in self.dir.glob("holdco-*.zip"))
        self.assertEqual(len(left), 2)
        self.assertNotIn(paths[0].name, left)
        last = backup.last_backup()
        self.assertEqual(last["path"], str(paths[2]))
        self.assertEqual(set(last), {"path", "name", "created_at", "size"})
        self.assertIsNotNone(db.parse_time(last["created_at"]))
        self.assertGreater(last["size"], 0)
        (self.dir / "holdco-20990101-000000.zip.part").write_bytes(b"x")   # unfinished: ignored
        self.assertEqual(backup.last_backup()["path"], str(paths[2]))
        out = backup.api_backups(Stub(), None, None)
        self.assertEqual(out["dir"], str(self.dir))
        self.assertEqual([b["path"] for b in out["backups"]], [str(paths[2]), str(paths[1])])
        with SettingsGuard():
            db.set_settings({"backup_keep": 2})
            out = backup.api_backup(Stub(), None, {})
        self.assertTrue(set(out) >= {"path", "created_at", "size"})
        self.assertEqual(backup.last_backup()["path"], out["path"])
        self.assertEqual(len(list(self.dir.glob("holdco-*.zip"))), 2)   # prune uses the stored backup_keep

    def test_maybe_run_daily_in_one_thread(self):
        release, calls = threading.Event(), []

        def slow_backup():
            calls.append(1)
            release.wait(5)
            return self.dir / "x.zip"

        with patch.object(backup, "run_backup", slow_backup):
            backup.maybe_run()                       # no backup yet: starts one
            deadline = time.time() + 2
            while not calls and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(calls, [1])
            backup._last_check = 0.0
            backup.maybe_run()                       # still running: not a second one
            self.assertEqual(calls, [1])
            release.set()
            backup._thread.join(5)
            backup._last_check = 0.0
            backup.maybe_run()                       # finished, still no real backup file: runs again
            backup._thread.join(5)
            self.assertEqual(calls, [1, 1])
        backup.run_backup()
        backup._last_check = 0.0
        with patch.object(backup, "run_backup", slow_backup):
            backup.maybe_run()                       # a fresh backup exists: nothing to do
            self.assertEqual(calls, [1, 1])
        with patch.object(backup, "run_backup", side_effect=RuntimeError("disk full")):
            backup._last_check = 0.0
            with patch.object(backup, "last_backup", return_value=None):
                backup.maybe_run()
                backup._thread.join(5)
        ev = db.one("SELECT * FROM events WHERE kind='error' AND message LIKE 'Backup failed:%' ORDER BY id DESC")
        self.assertIn("disk full", ev["message"])
        db.run("DELETE FROM events WHERE id=?", (ev["id"],))


class TestSettingsValidation(unittest.TestCase):
    """PUT /api/settings refuses what would fail quietly later and clamps the numbers (Phase 0 fix)."""

    def test_bad_chat_id_and_notify_level_answer_400(self):
        with SettingsGuard():
            for bad in ("@owner", "42abc", "4 2", "--1"):
                with self.assertRaises(server.HTTPError) as cm:
                    server.api_settings_put(Stub(), None, {"telegram_chat_id": bad, "owner_name": "Sam"})
                self.assertEqual(cm.exception.status, 400, bad)
                self.assertIn("chat id", cm.exception.message)
            self.assertNotEqual(db.get_settings()["owner_name"], "Sam")   # nothing from a refused request is saved
            with self.assertRaises(server.HTTPError) as cm:
                server.api_settings_put(Stub(), None, {"notify_level": "everything"})
            self.assertEqual(cm.exception.status, 400)
            self.assertIn("notify_level", cm.exception.message)
            out = server.api_settings_put(Stub(), None, {"telegram_chat_id": " -100123456 ", "notify_level": " All "})
            self.assertEqual(out["settings"]["telegram_chat_id"], "-100123456")
            self.assertEqual(out["settings"]["notify_level"], "all")
            out = server.api_settings_put(Stub(), None, {"telegram_chat_id": "", "notify_level": "important"})
            self.assertEqual(out["settings"]["telegram_chat_id"], "")
            self.assertEqual(db.set_settings({"telegram_chat_id": 42})["telegram_chat_id"], "42")

    def test_numbers_are_clamped(self):
        with SettingsGuard():
            s = db.set_settings({"max_task_attempts": 0, "backup_keep": 500, "num_ctx": 100,
                                 "max_concurrent_runs": "12"})
            self.assertEqual((s["max_task_attempts"], s["backup_keep"], s["num_ctx"], s["max_concurrent_runs"]),
                             (1, 90, 2048, 8))
            s = db.set_settings({"max_task_attempts": "3", "backup_keep": 7.0, "num_ctx": 65536,
                                 "max_concurrent_runs": 1})
            self.assertEqual((s["max_task_attempts"], s["backup_keep"], s["num_ctx"], s["max_concurrent_runs"]),
                             (3, 7, 65536, 1))
            with self.assertRaises(ValueError):
                db.set_settings({"backup_keep": "lots"})
            with self.assertRaises(server.HTTPError) as cm:
                server.api_settings_put(Stub(), None, {"max_task_attempts": "two"})
            self.assertEqual(cm.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
