"""Each company gets its own folder on disk. Agents can't read or write outside it."""
from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import config, db

SKIP = {".trash", ".runs", ".cache", ".qa", "__pycache__", "node_modules", ".git"}   # .cache: fetched pages (truth.py)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "company"


def root(company: dict) -> Path:
    return config.COMPANIES_DIR / company["slug"]


def create(company: dict) -> Path:
    r = root(company)
    for d in config.WORKSPACE_DIRS:
        (r / d).mkdir(parents=True, exist_ok=True)
    return r


class PathError(Exception):
    pass


def safe_path(company: dict, rel: str) -> Path:
    base = root(company).resolve()
    rel = (rel or ".").strip().replace("\\", "/")
    rel = re.sub(r"^(\./)+", "", rel).lstrip("/")
    if re.match(r"^[a-zA-Z]:", rel):
        raise PathError("Use paths relative to the company folder, e.g. site/index.html")
    p = (base / rel).resolve()
    if p != base and base not in p.parents:
        raise PathError("That path is outside the company folder.")
    return p


def rel(company: dict, p: Path) -> str:
    base = root(company).resolve()
    try:
        return p.resolve().relative_to(base).as_posix()
    except ValueError:
        # Windows: resolving a file that vanished a moment ago (an atomic rewrite's temp file, for example) can
        # return a \\?\ path that no longer matches the folder; the plain absolute path still does.
        return Path(os.path.abspath(p)).relative_to(Path(os.path.abspath(base))).as_posix()


def tree(company: dict, sub: str = ".", depth: int = 3, limit: int = 200) -> list[dict]:
    start = safe_path(company, sub)
    out: list[dict] = []
    if not start.exists():
        return out

    def walk(d: Path, level: int):
        if len(out) >= limit or level > depth:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.name in SKIP:
                continue
            if len(out) >= limit:
                return
            try:
                st = e.stat()
                path = rel(company, e)
            except (OSError, ValueError):   # gone or renamed while we looked (records are rewritten atomically)
                continue
            out.append({"path": path, "dir": e.is_dir(), "size": st.st_size if e.is_file() else 0,
                        "modified": st.st_mtime})
            if e.is_dir():
                walk(e, level + 1)

    walk(start, 1)
    return out


def tree_text(company: dict, limit: int = 60) -> str:
    items = [i for i in tree(company, ".", 3, 400) if not i["dir"]]
    if not items:
        return "(no files yet)"
    items.sort(key=lambda i: -i["modified"])
    lines = [f"- {i['path']} ({i['size']} bytes)" for i in items[:limit]]
    if len(items) > limit:
        lines.append(f"- ... and {len(items) - limit} more")
    return "\n".join(lines)


def trash(company: dict, p: Path) -> str:
    tdir = root(company) / ".trash" / time.strftime("%Y%m%d-%H%M%S")
    tdir.mkdir(parents=True, exist_ok=True)
    dest = tdir / p.name
    shutil.move(str(p), str(dest))
    return rel(company, dest)


def remove_company_folder(company: dict) -> None:
    src = root(company)
    if src.exists():
        config.TRASH_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(config.TRASH_DIR / f"{company['slug']}-{int(time.time())}"))


# ---------------- Phase 2: file leases (two agents never edit the same file at once)

LEASE_MINUTES = 30
_lease_lock = threading.Lock()


def _lease_key(rel_path: str) -> str:
    # Windows paths are case-insensitive: Site/index.html and site/index.html are one file.
    return rel_path.lower() if os.name == "nt" else rel_path


def _lease_live(row: dict, now: str) -> bool:
    """A lease holds until it expires, unless its run already ended (a run that crashed before releasing it)."""
    if (row.get("expires_at") or "") <= now:
        return False
    run = db.one("SELECT status FROM runs WHERE id=?", (row["run_id"],)) if row.get("run_id") else None
    return not (run and run["status"] != "running")


def lease(company: dict, rel_path: str, ctx) -> str | None:
    """Take or renew this run's lease on a file before writing it; the refusal text when another live run holds it.

    No lease, this run's own lease, or one that expired (or whose run ended): take it for LEASE_MINUTES and go on.
    A context without a run (run_id 0: tests and tools called outside a session) takes no lease, because nothing
    would release it, but it still respects the leases of real runs. Chat, cycle and founding runs have no task;
    their leases carry none."""
    run_id = int(getattr(ctx, "run_id", 0) or 0)
    task = getattr(ctx, "task", None) or {}
    agent = getattr(ctx, "agent", None) or {}
    key = _lease_key(rel_path)
    now_dt = _dt.datetime.now(_dt.timezone.utc)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lease_lock:
        row = db.one("SELECT * FROM leases WHERE company_id=? AND path=?", (company["id"], key))
        if row and row["run_id"] != run_id and _lease_live(row, now):
            holder = (db.get_agent(row["agent_id"]) or {}).get("name") if row.get("agent_id") else None
            until = (db.parse_time(row["expires_at"]) or now_dt).strftime("%H:%M")
            what = f" (task #{row['task_id']})" if row.get("task_id") else ""
            # Phase 3: the refusal says what to do. "Work on something else or wait" left agents nothing to do in
            # the session, and the real-model bench run finished 'blocked', where the task stays until someone
            # unblocks it by hand.
            return (f"ERROR: {rel_path} is being edited by {holder or 'another agent'}{what} until {until} UTC, so "
                    f"it can't be changed now. Don't wait for it: save your version in another file (for example "
                    f"drafts/{rel_path}), say in your finish where it is so it can be applied once that edit is done, "
                    f"and finish with status 'partial'.")
        if not run_id:
            return None
        expires = (now_dt + _dt.timedelta(minutes=LEASE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.run("INSERT OR REPLACE INTO leases(company_id, path, task_id, agent_id, run_id, expires_at) "
               "VALUES(?,?,?,?,?,?)", (company["id"], key, task.get("id"), agent.get("id"), run_id, expires))
    return None


def release(run_id: int) -> None:
    """Free every lease a run holds (agents._finalize)."""
    if run_id:
        db.run("DELETE FROM leases WHERE run_id=?", (int(run_id),))


def leases(company: dict) -> list[dict]:
    """The live leases of a company for the Files tab ("editing: Forge"): path, agent, task and expiry."""
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for r in db.q("SELECT l.*, a.name AS agent_name FROM leases l LEFT JOIN agents a ON a.id=l.agent_id "
                  "WHERE l.company_id=? AND l.expires_at>? ORDER BY l.path", (company["id"], now)):
        if _lease_live(r, now):
            out.append({"path": r["path"], "agent_id": r["agent_id"], "agent_name": r["agent_name"] or "",
                        "task_id": r["task_id"], "run_id": r["run_id"], "expires_at": r["expires_at"]})
    return out
