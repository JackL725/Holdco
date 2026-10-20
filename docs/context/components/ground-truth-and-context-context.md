# Ground truth and context

## Purpose
Gives every agent the real state of its company and a way to find anything, and stops the failure modes of the
first real company: fabricated evidence, duplicate questions to the owner, idle re-planning. Also runs the staged
founding.

## Key files
- `holdco/truth.py` (Phase 0.5): `facts_section()` (the facts block), `refresh_records()` and `records_changed()`
  (the read-only `records/` folder, debounced), `changes_section()` (what changed since this agent's last session),
  `waiting_section()`, `claims_guard()` (files, chat, reports, handoffs), `finish_guard()` and action-task rules
  (`action_state`, `classify_task`), `outreach_guard()` (identity, postal address and opt-out footer added to
  commercial email; placeholder and suppressed addresses refused), `suppressed()`, `is_stop_request()`,
  `deploy_guard()`, verified research facts (`record_fact`, `check_sources`).
- `holdco/context.py` (Phase 0.5): the index (SQLite FTS5 with a LIKE fallback; optional embeddings in
  `search_vectors` via `embed_texts`), `search(ctx, query, scope)` over scopes all, files, context, memory, tasks,
  emails, chat, decisions, sessions, facts, pages; line-range reads and outlines, `edit_file`, the owner's context
  library (`data/context/`, pinned files), `memory_section`, `index_section`, `company_core`.
- `holdco/planning.py` (Phase 0.5): `ask_owner` (one open question per topic, at most three pending from agents,
  options and a recommendation), duplicate detection, `cycle_header` and the change stamp and backoff that stop
  idle cycles, the daily token budget (`over_budget`), staged founding (`FOUNDING_STAGES` research, charter, critic,
  plan; `STAGE_STEPS`; `stage_problem`; charter validation), `BOARD_ORIGINS` (decisions that are not agent questions).
- `holdco/playbooks.py`: business archetypes (lead generation, newsletter, SaaS, services, digital products,
  e-commerce, local services) with riskiest assumptions, first experiments and traps.
- `holdco/checks.py`: deterministic checks agents run on their work (`audit_page`, `review_text`,
  `check_contrast`); `plain_punct()` maps typographic punctuation before pattern checks.

## Dependencies
- `db` (all records), `workspace`, `emailer` (mailbox state), `connectors` (what is down), `brain` (digest).

## Integration points
- `agents.system_prompt` and `task_prompt`/`cycle_prompt` call the section functions.
- Tools call the guards: `write_file`/`edit_file` (claims, records write guard), `report_to_owner`, `send_email`,
  `deploy_site`, `finish`.
- `records/` is rewritten after every run start, outbound action and owner decision.

## Patterns and rules
- Facts come from tables, never from files agents wrote (ADR-003). A guard refuses once with what really happened
  and what to do instead.
- Questions are deduplicated by topic and text; decisions with a board origin (trust, portfolio) are exempt from the
  agents' question cap.
- Search results and fetched pages are outside content (taint, see safety-and-approvals); the index skips
  `records/` and binary files.
