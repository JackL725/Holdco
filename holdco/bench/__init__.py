"""Holdco Bench (Phase 3, docs/phases/phase-3-verify.md section 1): scripted scenarios that seed a company, run one
agent session against mocks and assert on what happened, scoring any model on the owner's GPU.

RUNNING is set while a bench runs: the scheduler then skips task dispatch and CEO cycles so timing is not distorted
(chat still works). Bench companies have status 'bench' and are never dispatched or listed.
"""
from __future__ import annotations

RUNNING = False
