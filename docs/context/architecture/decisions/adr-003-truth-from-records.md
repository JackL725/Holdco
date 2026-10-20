# ADR-003: What agents are told comes from Holdco's records, not from agents

Status: Accepted | Date: 2026-10-20 (Phase 0.5, after the first real company)

## Context
The first real company (Rooforyou) fabricated evidence: files claimed a deployed site, sent emails and signups that
never happened, and later sessions believed those files. Agents also asked the owner the same question many times
and re-planned while nothing changed.

## Decision
- Every prompt carries a facts block built from the database (site deployed or not, emails really sent, accounts,
  what waits on the owner, what is down), and `records/` in each company folder holds the same history, read-only
  to agents.
- The claims gate refuses a file, a message or a task handoff that states a deploy, an email, a signup or a payment
  the records do not show, and says what really happened. Action tasks (outreach, deploy, post) are done only when
  the action happened.
- Numbers carry a `source`. Briefs may use only numbers from the data block code gathered; the weekly review scores
  goals only with connector numbers; the portfolio's "days since result" counts only connector changes, replies
  and payments.
- One open question per topic, a cap of three questions from agents, and change-driven planning.

## Alternatives considered
- Trusting a reviewer agent to catch fabrications: reviewers of the same model share its blind spots.
- Stricter prompts alone: tried first; the model kept fabricating under pressure to finish.

## Consequences
- New features that report numbers must say where they come from and must not let manual (agent-typed) rows score.
- Guards must say what to do instead and refuse once; see ADR-002.
