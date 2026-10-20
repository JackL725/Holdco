# Holdco

Read `docs/context/project-overview.md` first: it is the map of this codebase (what each part does, where it
lives, how to test and ship), with links to one context file per subsystem and the decision records.

Hard rules (each explained in `docs/context/architecture/decisions/`):
- Python standard library only, Python 3.10 to 3.13, Windows and Linux; no new dependencies; the dashboard has no
  build step.
- Every outbound action goes through `actions.gate`; spending and irreversible actions always ask the owner.
- What agents are told comes from Holdco's records, never from agents' own claims; numbers carry a source.
- Tests use the fakes, never the network or a real model, and never the owner's `data/` folder or port 8420.
- `docs/phases/` specs are binding; record deviations in the spec. Run the full suite
  (`python -m unittest discover -s tests -t .`) before pushing.
