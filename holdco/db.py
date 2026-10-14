"""SQLite storage. One shared connection guarded by a lock (the app is single-user)."""
from __future__ import annotations

import datetime as _dt
import json
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
CREATE INDEX IF NOT EXISTS ix_events_company ON events(company_id, id);
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
    "tasks": {"deliverable": "TEXT DEFAULT ''", "acceptance": "TEXT DEFAULT ''", "routine": "TEXT DEFAULT ''"},
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
    return s


def set_settings(values: dict) -> dict:
    allowed = set(config.DEFAULT_SETTINGS) | {"policies"}
    for k, v in values.items():
        if k in allowed:
            run("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)))
    return get_settings()


# ---------- companies ----------

def company_settings(company: dict) -> dict:
    s = dict(config.DEFAULT_COMPANY_SETTINGS)
    try:
        s.update(json.loads(company.get("settings") or "{}"))
    except ValueError:
        pass
    return s


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
