# ADR-005: Tests use fakes, never the network or a real model; real models are checked in sandboxes

Status: Accepted | Date: 2026-10-20

## Context
Holdco talks to a model server, Telegram, Stripe, Resend, MCP servers, OAuth servers, search engines, Porkbun,
Cloudflare, GitHub, analytics, forms and n8n. CI must be fast, free and deterministic on Windows and Linux, and the
owner's running Holdco and data must never be touched by a test.

## Decision
- Every outside service has a fake: `tests/fake_*.py` servers (ThreadingHTTPServer on 127.0.0.1), and from Phase 3
  `holdco/bench/mocks.py` as the one home for the fakes the Bench shares (Stripe, Telegram, MCP, search, fetch).
  The scripted model is `tests/fake_ollama.py`.
- `tests/__init__.py` gives in-process tests a free port of their own and turns the sandbox off; end-to-end tests
  start a real Holdco subprocess (`tests/helpers.HoldcoProcess`) with its own data folder. The whole suite passes
  with no network at all.
- Each phase ends with a real-model check: a sandbox Holdco on a spare port (8431-8433) with a scratch data folder
  and the owner's GPU. What it finds becomes a replay test with the scripted model.

## Alternatives considered
- Recorded HTTP cassettes: brittle and still need a first real run with real accounts.
- Running against vendor test modes in CI: needs secrets in CI and the network; flaky.

## Consequences
- A connector or tool is not done without its fake and tests through it.
- Vendor endpoint details that could not be checked against live documentation are named constants marked
  "verify" (listed in the Phase 4 pull request).
- Never point a test or a bench at port 8420 or at `data/`; never read or edit the owner's data folder.
