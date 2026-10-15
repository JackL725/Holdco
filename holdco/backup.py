"""Nightly backups of the data folder (Phase 0). Keeps the newest `backup_keep` zip files."""
from __future__ import annotations

from pathlib import Path

from . import config
from .routing import route

BACKUP_DIR: Path = config.DATA_DIR / "backups"


def run_backup() -> Path:
    """Write data/backups/holdco-YYYYMMDD-HHMMSS.zip (consistent DB copy + company folders) and prune old ones."""
    raise NotImplementedError("Phase 0: backup.run_backup")


def last_backup() -> dict | None:
    """{"path", "created_at", "size"} of the newest backup, or None."""
    raise NotImplementedError("Phase 0: backup.last_backup")


def maybe_run() -> None:
    """Scheduler hook: run a backup in a thread when the last one is older than 24 hours."""
    raise NotImplementedError("Phase 0: backup.maybe_run")


@route("POST", "/api/backup")
def api_backup(h, m, body):
    raise NotImplementedError("Phase 0: backup.api_backup")


@route("GET", "/api/backups")
def api_backups(h, m, body):
    raise NotImplementedError("Phase 0: backup.api_backups")
