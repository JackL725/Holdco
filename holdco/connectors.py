"""Connector framework (Phase 1, docs/phases/phase-1-connect.md sections 4 to 6).

Definitions from the conn_*.py modules, instances in the connectors table, setup cards, health checks, metric
pipes, the agents' prompt section, the catalog behind find_tool, and routing of loaded connector tools through the
risk gate. Scaffold: no definitions; every seam answers "nothing connected".
"""
from __future__ import annotations

DEFINITIONS: dict[str, dict] = {}

# A connector tool's risk tag decides which approval policy applies (read runs freely).
POLICY_OF_TAG = {"write": "connector_write", "publish": "connector_publish", "contact": "connector_contact",
                 "spend": "spend_money", "irreversible": "irreversible"}


def policy_for(tag: str) -> str | None:
    return None if tag == "read" else POLICY_OF_TAG.get(tag, "connector_publish")


def instance(key: str, company: dict | None) -> dict | None:
    return None


def secrets(inst: dict) -> dict:
    return {}


def check_if_stale() -> None:
    return None


def poll_if_due() -> None:
    return None


def down_for(company: dict) -> list[str]:
    return []


def prompt_section(company: dict) -> str:
    """'# Connected services' for the system prompt ("" when nothing is connected)."""
    return ""


def doctor_rows() -> list[dict]:
    """Doctor rows connector:<id> from the last health checks (no network)."""
    return []


def call(ctx, name: str, args: dict) -> str:
    """A loaded connector tool was called: read runs now, anything else goes through actions.gate."""
    raise NotImplementedError("Phase 1: connectors.call")


def execute(payload: dict) -> str:
    """actions.execute for action 'connector_call' (after approval or an auto policy)."""
    raise NotImplementedError("Phase 1: connectors.execute")
