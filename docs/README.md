# Holdco design documentation

Start with [plan.md](plan.md), the improvement plan everything else expands. Then:

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | How a company runs today, the target closed loop, module map, data model, trust boundaries, process model |
| [operating-model.md](operating-model.md) | Company brain, goals and experiments, task contracts, rhythms, verification, budgets and watchdog, decisions |
| [connectors.md](connectors.md) | Connector framework, MCP client (both protocol generations), OAuth, risk tags, vault, every connector |
| [reliability.md](reliability.md) | Holdco Bench, constrained tool calls, working notes, retry-differently, model routing, GPU scheduling, context |
| [safety.md](safety.md) | Trust ladder, taint tracking, code sandbox, secrets, test modes, outreach compliance, kill switch, audit, backups |
| [owner-experience.md](owner-experience.md) | Daily brief, decisions vs approvals, Doctor page, phone notifications, portfolio, connector setup |
| [testing.md](testing.md) | Test strategy, the fake services, phase gates as tests, the Bench |
| [phases/](phases/) | One implementation spec per phase: [0 Stabilize](phases/phase-0-stabilize.md), [0.5 Ground truth and context](phases/phase-0.5-ground-truth-and-context.md), [1 Connect](phases/phase-1-connect.md), [2 Organize](phases/phase-2-organize.md), [3 Verify](phases/phase-3-verify.md), [4 Scale](phases/phase-4-scale.md) |

Each phase ships as its own pull request. A phase document's **Status** line says whether it is planned, in
progress or built, and its "How to try it" section is the manual check on your own PC.
