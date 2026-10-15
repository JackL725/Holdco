"""Business archetype playbooks (Phase 0.5, design section 4.3)."""
from __future__ import annotations

PLAYBOOKS: dict[str, dict] = {}


def classify(text: str) -> str:
    """The archetype key for an idea or charter, or '' when unsure. Scaffold: unknown."""
    return ""


def section(company: dict) -> str:
    """'# Playbook' prompt section for the company's archetype. Scaffold: none."""
    return ""
