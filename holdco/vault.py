"""Credential vault (Phase 1, docs/phases/phase-1-connect.md section 1).

Secrets leave the plain-text settings: DPAPI on Windows, the Secret Service (secret-tool) on Linux, or a
passphrase-protected file. Settings keep a "vault:<name>" marker that db.get_settings / db.company_settings resolve.
Scaffold: no backend yet; get() finds nothing, redact() changes nothing, migrate() does nothing.
"""
from __future__ import annotations


class VaultError(Exception):
    pass


def backend() -> str:
    return "none"


def put(name: str, value: str) -> None:
    raise NotImplementedError("Phase 1: vault.put")


def get(name: str) -> str | None:
    return None


def delete(name: str) -> None:
    return None


def names() -> list[str]:
    return []


def redact(text: str) -> str:
    """Replace every known secret value (8+ characters) with [redacted]."""
    return text


def migrate() -> None:
    """Move plain-text secrets from the settings tables into the vault and leave markers."""
    return None


def doctor_rows() -> list[dict]:
    """The Doctor's 'vault' row (built with doctor._row)."""
    return []
