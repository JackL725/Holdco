"""The code sandbox (Phase 3, docs/phases/phase-3-verify.md section 4): run_code inside a Docker or Podman container
with no network, a memory and process cap and a read-only root, the company folder mounted at /work.

Scaffold: no runtime is detected, so run_code keeps today's subprocess path.
"""
from __future__ import annotations


def available() -> str | None:
    """'docker' or 'podman' when the binary exists and answers `info` (cached), else None."""
    return None


def run(company: dict, language: str, code: str, timeout: int = 60, network: bool = False) -> str | None:
    """The run_code result from inside the container, or None to fall back to the unsandboxed path."""
    return None
