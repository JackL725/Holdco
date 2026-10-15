"""Phone notifications (Phase 0): a Telegram bot with inline buttons, and ntfy as a notify-only option.

Long polling (getUpdates) means nothing on this PC is exposed to the internet.
"""
from __future__ import annotations

import threading

from . import db
from .routing import route


def configured(s: dict | None = None) -> bool:
    s = s or db.get_settings()
    return bool((s.get("telegram_bot_token") and s.get("telegram_chat_id")) or s.get("ntfy_topic"))


def send(text: str, buttons: list[list[dict]] | None = None, kind: str = "info") -> bool:
    """Send one message to every configured channel. Identical text is not resent within an hour."""
    raise NotImplementedError("Phase 0: notify.send")


def approval_requested(approval: dict) -> None:
    """Hook from actions.gate when a request is queued: message with Approve/Reject or option buttons."""
    raise NotImplementedError("Phase 0: notify.approval_requested")


def error(company: dict | None, message: str) -> None:
    """Hook for error events: one message per distinct error per hour."""
    raise NotImplementedError("Phase 0: notify.error")


def ensure_poller() -> None:
    """Start (or keep alive) the Telegram long-polling thread when a token and chat id are set."""
    raise NotImplementedError("Phase 0: notify.ensure_poller")


@route("POST", "/api/notify/test")
def api_notify_test(h, m, body):
    raise NotImplementedError("Phase 0: notify.api_notify_test")
