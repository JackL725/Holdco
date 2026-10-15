"""SQLite storage. One shared connection guarded by a lock (the app is single-user)."""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import threading
from typing import Any

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
HAS_FTS = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY, name TEXT, slug TEXT UNIQUE, idea TEXT, mission TEXT DEFAULT '',
    charter TEXT DEFAULT '', status TEXT DEFAULT 'founding', stage TEXT DEFAULT 'idea',
    settings TEXT DEFAULT '{}', tokens_used INTEGER DEFAULT 0, cycle_requested INTEGER DEFAULT 0,
    created_at TEXT, last_cycle_at TEXT
);
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY, company_id INTEGER, role TEXT, name TEXT, title TEXT, focus TEXT DEFAULT '',
    model TEXT DEFAULT '', enabled INTEGER DEFAULT 1, status TEXT DEFAULT 'idle', current_task_id INTEGER,
    status_note TEXT DEFAULT '', last_run_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, company_id INTEGER, title TEXT, description TEXT DEFAULT '', role TEXT,
    agent_id INTEGER, status TEXT DEFAULT 'todo', priority INTEGER DEFAULT 2, created_by TEXT,
    result TEXT DEFAULT '', attempts INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY, company_id INTEGER, agent_id INTEGER, kind TEXT DEFAULT 'note',
    content TEXT, pinned INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY, company_id INTEGER, agent_id INTEGER, task_id INTEGER, action TEXT,
    summary TEXT, payload TEXT DEFAULT '{}', status TEXT DEFAULT 'pending', result TEXT DEFAULT '',
    feedback TEXT DEFAULT '', created_at TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, company_id INTEGER, agent_id INTEGER, kind TEXT, message TEXT,
    data TEXT DEFAULT '{}', created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, company_id INTEGER, agent_id INTEGER, sender TEXT, content TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, company_id INTEGER, agent_id INTEGER, task_id INTEGER, kind TEXT,
    status TEXT DEFAULT 'running', summary TEXT DEFAULT '', transcript TEXT DEFAULT '[]',
    tokens INTEGER DEFAULT 0, steps INTEGER DEFAULT 0, started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY, company_id INTEGER, direction TEXT, uid TEXT, message_id TEXT,
    from_addr TEXT, to_addr TEXT, subject TEXT, body TEXT, status TEXT DEFAULT 'new', created_at TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY, company_id INTEGER, agent_id INTEGER, name TEXT, value REAL, note TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, created_at TEXT, company_id INTEGER, action TEXT, proposed_by TEXT DEFAULT '',
    approved_by TEXT DEFAULT '', payload TEXT DEFAULT '{}', result TEXT DEFAULT '', approval_id INTEGER
);
CREATE TABLE IF NOT EXISTS index_state (
    company_id INTEGER, source TEXT, ref TEXT, stamp TEXT, PRIMARY KEY (company_id, source, ref)
);
CREATE TABLE IF NOT EXISTS search_vectors (rowid INTEGER PRIMARY KEY, model TEXT, vector TEXT);
CREATE TABLE IF NOT EXISTS fetched (
    id INTEGER PRIMARY KEY, company_id INTEGER, url TEXT, sha TEXT, task_id INTEGER, run_id INTEGER, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, company_id INTEGER, agent_id INTEGER, task_id INTEGER, claim TEXT, url TEXT,
    quote TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS connectors (
    id INTEGER PRIMARY KEY, company_id INTEGER, key TEXT, enabled INTEGER DEFAULT 1, config TEXT DEFAULT '{}',
    status TEXT DEFAULT '', detail TEXT DEFAULT '', last_check TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_connectors_key ON connectors(key, company_id);
CREATE INDEX IF NOT EXISTS ix_fetched_company ON fetched(company_id, url);
CREATE INDEX IF NOT EXISTS ix_facts_company ON facts(company_id, id);
CREATE INDEX IF NOT EXISTS ix_events_company ON events(company_id, id);
CREATE INDEX IF NOT EXISTS ix_audit_company ON audit(company_id, id);
CREATE INDEX IF NOT EXISTS ix_tasks_company ON tasks(company_id, status);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS ix_memory_company ON memory(company_id);
CREATE INDEX IF NOT EXISTS ix_messages_company ON messages(company_id, id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, content='memory', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
  INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    title, heading, body,
    company_id UNINDEXED, source UNINDEXED, ref UNINDEXED, line UNINDEXED, updated_at UNINDEXED,
    tokenize = 'porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, content) VALUES('delete', old.id, old.content);
  INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    try:
        return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def minutes_since(s: str | None) -> float:
    t = parse_time(s)
    if not t:
        return 1e9
    return (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 60


# Columns added after the first release; added to older databases on start.
MIGRATIONS = {
    "agents": {"preset": "TEXT DEFAULT ''", "reasoning": "TEXT DEFAULT ''", "routines_on": "INTEGER DEFAULT 1"},
    "tasks": {"deliverable": "TEXT DEFAULT ''", "acceptance": "TEXT DEFAULT ''", "routine": "TEXT DEFAULT ''",
              # Phase 0: working notes that survive retries, and how often a task changed hands (watchdog)
              "notes": "TEXT DEFAULT ''", "reassigned": "INTEGER DEFAULT 0",
              # Phase 0.5: outreach | deploy | post | purchase | ask | research | build | write
              "kind": "TEXT DEFAULT ''"},
    # Phase 0.5: the change stamp and backoff that stop idle re-planning
    "companies": {"last_cycle_stamp": "TEXT DEFAULT ''", "cycle_backoff": "INTEGER DEFAULT 0"},
    # Phase 1: where a number came from (a connector key, or 'manual' when an agent typed it)
    "metrics": {"source": "TEXT DEFAULT 'manual'"},
    # Phase 1: the Message-ID an inbound email answers, so replies to outreach can be counted
    "emails": {"in_reply_to": "TEXT DEFAULT ''"},
}


def _migrate() -> None:
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, decl in cols.items():
            if col not in have:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init() -> None:
    global _conn, HAS_FTS
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.COMPANIES_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False, timeout=30)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=OFF")
        _conn.executescript(SCHEMA)
        _migrate()
        try:
            _conn.executescript(FTS_SCHEMA)
            HAS_FTS = True
        except sqlite3.OperationalError:
            HAS_FTS = False
        _conn.commit()
    try:   # Phase 1: plain-text secrets move into the vault (a no-op once done)
        from . import vault
        vault.migrate()
    except Exception as e:   # a vault problem must not stop Holdco from starting; the Doctor reports it
        print(f"Vault migration failed: {e}")


def q(sql: str, params: tuple | list = ()) -> list[dict]:
    with _lock:
        cur = _conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        return rows


def one(sql: str, params: tuple | list = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def run(sql: str, params: tuple | list = ()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


def scalar(sql: str, params: tuple | list = ()) -> Any:
    with _lock:
        row = _conn.execute(sql, params).fetchone()
        return row[0] if row else None


def update(table: str, row_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE {table} SET {cols} WHERE id=?", [*fields.values(), row_id])


def backup_to(path) -> None:
    """Copy the live database to `path` with SQLite's online backup, so the copy is consistent even in WAL mode
    with writers active (a plain file copy could miss the WAL). Used by backup.run_backup."""
    if _conn is None:
        raise RuntimeError("The database is not open.")
    with _lock:
        dest = sqlite3.connect(str(path))
        try:
            _conn.backup(dest)
        finally:
            dest.close()


# ---------- settings ----------

def get_settings() -> dict:
    s = dict(config.DEFAULT_SETTINGS)
    s["policies"] = dict(config.DEFAULT_POLICIES)
    for r in q("SELECT key, value FROM settings"):
        try:
            s[r["key"]] = json.loads(r["value"])
        except (ValueError, TypeError):
            s[r["key"]] = r["value"]
    pol = dict(config.DEFAULT_POLICIES)
    pol.update(s.get("policies") or {})
    for k in config.LOCKED_POLICIES:
        pol[k] = "approve"
    s["policies"] = pol
    return _resolve_secrets(s)


_secret_warned: set[str] = set()   # vault names whose read failure was already printed


def _resolve_secrets(s: dict) -> dict:
    """Phase 1: a secret is stored as "vault:<name>"; readers get the value from the vault ("" when missing).
    The result holds real secrets: never write it back to a table (update_company_settings does that safely)."""
    names = [k for k, v in s.items() if isinstance(v, str) and v.startswith("vault:")]
    if names:
        from . import vault   # imported here: vault imports db
        for k in names:
            name = s[k][len("vault:"):]
            try:
                s[k] = vault.get(name) or ""
            except Exception as e:   # a foreign-machine secret: treat as unset, the Doctor's vault row says why
                if name not in _secret_warned:   # once per secret: settings are read many times a minute
                    _secret_warned.add(name)
                    print(f"Vault: {k}: {e}")
                s[k] = ""
    return s


NOTIFY_LEVELS = ("important", "all")
# Whole-number settings and the range they are clamped into (None = no upper bound). Out-of-range values are
# pulled back silently because the form cannot produce anything harmful here, only something useless.
SETTING_RANGES = {
    "max_task_attempts": (1, 5),
    "backup_keep": (1, 90),
    "num_ctx": (2048, None),
    "max_concurrent_runs": (1, 8),
}
_CHAT_ID_RE = re.compile(r"^-?\d+$")   # a Telegram chat id is a number; groups and channels are negative


def validate_settings(values: dict) -> dict:
    """Normalise the settings being saved and refuse the ones that would fail quietly later.

    Numbers in SETTING_RANGES are clamped. A Telegram chat id is stripped and must be digits (with an optional
    leading minus); notify_level must be "important" or "all". Those two raise ValueError, which the API turns
    into a 400, because a wrong chat id makes every send fail with "chat not found" and an unknown level
    would silently drop escalations.
    """
    out = {}
    for k, v in values.items():
        if k in SETTING_RANGES:
            try:
                n = v if isinstance(v, int) else int(float(str(v).strip()))
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a whole number") from None
            low, high = SETTING_RANGES[k]
            v = max(low, n if high is None else min(n, high))
        elif k == "notify_level":
            v = str(v or "").strip().lower()
            if v not in NOTIFY_LEVELS:
                raise ValueError("notify_level must be 'important' or 'all'")
        elif k == "telegram_chat_id":
            v = str(v or "").strip()
            if v and not _CHAT_ID_RE.match(v):
                raise ValueError("The Telegram chat id is a number (for example 123456789, or -100123456789 "
                                 "for a group). Message your bot, press Send test message, and copy it from there.")
        out[k] = v
    return out


def set_settings(values: dict) -> dict:
    values = validate_settings(values)
    allowed = set(config.DEFAULT_SETTINGS) | {"policies"}
    for k, v in values.items():
        if k in allowed:
            run("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)))
    return get_settings()


# ---------- companies ----------

def company_settings(company: dict) -> dict:
    """A company's settings over the defaults, with vault markers resolved to the secrets themselves. For reading
    only: writing this back would put the secrets in the table in plain text (use update_company_settings)."""
    s = dict(config.DEFAULT_COMPANY_SETTINGS)
    try:
        s.update(json.loads(company.get("settings") or "{}"))
    except ValueError:
        pass
    return _resolve_secrets(s)


def update_company_settings(company_id: int, **changes) -> dict:
    """Merge changes into a company's stored settings JSON and write it back. The stored JSON is loaded as it is
    (vault markers stay markers, defaults are not added), so no resolved secret is ever written. A secret key among
    the changes (config.SECRET_COMPANY_SETTINGS) goes to the vault and only its marker is stored; an empty value
    deletes it. Returns the stored settings."""
    marker = "vault:"
    if any(k in config.SECRET_COMPANY_SETTINGS for k in changes):
        from . import vault   # imported here: vault imports db. Before the lock: vault.migrate locks vault, then db
        for k in config.SECRET_COMPANY_SETTINGS:
            v = changes.get(k)
            if not isinstance(v, str) or v.startswith(marker):
                continue
            name = f"company.{company_id}.{k}"
            if v:
                vault.put(name, v)
                changes[k] = marker + name
            else:
                vault.delete(name)
    with _lock:   # read, merge and write in one go, so a change made meanwhile is not lost
        row = one("SELECT settings FROM companies WHERE id=?", (company_id,))
        if not row:
            return {}
        stored = loads(row.get("settings"), {}) or {}
        if not isinstance(stored, dict):
            stored = {}
        stored.update(changes)
        run("UPDATE companies SET settings=? WHERE id=?", (json.dumps(stored), company_id))
        return stored


def effective_policy(company: dict, action: str) -> str:
    if action in config.LOCKED_POLICIES:
        return "approve"
    cs = company_settings(company)
    override = (cs.get("policies") or {}).get(action)
    if override in ("approve", "auto", "off"):
        return override
    return get_settings()["policies"].get(action, "approve")


def get_company(cid: int) -> dict | None:
    return one("SELECT * FROM companies WHERE id=?", (cid,))


def get_agent(aid: int) -> dict | None:
    return one("SELECT * FROM agents WHERE id=?", (aid,))


def get_task(tid: int) -> dict | None:
    return one("SELECT * FROM tasks WHERE id=?", (tid,))


def loads(s: str | None, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default
