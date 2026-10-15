"""Phase 1: the credential vault (holdco/vault.py), the secret settings routed through it, and redaction.

The file backend runs everywhere (HOLDCO_VAULT_BACKEND=file is set before Holdco is imported, so no test touches a
real keyring). DPAPI is tested on Windows only; the Secret Service backend is tested on Linux and macOS against a
fake secret-tool program put first on PATH. No network.

Run with:  python -m unittest tests.test_phase1_vault -v
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-vault-"))
os.environ["HOLDCO_VAULT_BACKEND"] = "file"
os.environ.pop("HOLDCO_VAULT_PASSPHRASE", None)

from holdco import actions, agents, bus, config, db, llm, roles, routing, server, truth, vault, workspace  # noqa: E402,F401
from holdco.routing import HTTPError  # noqa: E402

db.init()

_HAD_FILES: dict[str, bool] = {}


def setUpModule():
    for f in ("vault.json", "vault.key"):
        _HAD_FILES[f] = (config.DATA_DIR / f).exists()


def tearDownModule():
    # Leave the shared test data folder as this module found it.
    vault._reset()
    try:
        empty = not vault.names()
    except vault.VaultError:
        empty = False
    for f in ("vault.json", "vault.key"):
        if empty and not _HAD_FILES.get(f, True):
            try:
                (config.DATA_DIR / f).unlink()
            except OSError:
                pass
    vault._reset()


def secret(tag: str) -> str:
    """A value no other test (or real key) will ever contain."""
    return f"hv-{tag}-{secrets.token_hex(12)}"


def make_company(name="VaultCo", members=("ceo", "marketing"), settings=None) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "An idea", "active", "building", json.dumps(settings or {}), db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


class Stub:
    """Stands in for the request handler that route functions receive."""
    query: dict = {}


def call(method: str, path: str, body=None):
    """Dispatch to a dashboard route the way server.Handler does."""
    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(Stub(), m, body)
    raise AssertionError(f"no route for {method} {path}")


def raw_setting(key: str):
    v = db.scalar("SELECT value FROM settings WHERE key=?", (key,))
    return None if v is None else json.loads(v)


def raw_company(cid: int) -> dict:
    return json.loads(db.scalar("SELECT settings FROM companies WHERE id=?", (cid,)) or "{}")


class Snapshot:
    """Put the settings table and every company's stored settings back afterwards, so other test modules sharing
    this database see what they expect."""

    def __enter__(self):
        self.settings = db.q("SELECT key, value FROM settings")
        self.companies = db.q("SELECT id, settings FROM companies")
        return self

    def __exit__(self, *a):
        db.run("DELETE FROM settings")
        for r in self.settings:
            db.run("INSERT INTO settings(key, value) VALUES(?, ?)", (r["key"], r["value"]))
        for r in self.companies:
            db.run("UPDATE companies SET settings=? WHERE id=?", (r["settings"], r["id"]))


class VaultCase(unittest.TestCase):
    """Every test starts with the file backend and nothing cached, and removes the secrets it stored."""

    def setUp(self):
        os.environ["HOLDCO_VAULT_BACKEND"] = "file"
        os.environ.pop("HOLDCO_VAULT_PASSPHRASE", None)
        vault._reset()
        self.before = set(vault.names())

    def tearDown(self):
        os.environ["HOLDCO_VAULT_BACKEND"] = "file"
        os.environ.pop("HOLDCO_VAULT_PASSPHRASE", None)
        vault._reset()
        for n in set(vault.names()) - self.before:
            vault.delete(n)
        vault._reset()

    def vault_file(self) -> dict:
        return json.loads((config.DATA_DIR / "vault.json").read_text(encoding="utf-8"))

    @contextlib.contextmanager
    def tampered(self):
        """Yield the parsed vault.json for editing; it is written, the caches dropped, and afterwards the original
        bytes are restored."""
        p = config.DATA_DIR / "vault.json"
        original = p.read_bytes() if p.exists() else None
        data = json.loads(original) if original else {"version": 1, "secrets": {}}
        try:
            yield data
        finally:
            if original is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(original)
            vault._reset()

    def write_vault(self, data: dict):
        (config.DATA_DIR / "vault.json").write_text(json.dumps(data), encoding="utf-8")
        vault._reset()


def open_like_the_design(entry: dict, master: bytes) -> str:
    """Decrypt one file-backend entry with the construction in the design document, independently of vault.py."""
    enc = hmac.new(master, b"enc", hashlib.sha256).digest()
    mac = hmac.new(master, b"mac", hashlib.sha256).digest()
    nonce, ct, tag = (base64.b64decode(entry[k]) for k in ("nonce", "ct", "tag"))
    assert len(nonce) == 16
    assert hmac.compare_digest(hmac.new(mac, nonce + ct, hashlib.sha256).digest(), tag)
    stream = b"".join(hmac.new(enc, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
                      for i in range(len(ct) // 32 + 1))
    return bytes(a ^ b for a, b in zip(ct, stream)).decode("utf-8")


# =============================================================== the store itself

class TestVaultStore(VaultCase):
    def test_round_trip_with_dotted_names(self):
        self.assertEqual(vault.backend(), "file")
        values = {"settings.brave_api_key": secret("a"), "company.41.smtp_password": secret("b"),
                  "connector.7.api_key": secret("c"),
                  "oauth.7": json.dumps({"access_token": secret("d"), "refresh_token": secret("e")})}
        for n, v in values.items():
            vault.put(n, v)
        for n, v in values.items():
            self.assertEqual(vault.get(n), v)
        self.assertTrue(set(values) <= set(vault.names()))
        self.assertEqual(vault.names(), sorted(vault.names()))
        vault._reset()   # a fresh start reads them from disk
        for n, v in values.items():
            self.assertEqual(vault.get(n), v)
        new = secret("c2")
        vault.put("connector.7.api_key", new)
        self.assertEqual(vault.get("connector.7.api_key"), new)
        vault.delete("connector.7.api_key")
        self.assertIsNone(vault.get("connector.7.api_key"))
        self.assertNotIn("connector.7.api_key", vault.names())
        vault.delete("connector.7.api_key")   # deleting twice is fine
        vault.put("company.41.smtp_password", "")   # an empty value deletes
        self.assertIsNone(vault.get("company.41.smtp_password"))
        self.assertIsNone(vault.get("connector.404.nothing"))
        for bad in ("", "has space", "../x", None):
            with self.assertRaises(vault.VaultError):
                vault.put(bad, "value-1234")

    def test_file_layout_matches_the_design(self):
        value = secret("layout")
        vault.put("connector.8.api_key", value)
        text = (config.DATA_DIR / "vault.json").read_text(encoding="utf-8")
        self.assertNotIn(value, text)
        data = json.loads(text)
        self.assertEqual(data["version"], 1)
        self.assertTrue(base64.b64decode(data["salt"]))
        entry = data["secrets"]["connector.8.api_key"]
        self.assertEqual(entry["backend"], "file")
        self.assertTrue({"nonce", "ct", "tag"} <= set(entry))
        key_file = config.DATA_DIR / "vault.key"
        key = key_file.read_bytes()
        self.assertEqual(len(key), 32)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertEqual(open_like_the_design(entry, key), value)
        vault.put("connector.8.other", value)   # the same value twice: a fresh nonce each time
        again = self.vault_file()["secrets"]["connector.8.other"]
        self.assertNotEqual(again["nonce"], entry["nonce"])
        self.assertNotEqual(again["ct"], entry["ct"])

    def test_a_second_process_with_the_same_key_reads_the_value(self):
        value = secret("second")
        vault.put("connector.9.api_key", value)
        code = ("import hashlib, sys; sys.path.insert(0, sys.argv[1]); from holdco import vault; "
                "v = vault.get('connector.9.api_key'); print(hashlib.sha256(v.encode()).hexdigest() if v else 'none')")
        env = {**os.environ, "HOLDCO_DATA": str(config.DATA_DIR), "HOLDCO_VAULT_BACKEND": "file"}
        env.pop("HOLDCO_VAULT_PASSPHRASE", None)
        r = subprocess.run([sys.executable, "-c", code, str(ROOT)], env=env, capture_output=True, text=True,
                           timeout=120, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), hashlib.sha256(value.encode()).hexdigest())

    def test_passphrase_derivation_and_a_wrong_passphrase(self):
        os.environ["HOLDCO_VAULT_PASSPHRASE"] = "correct horse battery staple"
        vault._reset()
        value = secret("pass")
        vault.put("connector.10.api_key", value)
        self.assertEqual(vault.get("connector.10.api_key"), value)
        data = self.vault_file()
        entry = data["secrets"]["connector.10.api_key"]
        master = hashlib.pbkdf2_hmac("sha256", b"correct horse battery staple", base64.b64decode(data["salt"]), 200000)
        self.assertEqual(open_like_the_design(entry, master), value)
        os.environ["HOLDCO_VAULT_PASSPHRASE"] = "wrong horse"
        vault._reset()
        with self.assertRaises(vault.VaultError) as cm:
            vault.get("connector.10.api_key")
        self.assertIn("integrity check", str(cm.exception))
        self.assertNotIn(value, str(cm.exception))
        os.environ.pop("HOLDCO_VAULT_PASSPHRASE")
        vault._reset()
        with self.assertRaises(vault.VaultError) as cm:   # vault.key instead of the passphrase: says which is missing
            vault.get("connector.10.api_key")
        self.assertIn("HOLDCO_VAULT_PASSPHRASE", str(cm.exception))

    def test_one_flipped_ciphertext_byte_is_refused(self):
        value = secret("flip")
        vault.put("connector.11.api_key", value)
        with self.tampered() as data:
            entry = data["secrets"]["connector.11.api_key"]
            ct = bytearray(base64.b64decode(entry["ct"]))
            ct[3] ^= 0x01
            entry["ct"] = base64.b64encode(bytes(ct)).decode()
            self.write_vault(data)
            with self.assertRaises(vault.VaultError) as cm:
                vault.get("connector.11.api_key")
            self.assertIn("connector.11.api_key", str(cm.exception))
            self.assertEqual(vault.doctor_rows()[0]["status"], "bad")
        self.assertEqual(vault.get("connector.11.api_key"), value)   # the original file reads again

    def test_a_foreign_backend_secret_fails_loudly(self):
        foreign = "secret-service" if os.name == "nt" else "dpapi"
        name = "company.999.smtp_password"
        with self.tampered() as data:
            data["secrets"][name] = {"backend": foreign, "blob": "AAAA"}
            self.write_vault(data)
            with self.assertRaises(vault.VaultError) as cm:
                vault.get(name)
            self.assertEqual(str(cm.exception),
                             f"Secret {name} was stored with {foreign} on another machine; enter it again in Settings")
            row = vault.doctor_rows()[0]
            self.assertEqual((row["key"], row["status"]), ("vault", "bad"))
            self.assertIn(name, row["detail"])
            self.assertEqual(row["fix"]["kind"], "text")
            with contextlib.redirect_stdout(io.StringIO()):   # a marker pointing at it reads as unset
                self.assertEqual(db._resolve_secrets({"smtp_password": "vault:" + name})["smtp_password"], "")
            vault.delete(name)   # entering it again starts by replacing it
            self.assertEqual(vault.doctor_rows()[0]["status"], "ok")

    def test_doctor_row(self):
        vault.put("connector.12.api_key", secret("doc"))
        row = vault.doctor_rows()[0]
        self.assertEqual((row["key"], row["status"]), ("vault", "ok"))
        self.assertTrue(row["detail"].startswith("file"))
        with self.tampered():
            (config.DATA_DIR / "vault.json").write_text("{not json", encoding="utf-8")
            vault._reset()
            row = vault.doctor_rows()[0]
            self.assertEqual(row["status"], "bad")
            self.assertIn("damaged", row["detail"])
            self.assertEqual(row["fix"]["kind"], "text")
            with self.assertRaises(vault.VaultError):
                vault.names()
            self.assertEqual(vault.redact("nothing to hide here"), "nothing to hide here")   # never raises
        with mock.patch.dict(os.environ, {"HOLDCO_VAULT_BACKEND": "dpapi" if os.name != "nt" else "file"}):
            vault._reset()
            if os.name != "nt":
                self.assertEqual(vault.doctor_rows()[0]["status"], "bad")
        vault._reset()

    def test_secrets_never_reach_the_console(self):
        value = secret("quiet")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            vault.put("connector.13.api_key", value)
            vault.get("connector.13.api_key")
            vault.redact(value)
            vault.doctor_rows()
        self.assertNotIn(value, out.getvalue())


# =============================================================== migration and the settings routes

class TestMigration(VaultCase):
    def test_migrate_moves_secrets_and_readers_resolve_them(self):
        with Snapshot():
            key, pw = secret("brave"), secret("smtp")
            db.set_settings({"brave_api_key": key, "llm_api_key": ""})
            c = make_company("MigrateCo", settings={"smtp_host": "smtp.example.org", "smtp_password": pw})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                moved = vault.migrate()
            self.assertGreaterEqual(moved, 2)
            self.assertRegex(out.getvalue(), r"Vault: file, moved \d+ secrets? out of the settings tables")
            self.assertNotIn(key, out.getvalue())
            self.assertEqual(raw_setting("brave_api_key"), "vault:settings.brave_api_key")
            self.assertEqual(raw_setting("llm_api_key"), "")   # empty stays empty
            self.assertEqual(raw_company(c["id"])["smtp_password"], f"vault:company.{c['id']}.smtp_password")
            self.assertEqual(raw_company(c["id"])["smtp_host"], "smtp.example.org")
            self.assertEqual(db.get_settings()["brave_api_key"], key)
            self.assertEqual(db.company_settings(db.get_company(c["id"]))["smtp_password"], pw)
            # the plain text is gone from the database files, not only from the rows
            for f in (config.DB_PATH, Path(str(config.DB_PATH) + "-wal")):
                if f.exists():
                    blob = f.read_bytes()
                    self.assertFalse(key.encode() in blob, f"{f.name} still holds the plain value")
                    self.assertFalse(pw.encode() in blob, f"{f.name} still holds the plain value")
            # the routes mask them
            self.assertEqual(call("GET", "/api/settings")["settings"]["brave_api_key"], vault.MASK)
            self.assertEqual(server._public_company(db.get_company(c["id"]))["settings"]["smtp_password"], vault.MASK)
            # a second start moves nothing and says nothing
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(vault.migrate(), 0)
            self.assertEqual(out.getvalue(), "")


class TestSettingsRoutes(VaultCase):
    def test_global_secrets_masked_kept_and_deleted(self):
        with Snapshot():
            key = secret("brave")
            call("PUT", "/api/settings", {"llm_api_key": ""})
            out = call("PUT", "/api/settings", {"brave_api_key": key, "owner_name": "Sam"})
            self.assertEqual(out["settings"]["brave_api_key"], vault.MASK)   # the save's answer is masked too
            self.assertEqual(out["settings"]["owner_name"], "Sam")
            self.assertEqual(raw_setting("brave_api_key"), "vault:settings.brave_api_key")
            self.assertEqual(vault.get("settings.brave_api_key"), key)
            self.assertEqual(db.get_settings()["brave_api_key"], key)
            got = call("GET", "/api/settings")["settings"]
            self.assertEqual(got["brave_api_key"], vault.MASK)
            self.assertEqual(got["llm_api_key"], "")   # masked only when set
            self.assertNotIn(key, json.dumps(got))
            # the form sends the mask back: nothing changes
            call("PUT", "/api/settings", {"brave_api_key": vault.MASK, "owner_name": "Sam"})
            self.assertEqual(vault.get("settings.brave_api_key"), key)
            self.assertEqual(raw_setting("brave_api_key"), "vault:settings.brave_api_key")
            # a malformed value elsewhere saves nothing, the new secret included
            with self.assertRaises(HTTPError):
                call("PUT", "/api/settings", {"brave_api_key": secret("other"), "telegram_chat_id": "not-a-number"})
            self.assertEqual(vault.get("settings.brave_api_key"), key)
            # an empty value deletes the secret
            call("PUT", "/api/settings", {"brave_api_key": ""})
            self.assertIsNone(vault.get("settings.brave_api_key"))
            self.assertNotIn("settings.brave_api_key", vault.names())
            self.assertEqual(raw_setting("brave_api_key"), "")
            self.assertEqual(db.get_settings()["brave_api_key"], "")
            self.assertEqual(call("GET", "/api/settings")["settings"]["brave_api_key"], "")

    def test_company_secrets_masked_kept_and_deleted(self):
        c = make_company("PatchVaultCo")
        pw, token = secret("smtp"), secret("netlify")
        path = f"/api/companies/{c['id']}"
        out = call("PATCH", path, {"settings": {"smtp_host": "smtp.example.org", "smtp_password": pw,
                                                "netlify_token": token}})["company"]
        self.assertEqual(out["settings"]["smtp_password"], vault.MASK)
        self.assertEqual(out["settings"]["netlify_token"], vault.MASK)
        self.assertEqual(out["settings"]["imap_password"], "")   # masked only when set
        self.assertEqual(out["settings"]["smtp_host"], "smtp.example.org")
        stored = raw_company(c["id"])
        self.assertEqual(stored["smtp_password"], f"vault:company.{c['id']}.smtp_password")
        self.assertNotIn(pw, json.dumps(stored))
        self.assertEqual(vault.get(f"company.{c['id']}.smtp_password"), pw)
        self.assertEqual(db.company_settings(db.get_company(c["id"]))["netlify_token"], token)
        # an unrelated change writes the markers back, never the values they resolve to
        call("PATCH", path, {"settings": {"directives": "Be brief.", "smtp_password": vault.MASK}})
        stored = raw_company(c["id"])
        self.assertEqual(stored["directives"], "Be brief.")
        self.assertNotIn(pw, json.dumps(stored))
        self.assertNotIn(token, json.dumps(stored))
        self.assertEqual(vault.get(f"company.{c['id']}.smtp_password"), pw)
        # an empty value deletes
        out = call("PATCH", path, {"settings": {"netlify_token": ""}})["company"]
        self.assertEqual(out["settings"]["netlify_token"], "")
        self.assertIsNone(vault.get(f"company.{c['id']}.netlify_token"))
        self.assertEqual(raw_company(c["id"])["netlify_token"], "")
        self.assertEqual(server._public_company(db.get_company(c["id"]))["settings"]["smtp_password"], vault.MASK)

    def test_a_plain_value_left_by_an_older_writer_moves_on_the_next_save(self):
        c = make_company("OldWriterCo", settings={"smtp_password": "plain-old-password-1"})
        call("PATCH", f"/api/companies/{c['id']}", {"settings": {"directives": "x"}})
        self.assertEqual(raw_company(c["id"])["smtp_password"], f"vault:company.{c['id']}.smtp_password")
        self.assertEqual(vault.get(f"company.{c['id']}.smtp_password"), "plain-old-password-1")


# =============================================================== redaction

class TestRedaction(VaultCase):
    def test_redact_rules(self):
        value = secret("rule")
        vault.put("connector.20.api_key", value)
        self.assertEqual(vault.redact(f"key={value}; again {value}"), "key=[redacted]; again [redacted]")
        vault.put("connector.20.pin", "1234567")   # under 8 characters: too common to replace everywhere
        self.assertEqual(vault.redact("pin 1234567"), "pin 1234567")
        odd = 'pa"ss\\word-' + secrets.token_hex(6)   # characters JSON escapes
        vault.put("connector.20.password", odd)
        dumped = vault.redact(json.dumps({"p": odd}))
        self.assertNotIn(json.dumps(odd)[1:-1], dumped)
        self.assertEqual(json.loads(dumped), {"p": "[redacted]"})
        access, refresh = secret("access"), secret("refresh")
        vault.put("oauth.20", json.dumps({"access_token": access, "refresh_token": refresh,
                                          "token_endpoint": "https://auth.example.org/token", "expires_at": 1}))
        self.assertEqual(vault.redact(f"Bearer {access} {refresh} at https://auth.example.org/token"),
                         "Bearer [redacted] [redacted] at https://auth.example.org/token")
        self.assertIsNone(vault.redact(None))
        self.assertEqual(vault.redact(""), "")

    def test_cache_follows_put_and_delete(self):
        old, new = secret("old"), secret("new")
        vault.put("connector.21.api_key", old)
        self.assertEqual(vault.redact(old), "[redacted]")
        vault.put("connector.21.api_key", new)
        self.assertEqual(vault.redact(new), "[redacted]")
        self.assertEqual(vault.redact(old), old)
        vault.delete("connector.21.api_key")
        self.assertEqual(vault.redact(new), new)

    def test_first_use_sweeps_then_no_decryption_per_call(self):
        value = secret("sweep")
        vault.put("connector.22.api_key", value)
        vault._reset()   # as after a restart: nothing loaded yet
        with mock.patch.object(vault, "_file_open", wraps=vault._file_open) as opened:
            self.assertEqual(vault.redact(f"x {value} y"), "x [redacted] y")
            first = opened.call_count
            self.assertGreaterEqual(first, 1)
            for _ in range(200):
                vault.redact(f"x {value} y")
            self.assertEqual(opened.call_count, first)
        start = time.perf_counter()
        for _ in range(2000):
            vault.redact("An ordinary activity feed line about a task that moved to done. " * 4)
        self.assertLess((time.perf_counter() - start) / 2000, 0.001)   # far under a millisecond per event

    def test_bus_emit_message_and_data(self):
        c = make_company("EmitCo", members=())
        value = secret("emit")
        vault.put("connector.23.api_key", value)
        ev = bus.emit(c["id"], "info", f"Called the API with {value}", None,
                      {"headers": {"Authorization": f"Bearer {value}"}, "list": [value], "n": 3})
        row = db.one("SELECT message, data FROM events WHERE id=?", (ev["id"],))
        self.assertNotIn(value, row["message"])
        self.assertNotIn(value, row["data"])
        self.assertIn("[redacted]", row["message"])
        self.assertEqual(json.loads(row["data"]), {"headers": {"Authorization": "Bearer [redacted]"},
                                                   "list": ["[redacted]"], "n": 3})
        self.assertNotIn(value, json.dumps(ev))

    def test_stored_transcript_and_thinking_events(self):
        c = make_company("TranscriptCo", members=("ceo", "marketing"))
        value = secret("tool")
        vault.put("connector.24.api_key", value)
        p = workspace.safe_path(c, "docs/keys.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# Keys\nThe Stripe key is {value}\n", encoding="utf-8")
        agent = db.one("SELECT * FROM agents WHERE company_id=? AND role='marketing'", (c["id"],))
        task_id = db.run("INSERT INTO tasks(company_id, title, role, status, deliverable, created_by, created_at, "
                         "updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         (c["id"], "Check the notes", "marketing", "in_progress", "docs/keys.md", "test", db.now(),
                          db.now()))
        steps = iter([("read_file", {"path": "docs/keys.md"})])

        def fake_chat(messages, tools_spec=None, *a, **kw):
            try:
                name, args = next(steps)
            except StopIteration:
                return llm.Reply(content="")
            return llm.Reply(tool_calls=[llm.ToolCall(name=name, arguments=args)])

        with mock.patch.object(agents.llm, "chat", fake_chat):
            agents.run(agent["id"], "task", task_id=task_id)
        run = db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (agent["id"],))
        transcript = json.loads(run["transcript"])
        results = [m["content"] for m in transcript if m.get("role") == "tool" and m.get("name") == "read_file"]
        self.assertTrue(results, transcript)
        self.assertIn("The Stripe key is [redacted]", results[0])
        self.assertNotIn(value, run["transcript"])
        for e in db.q("SELECT message, data FROM events WHERE company_id=?", (c["id"],)):
            self.assertNotIn(value, e["message"] + e["data"])

    def test_a_connector_result_never_reaches_the_audit_log(self):
        c = make_company("AuditCo", members=())
        value = secret("audit")
        vault.put("connector.25.api_key", value)
        aid = actions.audit(c["id"], "connector_call", "Forge", "policy:auto",
                            {"connector": "stripe", "tool": "create_payment_link", "tag": "publish",
                             "args": {"api_key": value}}, f'HTTP 200 {{"secret": "{value}"}}')
        row = db.one("SELECT payload, result FROM audit WHERE id=?", (aid,))
        self.assertNotIn(value, row["payload"])
        self.assertNotIn(value, row["result"])
        self.assertEqual(json.loads(row["payload"])["args"]["api_key"], "[redacted]")
        self.assertIn("[redacted]", row["result"])

    def test_records_files(self):
        c = make_company("RecordsCo", members=())
        value = secret("records")
        vault.put("connector.26.api_key", value)
        db.run("INSERT INTO emails(company_id, direction, to_addr, subject, body, status, created_at) "
               "VALUES(?,?,?,?,?,?,?)", (c["id"], "out", "pat@example.org", f"Your key {value}",
                                         f"Here is the key: {value}", "sent", db.now()))
        truth.refresh_records(c)
        text = (workspace.root(c) / "records" / "emails.md").read_text(encoding="utf-8")
        self.assertIn("pat@example.org", text)
        self.assertIn("[redacted]", text)
        for f in (workspace.root(c) / "records").glob("*.md"):
            self.assertNotIn(value, f.read_text(encoding="utf-8"), f.name)


# =============================================================== the other backends

@unittest.skipUnless(os.name == "nt", "DPAPI exists only on Windows")
class TestDpapi(VaultCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "DATA_DIR", Path(tmp)), \
                mock.patch.dict(os.environ, {"HOLDCO_VAULT_BACKEND": "dpapi"}):
            vault._reset()
            self.assertEqual(vault.backend(), "dpapi")
            blob = vault._dpapi(b"plain bytes", protect=True)
            self.assertNotIn(b"plain bytes", blob)
            self.assertEqual(vault._dpapi(blob, protect=False), b"plain bytes")
            value = secret("dpapi")
            vault.put("connector.30.api_key", value)
            entry = json.loads((Path(tmp) / "vault.json").read_text(encoding="utf-8"))["secrets"]["connector.30.api_key"]
            self.assertEqual(entry["backend"], "dpapi")
            self.assertNotIn(value.encode(), base64.b64decode(entry["blob"]))
            vault._reset()
            self.assertEqual(vault.get("connector.30.api_key"), value)
            self.assertEqual(vault.doctor_rows()[0]["status"], "ok")
            vault.delete("connector.30.api_key")
            self.assertIsNone(vault.get("connector.30.api_key"))
        vault._reset()


FAKE_SECRET_TOOL = textwrap.dedent('''
    """A stand-in for secret-tool: items live in a JSON file; every command line is logged."""
    import json, os, sys
    path = os.environ["FAKE_SECRET_TOOL_STORE"]
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {"items": [], "calls": []}
    args = sys.argv[1:]
    state["calls"].append(args)
    pairs = lambda a: dict(zip(a[0::2], a[1::2]))
    matches = lambda item, q: all(item["attrs"].get(k) == v for k, v in q.items())
    code = 0
    if args[0] == "store":
        q = pairs(args[3:])
        state["items"] = [i for i in state["items"] if i["attrs"] != q]
        state["items"].append({"label": args[2], "attrs": q, "value": sys.stdin.read()})
    elif args[0] == "lookup":
        found = [i for i in state["items"] if matches(i, pairs(args[1:]))]
        if found:
            sys.stdout.write(found[0]["value"] + os.environ.get("FAKE_SECRET_TOOL_SUFFIX", ""))
        else:
            code = 1
    elif args[0] == "clear":
        state["items"] = [i for i in state["items"] if not matches(i, pairs(args[1:]))]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    sys.exit(code)
''')


@unittest.skipIf(os.name == "nt", "secret-tool is a Linux program")
class TestSecretService(VaultCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="holdco-secret-tool-"))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (self.tmp / "fake_secret_tool.py").write_text(FAKE_SECRET_TOOL, encoding="utf-8")
        tool = bin_dir / "secret-tool"
        tool.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.tmp / "fake_secret_tool.py"}" "$@"\n',
                        encoding="utf-8")
        tool.chmod(0o755)
        self.store = self.tmp / "store.json"
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.patches = [mock.patch.object(config, "DATA_DIR", self.data),
                        mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                                                     "FAKE_SECRET_TOOL_STORE": str(self.store),
                                                     "HOLDCO_VAULT_BACKEND": "auto"})]
        for p in self.patches:
            p.start()
        vault._reset()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()

    def state(self) -> dict:
        return json.loads(self.store.read_text(encoding="utf-8"))

    def test_chosen_after_a_probe_and_round_trip(self):
        self.assertEqual(vault.backend(), "secret-service")
        self.assertEqual([c[0] for c in self.state()["calls"]], ["store", "lookup", "clear"])   # the probe
        self.assertEqual(self.state()["items"], [])   # it cleans up after itself
        value = secret("keyring")
        vault.put("connector.40.api_key", value)
        item = self.state()["items"][0]
        self.assertEqual(item["value"], value)   # sent on stdin...
        self.assertEqual((item["attrs"]["app"], item["attrs"]["name"]), ("holdco", "connector.40.api_key"))
        self.assertEqual(item["label"], "Holdco connector.40.api_key")
        self.assertFalse([c for c in self.state()["calls"] if any(value in a for a in c)])   # ...never on argv
        text = (self.data / "vault.json").read_text(encoding="utf-8")
        self.assertNotIn(value, text)
        self.assertEqual(json.loads(text)["secrets"]["connector.40.api_key"], {"backend": "secret-service"})
        self.assertEqual(vault.names(), ["connector.40.api_key"])
        vault._reset()
        self.assertEqual(vault.get("connector.40.api_key"), value)   # read back through lookup
        self.assertEqual(vault.redact(f"k={value}"), "k=[redacted]")
        self.assertEqual(vault.doctor_rows()[0]["status"], "ok")
        vault.delete("connector.40.api_key")
        self.assertEqual(self.state()["items"], [])
        self.assertIsNone(vault.get("connector.40.api_key"))

    def test_a_secret_missing_from_the_keyring(self):
        value = secret("gone")
        vault.put("connector.41.api_key", value)
        self.store.write_text(json.dumps({"items": [], "calls": []}), encoding="utf-8")   # another machine's keyring
        vault._reset()
        with self.assertRaises(vault.VaultError) as cm:
            vault.get("connector.41.api_key")
        self.assertIn("keyring", str(cm.exception))
        self.assertEqual(vault.doctor_rows()[0]["status"], "bad")

    def test_auto_keeps_the_backend_the_stored_secrets_use(self):
        value = secret("kept")
        with mock.patch.dict(os.environ, {"HOLDCO_VAULT_BACKEND": "file"}):
            vault._reset()
            vault.put("connector.42.api_key", value)
        vault._reset()   # auto again, now with a working keyring on PATH: the stored secrets still read
        self.assertEqual(vault.backend(), "file")
        self.assertEqual(vault.get("connector.42.api_key"), value)

    def test_a_probe_that_fails_falls_back_to_the_file(self):
        with mock.patch.dict(os.environ, {"FAKE_SECRET_TOOL_SUFFIX": "-garbled"}):
            vault._reset()
            self.assertEqual(vault.backend(), "file")
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp / "empty")}):
            vault._reset()
            self.assertEqual(vault.backend(), "file")


if __name__ == "__main__":
    unittest.main()
