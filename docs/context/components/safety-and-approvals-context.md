# Safety and approvals

## Purpose
Keeps the owner in control of everything that leaves the PC, makes "done" mean checked, contains code and secrets,
and stops outside content from steering agents.

## Key files
- `holdco/actions.py`: `gate()` (the one path for outbound actions; see ADR-004 for the order), `decide()` (owner,
  phone, MCP and deadline answers; atomic claim of the row; execute, audit, trust streak, reopen the waiting task
  with `followup_at`), `execute()` (email, posts, deploys with Netlify or Cloudflare, `http_post`, `run_code`,
  connector calls), `audit()`, `ACTION_LABELS`, `TAG_OF_ACTION`.
- `holdco/trust.py` (Phase 3): `gate_policy()` (taint, `LEGAL_RE`, first contact, levels 0/1/2, daily caps),
  `after_decision()` (clean approvals build a streak; an edit resets it; a rejection demotes), promotion decisions
  at a streak of 15, `apply_promotion()`, `GET/PUT /api/companies/<id>/trust`.
- `holdco/taint.py` (Phase 3): `current(ctx)` (context variable for the tool running now), `current_sources()`,
  `after_tool()` (marks the session tainted after web search, fetched pages, inbound email, external browser pages,
  connector tools marked `external`; fences the content between `--- untrusted content from <source> ---` lines).
- `holdco/verify.py` (Phase 3): `finish_problem()` (the gate by kind: web page with `browser_check` or a labelled
  static check, code tests in the sandbox or through the `run_code` approval, Stripe payment link, outreach rules,
  research citations against the `fetched` table, `task.checks` lines), results in `tasks.verification`,
  `summary_line()` for the CEO's planning prompt, `outreach_check()` (suppression, EU consent, daily cap,
  `review_text` hard issues), `suppress()`/`is_suppressed()` (the global `suppression` table).
- `holdco/sandbox.py` (Phase 3): Docker or Podman detection (cached), `command()` (`--network none` unless allowed,
  memory, CPU and process caps, read-only root, company folder at `/work`), `run()`; `HOLDCO_SANDBOX=off`.
- `holdco/vault.py` (Phase 1): secrets in Windows DPAPI, the Linux keyring (`secret-tool`) or an encrypted file;
  `vault:<name>` markers in settings; `redact()` for events, transcripts, the audit log and `records/`.
- `holdco/config.py`: `POLICY_ACTIONS`, `LOCKED_POLICIES`, `RISK_TAGS`, `SECRET_SETTINGS`.

## Dependencies
- `db`, `workspace`, `notify` (requests on the phone), `checks`, `browser`, `conn_stripe`, `decisions` (promotion
  and other owner decisions).

## Integration points
- Every outbound tool and `connectors.call` reaches `actions.gate`; `tools.execute` wraps every tool in
  `taint.current` and `taint.after_tool`; `tools.finish` calls `verify.finish_problem`; `tools.send_email` calls
  `truth.outreach_guard` then `verify.outreach_check`.
- `actions.execute("deploy_site")` asks `conn_cloudflare.deploy_if_selected` first, then Netlify.

## Patterns and rules
- Locked actions (spend, irreversible) always ask; nothing promotes them. Porkbun purchases are spend plus
  irreversible, re-check the price at execution, honour the dry run and sandbox mode the approved card showed, and
  send an idempotency key so a retry never buys twice.
- A tainted session's outward actions always ask, whatever the policy or trust level; the payload carries `_taint`
  and `_why_asked` for the Inbox card.
- The kill switch freezes execution, not answers: questions and decisions can still be answered.
- Without a sandbox, `run_code` runs on the PC and says so; approved-as-sandboxed code never falls back.
