# ADR-004: Every outbound action goes through one gate, and money and irreversible actions always ask

Status: Accepted | Date: 2026-10-20

## Context
Agents can email people, post publicly, deploy sites, call outside services, run code and, since Phase 4, buy a
domain. The owner must stay in control from any surface (dashboard, phone, MCP client) without reviewing
everything forever.

## Decision
- Tools never perform outbound actions themselves; they call `actions.gate(...)`. The gate applies, in order:
  locked actions (spending money and anything tagged irreversible always ask), the policy `off`, then
  `trust.gate_policy` (a session that read outside content asks; legal wording and a first email to a new person
  ask; trust level 2 runs; level 1 runs under a daily cap; otherwise the owner's policy), then the kill switch.
- Every tool carries a risk tag (read, write, publish, contact, spend, irreversible); connector tools follow the
  policy of their tag; unknown tools ask.
- Every surface decides through `actions.decide`, which claims the row atomically (a request runs at most once),
  executes, writes the audit row with who approved it (`owner`, `policy:auto`, `trust:<level>`, `mcp:<client>`) and
  updates the trust streak.
- Holdco never deletes a company on its own (it pauses), and never pays without approval of that exact purchase.

## Alternatives considered
- Per-tool approval logic: grew inconsistent quickly in early versions.
- Fully automatic agents with a spending cap: rejected; a mistaken email or post cannot be recalled.

## Consequences
- A new outbound capability is a tool plus a gate action (or a connector tool with a risk tag), plus a test that it
  queues under the default policy, is refused under `off`, and asks when the session is tainted.
- The kill switch stops every agent at its next step and freezes the gate; questions and decisions can still be
  answered.
