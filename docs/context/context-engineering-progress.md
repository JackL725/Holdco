# Context engineering progress

Workflow state for the AI-collaboration documentation in `docs/context/`. A new session continues with: "Continue
context engineering: read `docs/context/context-engineering-progress.md`, then help me with <task>."

## Method
- Goal: a small, accurate memory system so an AI collaborator understands Holdco without re-reading 36,000 lines.
- Three phases at most: (1) discovery and plan, (2) core documents, (3) integration into the workflow.
- Write from the code, not from memory: every module, function and table named in these files was checked to exist
  when written. Link to the design documents in `docs/` and the phase specs instead of copying them.
- Templates: the ADR shape (Status and date, Context, Decision, Alternatives considered, Consequences); component
  files (Purpose, Key files, Dependencies, Integration points, Patterns and rules). File names:
  `adr-NNN-title.md`, `<component>-context.md`.
- Scope: start minimal; add documents only when a gap shows up in real use.

## Project specifics
- Holdco: stdlib-only Python 3.10+ app (one process: HTTP server, scheduler, Telegram poller, agent-run threads;
  SQLite; plain-JS dashboard) running AI-operated companies on a local Ollama model. Repo JackL725/Holdco.
- Built in phases 0, 0.5, 1, 2, 3, 4 (`docs/phases/`). Written against branch `claude/phase-4-scale` (aed2a91),
  i.e. `main` plus Phase 4 (pull request JackL725/Holdco#8).

## Status
| Phase | State | Output |
|---|---|---|
| 1 Discovery and plan | done 2026-10-20 | module survey (58 modules, sizes, docstrings), docs inventory, structure below |
| 2 Core documents | done 2026-10-20 | `project-overview.md`, `architecture/system-design.md`, 6 ADRs, 7 component files, `workflows/development.md` |
| 3 Integration | done 2026-10-20 | `CLAUDE.md` at the repo root points Claude Code here; `docs/README.md` links this folder; the development workflow says when to update these files |

## Files
```
CLAUDE.md                                   short pointer and hard constraints (read by Claude Code)
docs/context/
  project-overview.md                       master index: navigation, essentials, conventions, glossary
  context-engineering-progress.md           this file
  architecture/system-design.md             process model, tick, agent session, outbound path, data, extension points
  architecture/decisions/adr-001..006       stdlib only; local model first; truth from records; one gate for
                                            outbound; fakes not network; phases, specs and parallel builders
  components/                               agent-runtime, ground-truth-and-context, organization,
                                            safety-and-approvals, integrations, operations-and-dashboard, bench
  workflows/development.md                  run, test (Windows rules), real-model checks, change, ship
```

## Keeping it current
- When a module gains or loses a responsibility, an extension point changes, or a rule in "AI collaboration notes"
  changes, update the component file and, if needed, `project-overview.md` in the same pull request.
- A new settled decision that people may want to re-debate gets an ADR (next number: 007).
- After Phase 4 merges, change "Current state" in `project-overview.md` and the branch line above.

## Next steps (only if wanted)
- ADRs for smaller settled choices if they get re-debated (for example: the CEO belt of 24 tools, owner-only budgets,
  the stdio proxy instead of a second database writer).
- A short "how to debug a stuck company" page in `workflows/` built from the watchdog patterns and Doctor rows.
