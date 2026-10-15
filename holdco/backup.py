"""Nightly backups of the data folder (Phase 0). Keeps the newest `backup_keep` zip files.

A backup is one zip: holdco.db (copied with sqlite3's online backup API, so it is consistent even while agents
write), companies/ (without run logs, trash and build junk) and the vault files when they exist. Restore is
manual: stop Holdco, unzip the archive over the data folder, start Holdco again.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import Path

from . import bus, config, db
from .routing import route

BACKUP_DIR: Path = config.DATA_DIR / "backups"
SKIP_DIRS = {".runs", ".trash", "__pycache__", "node_modules"}
EXTRA_FILES = ("vault.json", "vault.key")      # Phase 1 secrets store; skipped when absent
BACKUP_EVERY_HOURS = 24
CHECK_EVERY_SECONDS = 600                     # how often maybe_run looks at the newest backup's age
_NAME_RE = re.compile(r"^holdco-(\d{8}-\d{6})(?:-(\d+))?\.zip$")

_run_lock = threading.Lock()                  # two backups never write at the same time
_thread: threading.Thread | None = None
_last_check = 0.0


def _stamp_to_iso(stamp: str) -> str:
    return _dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")


def _sort_key(p: Path) -> tuple:
    m = _NAME_RE.match(p.name)
    if not m:
        return ("", 0, p.name)
    return (m.group(1), int(m.group(2) or 0), p.name)


def _info(p: Path) -> dict:
    m = _NAME_RE.match(p.name)
    try:
        st = p.stat()
    except OSError:
        return {"path": str(p), "name": p.name, "created_at": None, "size": 0}
    if m:
        created = _stamp_to_iso(m.group(1))
    else:
        created = _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"path": str(p), "name": p.name, "created_at": created, "size": st.st_size}


def list_backups() -> list[dict]:
    """Every finished backup, newest first."""
    if not BACKUP_DIR.exists():
        return []
    files = [p for p in BACKUP_DIR.glob("holdco-*.zip") if p.is_file()]
    return [_info(p) for p in sorted(files, key=_sort_key, reverse=True)]


def last_backup() -> dict | None:
    """{"path", "created_at", "size"} of the newest backup, or None."""
    rows = list_backups()
    return rows[0] if rows else None


def _company_files():
    """(absolute path, zip name) for every file worth keeping under companies/."""
    root = config.COMPANIES_DIR
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            yield p, "companies/" + p.relative_to(root).as_posix()


def _fresh_target(stamp: str) -> Path:
    """holdco-<stamp>.zip, or holdco-<stamp>-N.zip when backups land inside one second (an eager owner, tests).
    N continues from the highest suffix present so the newest file always sorts last, even after pruning."""
    taken = []
    for p in BACKUP_DIR.glob(f"holdco-{stamp}*.zip"):
        m = _NAME_RE.match(p.name)
        if m and m.group(1) == stamp:
            taken.append(int(m.group(2) or 0))
    if not taken:
        return BACKUP_DIR / f"holdco-{stamp}.zip"
    return BACKUP_DIR / f"holdco-{stamp}-{max(taken) + 1}.zip"


def prune(keep: int | None = None) -> list[Path]:
    """Delete all but the newest `keep` backups. Returns the deleted paths."""
    if keep is None:
        keep = int(db.get_settings().get("backup_keep") or 14)
    keep = max(1, keep)   # never delete the backup that was just written
    gone = []
    for row in list_backups()[keep:]:
        p = Path(row["path"])
        try:
            p.unlink()
            gone.append(p)
        except OSError:
            pass
    return gone


def run_backup() -> Path:
    """Write data/backups/holdco-YYYYMMDD-HHMMSS.zip (consistent DB copy + company folders) and prune old ones."""
    with _run_lock:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        final = _fresh_target(stamp)
        part = final.with_name(final.name + ".part")   # a half-written file never counts as a backup
        with tempfile.TemporaryDirectory(prefix="holdco-backup-") as td:
            tmp_db = Path(td) / "holdco.db"
            db.backup_to(tmp_db)
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(tmp_db, "holdco.db")
                for p, arc in _company_files():
                    try:
                        z.write(p, arc)
                    except OSError:
                        pass   # a file an agent is replacing right now: the next backup gets it
                for name in EXTRA_FILES:
                    p = config.DATA_DIR / name
                    if p.is_file():
                        z.write(p, name)
        part.replace(final)
        prune()
    size = final.stat().st_size
    bus.emit(None, "info", f"Backup written: {final.name} ({size / 1e6:.1f} MB)", None, {"path": str(final)})
    return final


def _run_safely() -> None:
    try:
        run_backup()
    except Exception as e:   # reaches the feed (and the phone, once notify is wired to error events)
        traceback.print_exc()
        bus.emit(None, "error", f"Backup failed: {e}")


def maybe_run() -> None:
    """Scheduler hook: run a backup in a thread when the last one is older than 24 hours."""
    global _thread, _last_check
    now = time.time()
    if _thread is not None and _thread.is_alive():
        return
    if now - _last_check < CHECK_EVERY_SECONDS:
        return
    _last_check = now
    last = last_backup()
    if last and db.minutes_since(last.get("created_at")) < BACKUP_EVERY_HOURS * 60:
        return
    _thread = threading.Thread(target=_run_safely, daemon=True, name="backup")
    _thread.start()


@route("POST", "/api/backup")
def api_backup(h, m, body):
    p = run_backup()
    return _info(p)


@route("GET", "/api/backups")
def api_backups(h, m, body):
    folder = str(BACKUP_DIR)
    return {"backups": list_backups(), "dir": folder, "folder": folder,
            "keep": int(db.get_settings().get("backup_keep") or 14)}
