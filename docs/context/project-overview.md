# Holdco: context overview

This file is the master index for AI collaborators working on the Holdco codebase. Read it first, then open only the
file your task needs. It describes the code **as built** (Phases 0 to 4). The design documents in `docs/` explain why;
the phase specs in `docs/phases/` are the binding record of what each phase had to do.

## Quick navigation

| You are about to... | Read |
|---|---|
| Change how an agent session runs, a prompt, a tool, a preset | [components/agent-runtime-context.md](components/agent-runtime-context.md) |
| Touch the facts block, `records/`, claims checks, search, owner questions, founding | [components/ground-truth-and-context-context.md](components/ground-truth-and-context-context.md) |
| Touch goals, the brain, tasks and dependencies, briefs, decisions, the portfolio | [components/organization-context.md](components/organization-context.md) |
| Touch approvals, policies, trust, taint, the verification gate, the sandbox, secrets | [components/safety-and-approvals-context.md](components/safety-and-approvals-context.md) |
| Add or change a connector, the MCP client or server, email, web search, the browser | [components/integrations-context.md](components/integrations-context.md) |
| Touch the scheduler, watchdog, Doctor, phone notifications, backups, the dashboard or its API | [components/operations-and-dashboard-context.md](components/operations-and-dashboard-context.md) |
| Touch Holdco Bench, its scenarios or the fakes | [components/bench-context.md](components/bench-context.md) |
| Understand the whole system at once | [architecture/system-design.md](architecture/system-design.md) |
| Know why something is the way it is (and not re-debate it) | [architecture/decisions/](architecture/decisions/) |
| Run, test, ship a change | [workflows/development.md](workflows/development.md) |
| Find out why a company is stuck or misbehaving | [workflows/debugging.md](workflows/debugging.md) |
| Continue this documentation work | [context-engineering-progress.md](context-engineering-progress.md) |

## Project essentials

- **Purpose.** Holdco runs a portfolio of small online companies operated by AI agents on the owner's own GPU. A CEO
  agent founds each company from an idea, plans top down from measured goals, and hands tasks to a team of preset
  roles (engineering, marketing, sales, research, support...). Everything outbound (email, posts, deploys, purchases,
  code) waits for the owner's approval unless the owner's policies or the trust ladder allow it. The owner works
  from a dashboard, Telegram, or any MCP client (Claude Desktop, Claude Code).
- **Users.** One owner per installation (single-user app). Usually a Windows PC with an RTX-class GPU; Linux works.
- **Tech stack.** Python 3.10+ standard library only (no pip installs), SQLite (one shared connection, WAL), a
  plain HTML/CSS/JavaScript dashboard with no build step, Ollama or any OpenAI-compatible model server
  (default `gpt-oss:20b`). Optional: Docker or Podman (code sandbox), Node.js (Playwright MCP browser), an
  embedding model and a small model in Ollama.
- **Architecture.** One process: a `ThreadingHTTPServer` (dashboard, JSON API, `/mcp`), a scheduler thread, a
  Telegram poller and short-lived agent-run threads, all over one SQLite database and one folder per company.
  Feature modules register their own tools (`@tool`) and routes (`@route`) at import. See
  [system-design.md](architecture/system-design.md).
- **Size.** About 36,000 lines of Python in `holdco/` (58 modules), 4,000 lines of `app.js`, about 920 tests.
- **Current state (2026-10-20).** Every phase of `docs/plan.md` (0 to 4) is built and in `main`. What remains is
  the owner's one-week gate (`docs/phases/phase-4-scale.md`) and checking the vendor API details marked "verify" in
  the Phase 4 connectors before real accounts are connected.

## Where things are

```
run.py                 start server + scheduler (python run.py [--port N] [--no-browser])
holdco/
  agents.py llm.py tools.py roles.py presets.py         agent runtime
  truth.py context.py planning.py playbooks.py checks.py ground truth, search, owner questions, founding
  brain.py contracts.py rhythms.py decisions.py portfolio.py   organization (Phase 2, 4)
  actions.py trust.py taint.py verify.py sandbox.py vault.py   safety and approvals
  connectors.py conn_*.py mcp.py oauth.py mcp_server.py emailer.py web.py browser.py   integrations
  scheduler.py watchdog.py doctor.py notify.py backup.py server.py routing.py bus.py   operations
  db.py config.py workspace.py embeddings.py             storage, settings, company folders
  bench/                 Holdco Bench (scenarios, runner, mocks = the shared fake outside world, CLI)
  static/                dashboard (index.html, app.js, style.css)
tests/                   unittest suites per phase, test_e2e.py (real server subprocess), fake_*.py services
docs/                    plan, design documents, phase specs; docs/context/ (this folder)
data/                    created at run time (git-ignored): holdco.db, companies/<slug>/, vault, backups
```

## Key commands

```
python run.py                                          # dashboard on http://127.0.0.1:8420
python -m unittest discover -s tests -t .              # full suite, no network or GPU (about 2.5 min)
python -m unittest tests.test_phase3_verify -v         # one module
python -m holdco.bench --model gpt-oss:20b             # score a real model (uses a temporary data folder)
python tests/fake_ollama.py 11999                      # a scripted model server for clicking around
python -m holdco.mcp_server                            # stdio MCP entry point (proxies to the running dashboard)
```

## AI collaboration notes

**Hard constraints** (each has an ADR):
- Standard library only, and it must run on Python 3.10, 3.12 and 3.13 on Windows and Linux. No new dependencies.
- Agents never act on the outside world directly: every outbound action goes through `actions.gate`. Purchases,
  irreversible actions and the kill switch cannot be bypassed by any policy, trust level or client.
- What agents are told about the world comes from Holdco's records, not from agents' own claims.
- Tests never use the network or a real model; they use the fakes. A tool or connector without a fake is not done.
- The phase specs in `docs/phases/` are binding; a deviation is recorded in the spec with its reason.

**Conventions** (see [workflows/development.md](workflows/development.md)):
- Comments and messages in plain English, one idea per sentence; module docstrings say what the module owns and
  which phase and spec section it implements.
- Tool results the model reads are short and actionable: `ERROR: <what is wrong>. <what to do instead>.`
  A refusal that can never be satisfied makes a small model loop; every push-back happens once.
- Times are UTC strings from `db.now()` (`YYYY-MM-DDTHH:MM:SSZ`); only routine hours and quiet hours use local time.
- New tables go in `db.SCHEMA` (`CREATE TABLE IF NOT EXISTS`), new columns in `db.MIGRATIONS`. Never drop data.
- Commits end with the co-author line the session asks for; one pull request per phase or topic.

**Things that look like bugs but are deliberate:** unknown tool arguments are dropped silently (local models add
harmless extra keys); `0` for an optional ranged number means "not given"; agents cannot set task budgets; a
company is never deleted by Holdco, only paused; bench companies (status `bench`) are invisible everywhere.

## Glossary

- **Company / agent / preset.** A company has agents; each agent is hired from a preset (role definition, tool
  belt, routines) in `presets.py`. The CEO plans; executors work tasks.
- **Run kinds.** `founding` (staged: research, charter, critic, plan), `cycle` (CEO planning), `task`, `chat`.
- **Routine / rhythm.** Recurring duties from presets; the rhythms (morning plan, evening brief, weekly review,
  monthly check) get data gathered by code and store a brief.
- **Request, question, decision.** All are `approvals` rows. A request asks "may I do this?" (send, post, deploy,
  buy); a question asks for information; a decision offers options with a recommendation and a deadline.
- **Policy / trust level / taint.** Per action: ask, auto or off. The trust ladder can promote routine actions;
  taint (the session read outside content) forces asking.
- **Facts block / `records/`.** What Holdco knows from its database, injected into every prompt and written to a
  read-only folder in the company's workspace.
- **Brain / digest.** Strategy, goals, contacts, decisions, experiments and lessons as tables; the digest is their
  6,000-character summary at the top of every prompt.
- **Claims gate / verification gate.** The first refuses text that states things the records don't show; the
  second checks finished work by its kind (page, code, payment link, outreach, research).
- **Connector / risk tag.** An outside service on a card; every tool carries a risk tag (read, write, publish,
  contact, spend, irreversible) that decides how the gate treats it.
- **Bench.** 33 scripted scenarios that score a model on Holdco's own work.
- **Scorecard / share.** Phase 4's per-company score that sets each company's share of the GPU.
