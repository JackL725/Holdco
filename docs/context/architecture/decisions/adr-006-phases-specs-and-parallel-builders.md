# ADR-006: Phased delivery from binding specs, built by parallel builders on disjoint files

Status: Accepted | Date: 2026-10-20

## Context
`docs/plan.md` defines the improvement plan; `docs/phases/` turns it into one implementation spec per phase (0, 0.5,
1, 2, 3, 4). Each phase is large (thousands of lines) and was built by several AI builders working at once.

## Decision
- One pull request per phase (or per topic), against `main`. A phase spec's Status line says planned, in progress
  or built; deviations decided during the build are recorded at the top of the spec with their reasons.
- Build order for a phase: a scaffold commit (stub modules, shared interfaces, hook calls in shared files, tables
  and settings) so builders edit disjoint files; parallel builders in git worktrees, each resetting to the scaffold
  commit; an integration pass that fixes the seams and adds end-to-end tests; a UI pass on `static/` only; an
  independent review that fixes confirmed defects with regression tests; one real-model check; then the PR.
- Every merge runs the full suite on Python 3.10, 3.12 and 3.13 before it is pushed.

## Alternatives considered
- Stacked pull requests per phase: tried for Phases 0 to 1; merging a stacked PR lands it in its base branch, not
  `main`, which needed a catch-up PR. PRs now target `main` directly.
- One builder per phase: slower, and the seams were no better.

## Consequences
- Shared files (`tools.py`, `agents.py`, `actions.py`, `scheduler.py`) change through small hooks into feature
  modules, which keeps merges clean and makes each feature findable in its own module.
- Review and integration findings are written as tests named after the defect (`tests/test_phase*_review.py`,
  `tests/test_phase*_seams.py`).
