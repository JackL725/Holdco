"""MCP client, standard library only (Phase 1, docs/phases/phase-1-connect.md section 2).

Speaks the 2026-07-28 stateless generation (server/discover, namespaced _meta keys, Mcp-Method/Mcp-Name headers)
and falls back to the 2025-11-25 initialize handshake, over stdio or Streamable HTTP. Scaffold only.
"""
from __future__ import annotations


class MCPError(Exception):
    pass


class AuthRequired(MCPError):
    def __init__(self, resource_metadata_url: str):
        super().__init__(f"authorization required ({resource_metadata_url})")
        self.resource_metadata_url = resource_metadata_url


class Client:
    def __init__(self, name: str, transport: str, command: str | None = None, args=(), env: dict | None = None,
                 url: str | None = None, headers: dict | None = None, auth=None):
        raise NotImplementedError("Phase 1: mcp.Client")


def text_of(result: dict) -> str:
    raise NotImplementedError("Phase 1: mcp.text_of")
