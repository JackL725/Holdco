"""Activity feed: every event is stored and pushed live to open dashboards (Server-Sent Events)."""
from __future__ import annotations

import json
import queue
import threading

from . import db

_subs: set[queue.Queue] = set()
_lock = threading.Lock()


def subscribe() -> queue.Queue:
    qu: queue.Queue = queue.Queue(maxsize=2000)
    with _lock:
        _subs.add(qu)
    return qu


def unsubscribe(qu: queue.Queue) -> None:
    with _lock:
        _subs.discard(qu)


def publish(payload: dict) -> None:
    with _lock:
        subs = list(_subs)
    for qu in subs:
        try:
            qu.put_nowait(payload)
        except queue.Full:
            pass


def emit(company_id: int | None, kind: str, message: str, agent_id: int | None = None,
         data: dict | None = None) -> dict:
    """Log an activity-feed event and push it to the dashboard."""
    created = db.now()
    eid = db.run(
        "INSERT INTO events(company_id, agent_id, kind, message, data, created_at) VALUES(?,?,?,?,?,?)",
        (company_id, agent_id, kind, message, json.dumps(data or {}), created),
    )
    ev = {"type": "event", "id": eid, "company_id": company_id, "agent_id": agent_id, "kind": kind,
          "message": message, "data": data or {}, "created_at": created}
    publish(ev)
    if kind == "error":
        _tell_phone(company_id, message)
    return ev


def _tell_phone(company_id: int | None, message: str) -> None:
    """Phase 0: errors reach the owner's phone (notify rate-limits them to one per distinct message per hour)."""
    try:
        from . import notify   # imported here: notify imports bus
        notify.error(db.get_company(company_id) if company_id else None, message)
    except Exception:
        pass   # the phone must never break the activity feed


def notify(kind: str, **data) -> None:
    """Push a UI refresh hint without logging it."""
    publish({"type": kind, **data})
