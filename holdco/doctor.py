"""Doctor: health checks for everything Holdco depends on (Phase 0).

Each check returns a row {key, label, status: ok|warn|bad, detail, fix, checked_at}. The scheduler refreshes
the rows hourly, the Doctor page shows them, and founding runs the preflight subset first.
"""
from __future__ import annotations

import threading

from . import db
from .routing import route

LAST: list[dict] = []          # most recent rows, newest run
_lock = threading.Lock()


def run(deep: bool = False) -> list[dict]:
    """Run every check and cache the rows. deep=True also runs slow checks (model tool-call ping, live search)."""
    raise NotImplementedError("Phase 0: doctor.run")


def preflight() -> dict:
    """Checks that must pass before founding a company. Returns {"ok": bool, "checks": [rows], "soft": [rows]}."""
    raise NotImplementedError("Phase 0: doctor.preflight")


def summary(rows: list[dict] | None = None) -> dict:
    rows = LAST if rows is None else rows
    return {"ok": sum(r["status"] == "ok" for r in rows), "warn": sum(r["status"] == "warn" for r in rows),
            "bad": sum(r["status"] == "bad" for r in rows), "checked_at": rows[0]["checked_at"] if rows else None}


def refresh_if_stale(max_age_minutes: float = 60) -> None:
    """Called by the scheduler every tick; reruns the checks when the cache is older than max_age_minutes."""
    raise NotImplementedError("Phase 0: doctor.refresh_if_stale")


@route("GET", "/api/doctor")
def api_doctor(h, m, body):
    return {"checks": LAST, "summary": summary()}


@route("POST", "/api/doctor/run")
def api_doctor_run(h, m, body):
    rows = run(deep=bool((body or {}).get("deep")))
    return {"checks": rows, "summary": summary(rows)}
