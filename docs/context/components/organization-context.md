# Organization: brain, contracts, rhythms, decisions, portfolio

## Purpose
Turns a company into a managed organization: one structured source of truth (the brain), measurable goals and
experiments, tasks as contracts with dependencies and budgets, daily and weekly rhythms that produce briefs from
real numbers, a decisions queue for the owner, and (Phase 4) a portfolio that funds what works.

## Key files
- `holdco/brain.py` (Phase 2): tables strategy, goals (at most 3 active), contacts, decisions, experiments,
  lessons, brain_changes; `digest()` (at most 6,000 characters, every number with its source),
  `company_section()`, `active_goals()`/`goal_line()`; tools `brain_lookup`, `propose_brain_change` (price, offer,
  positioning or ICP changes become owner decisions), `add_contact`/`update_contact` (unsubscribe is final and
  suppresses), `add_lesson`, `set_strategy`, `set_goal`, `drop_goal`, `log_decision`, `start_experiment`,
  `conclude_experiment`, `review_brain_change`; the Brain tab routes.
- `holdco/contracts.py` (Phase 2): the task contract (`goal_id`, `expected`, `inputs`, `depends_on`, `due_at`,
  `checks`; budgets set by the owner only), `validate()` (shared by tools and routes), dependency graph and cycle
  check, `cycle_sections()` (goals, experiments, proposals, decisions due), budgets (`gpu_hours`, `over_gpu_budget`,
  `max_steps_for`, token budget, `post_budget_problem`), `rhythm_task_problem()`.
- `holdco/rhythms.py` (Phase 2): rhythm keys (morning plan, evening brief, weekly review, monthly check), data
  gatherers (`daily_data`, `weekly_data`, ...), `task_data_section()`, `write_brief` (numbers must be in the data),
  `after_weekly()` (cancels agent tasks that serve no goal), the daily portfolio brief (in a thread), brief routes.
- `holdco/decisions.py` (Phase 2): `create()` (options, recommendation, deadline, reversible, `origin`),
  `ask_decision` tool, `on_decided()` (writes the brain's decisions row, applies the origin: brain changes, goals,
  trust promotions, the monthly check, the portfolio review), `resolve_due()` (reversible decisions take the
  recommendation at the deadline; irreversible ones wait and are re-announced daily).
- `holdco/portfolio.py` (Phase 4): `scorecard()` (goal progress, revenue, MRR, GPU hours, money spent, days since a
  real-world result, age, score, share), `WEIGHTS`, seed floor and Scale boost, `order()` (deficit ordering inside
  dispatch classes 3 to 6, starvation guard, `allocation_mode`), `monthly_review_if_due()`, `apply_answer()`,
  `GET /api/portfolio`.
- File leases (`workspace.lease/release`, Phase 2) keep two runs from writing the same file.

## Dependencies
- `db`, `agents` (prompts), `scheduler` (routines, dispatch, housekeeping), `actions` (decisions are approvals),
  `notify` (briefs and decisions on the phone), `connectors` (metrics with sources), `planning` (question rules).

## Integration points
- Prompts: `brain.company_section`, `contracts.cycle_sections`, `contracts.task_contract_section`,
  `rhythms.task_data_section`.
- Scheduler: `_routines` (at_hour, `no_analyst`), `contracts.dependency_status/dispatchable`, `portfolio.order`,
  housekeeping (`decisions.resolve_due`, `rhythms.portfolio_brief_in_background`, `portfolio.monthly_review_if_due`).
- `actions.decide` hands answered decisions to `decisions.on_decided`.

## Patterns and rules
- Metrics with `source = 'manual'` (typed by an agent) never score: weekly review, goal current values for scoring,
  portfolio results (ADR-003).
- Tasks created by the owner (`config.OWNER_CREATORS`, including "owner (mcp)"), routines and inbox replies are
  never cancelled for having no goal.
- Dates in these modules are UTC calendar dates; routine hours use the PC's local time.
