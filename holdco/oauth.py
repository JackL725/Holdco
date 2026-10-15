"""OAuth for hosted MCP servers (Phase 1, docs/phases/phase-1-connect.md section 3). Scaffold only."""
from __future__ import annotations


class OAuthError(Exception):
    pass


def start(connector_id: int) -> str:
    raise NotImplementedError("Phase 1: oauth.start")


def finish(query: dict) -> dict:
    raise NotImplementedError("Phase 1: oauth.finish")


def callback_page(query: dict) -> tuple[int, str]:
    """GET /oauth/callback: (status, HTML). Scaffold: not available yet."""
    return 404, "<p>OAuth sign-in is not available yet.</p>"


class TokenSource:
    def __init__(self, connector_id: int):
        raise NotImplementedError("Phase 1: oauth.TokenSource")
