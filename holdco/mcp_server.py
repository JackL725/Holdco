"""Holdco as an MCP server (Phase 4, docs/phases/phase-4-scale.md section 1): Streamable HTTP at /mcp on the
dashboard port and a stdio entry point (python -m holdco.mcp_server) that proxies to it. Both protocol generations,
eight tools, every call as the owner and audited.

Scaffold only.
"""
from __future__ import annotations


def handle(message: dict, client: dict | None = None) -> dict | None:
    """One JSON-RPC message in, its reply out (None for notifications). Scaffold: method not found."""
    if "id" not in message:
        return None
    return {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32601, "message": "Method not found"}}
