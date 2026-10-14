# Holdco

Run AI-operated companies on your own PC. You describe an idea (or let the AI suggest one), and a team of
agents running on your GPU founds the company and keeps it going around the clock: a CEO that plans, plus
research, engineering, marketing, sales, support and operations agents that do the work. You sit on top as
the owner: you set direction, chat with the team, and approve anything that leaves your computer.

Everything runs locally. The only requirements are Python and a local model server (Ollama).
No cloud AI, no API bills, no pip installs.

## What you get

- **Portfolio of companies.** Start as many as you like; each has its own team, task board, memory and folder.
- **Agent library: 19 researched roles.** Each preset has a mission, a definition of done, a step-by-step
  playbook, hard rules, KPIs, a focused tool belt, recurring duties and model settings, built from primary
  sources (Google Search Central, web.dev, W3C, the FTC, the ICO, the IRS…) and research on why multi-agent
  systems fail. Start from a team template or hire roles one by one.
- **Founding from scratch.** The CEO researches the market on the web, writes a charter (offer, pricing,
  go-to-market, 30-day plan, KPIs), names the company and hands out the first week of tasks.
- **24/7 autopilot.** The CEO re-plans on a schedule (hourly by default, or sooner when the team runs out of
  work). Idle agents pick up tasks automatically. Interrupted work resumes after a restart.
- **Real output.** Agents write real files: a website in `site/` (previewable instantly), research,
  marketing drafts, lead lists, an FAQ, a ledger. Every file is a normal file you can open.
- **Inbox (you're the layer on top).** Emails, social posts, website deploys, outside API calls, code
  execution and purchases wait for your OK. You can edit an email or post before approving it. Reject with
  a note and the agent gets a task to revise it. Spending money always needs you; Holdco never pays for anything.
- **Chat with your team.** Message the CEO or any agent; they act on it (create tasks, change plans) and reply.
- **Live feed** of everything every agent does, plus full transcripts of every work session.
- **Thinking tab.** Watch each agent's reasoning, the tool it picks and what the tool returns, live, per agent.
  Shows the model's own reasoning when it shares it (gpt-oss, Qwen3 and other thinking models).
- **Integrations:** web search (DuckDuckGo, SearXNG or Brave), company email (SMTP/IMAP; inbound mail becomes
  support tasks), website publishing (Netlify), and a posting webhook for X/LinkedIn/Discord via Zapier, Make or n8n.

## Quick start

### 1. Install the two prerequisites

- **Python 3.10+**: <https://www.python.org/downloads/>. On Windows, tick *Add python.exe to PATH*.
  On Linux it's usually already there (`python3 --version`).
- **Ollama**: <https://ollama.com/download>.

### 2. Download a model

```
ollama pull gpt-oss:20b
```

`gpt-oss:20b` is the default because it produces the most reliable tool calls among open models that fit
comfortably on a 32 GB card, and tool-call reliability matters more than raw benchmark scores in an agent
loop. You can switch models any time in **Settings**, which lists everything you have installed. Check
<https://ollama.com/library> for newer tool-capable models (look for the *tools* tag).

### 3. Start Holdco

- **Windows:** double-click `start_windows.bat`
- **Linux:** `./start_linux.sh`

The dashboard opens at <http://127.0.0.1:8420>. Click **Start a company**.

## Agent presets

| Category | Roles |
|---|---|
| Leadership | Chief Executive, Product Manager |
| Research | Market Research Analyst, Customer Research Lead |
| Build | Web Engineer, QA & Review Lead, Brand & UX Designer |
| Growth | Growth Marketer, SEO & Content Writer, Social Media & Community Manager, Email & Lifecycle Marketer, Paid Ads Strategist, Partnerships Manager |
| Sales & Customers | Sales Development (Outbound), Customer Support Lead, Customer Success & Retention |
| Operations | Finance & Operations, Legal & Compliance Officer, Data & Analytics Analyst |

Browse them under **Agent library**, read any role's full playbook and sources, and hire from a company's
**Team** tab. Team templates (Startup launch, SaaS, Content & newsletter, B2B services, Digital products,
Minimal) pick a starting team when you found a company. Every agent can be customised: extra instructions,
reasoning effort, model, and whether it does recurring duties.

What's built into every preset, and why:

- **Short, structured prompts.** Mission, definition of done, numbered workflow, at most about 7 rules with the
  reason behind each. Instruction-following in small models drops as instruction counts climb, so the
  important rules come first.
- **8–14 tools per role** instead of all 28. Small models pick the right tool far more often from a short list,
  and an agent can't use tools outside its role.
- **Tasks with a deliverable and acceptance criteria.** An agent can't mark a task done while its deliverable
  file is missing or unchanged; it gets sent back once, then the task is marked partial. "Done but never
  shipped" is the most reported failure of AI-run companies.
- **Checks instead of opinions.** `audit_page` (accessibility, SEO, broken links, performance, fake-claim
  detection), `check_contrast` (WCAG ratios) and `review_text` (reading grade, cold-email length, opt-out
  lines, hype and unsourced numbers). Research shows reviewers grounded in tool output catch real problems,
  while a model re-reading its own work mostly doesn't.
- **A QA & Review Lead** in the default team that reviews finished work daily against its criteria.
- **Loop guard.** Repeating the identical action is stopped, since step repetition is the single most common
  multi-agent failure.
- **Standard handoff** (status, deliverable, evidence, open issues, next step) so the CEO can verify work, plus
  an automatic deliverable check on everything the CEO reviews.
- **Recurring duties** such as the CEO's morning report and weekly review, QA's daily review, sales follow-ups
  every 3 days, support inbox sweeps every 4 hours, the analyst's weekly KPI report and the monthly finance
  close. Duties that need email only start once email is connected.
- **Model settings per role.** gpt-oss gets a reasoning effort per role (high for CEO, engineering, QA,
  finance, legal and ads planning; medium elsewhere), which raised its tool-use benchmark scores
  substantially in OpenAI's model card. Sampling follows each model maker's recommendation automatically
  (gpt-oss: temperature 1.0; Qwen3: 0.6/0.95/top-k 20). Switch to manual in Settings if you prefer.

The research notes behind each role are linked from its playbook. Benchmarks that come from vendors (cold-email
reply rates, email open rates) are used as rough guides, not targets.

## Getting the most out of an RTX 5090

On a single 32 GB card, use **one model for every agent**. If agents use different models, Ollama has to
swap them in and out of VRAM, which is far slower than sharing one.

Let two agents work at the same time by telling Ollama to serve parallel requests, then set
**Settings → Agents working at once** to the same number:

- **Windows:** *System Properties → Environment Variables*, add user variables, then quit and reopen Ollama
  from the tray:
  - `OLLAMA_NUM_PARALLEL` = `2`
  - `OLLAMA_KEEP_ALIVE` = `30m`
  - `OLLAMA_FLASH_ATTENTION` = `1` (smaller memory use for long contexts)
- **Linux:** `sudo systemctl edit ollama` and add:
  ```
  [Service]
  Environment="OLLAMA_NUM_PARALLEL=2" "OLLAMA_KEEP_ALIVE=30m" "OLLAMA_FLASH_ATTENTION=1"
  ```
  then `sudo systemctl restart ollama`.

With `gpt-oss:20b` and the default 32k context, two parallel agents fit with room to spare. If you try a
bigger model and see slowdowns, run `ollama ps`: anything not 100% on GPU is spilling into system RAM,
so lower *Agents working at once* or the context window.

## Keeping companies running

Agents work while Holdco is running and your PC is awake.

- **Start automatically at login:** Windows `autostart_windows.bat`; Linux `./autostart_linux.sh`
  (installs a systemd user service; run `sudo loginctl enable-linger $USER` once so it runs while you're logged out).
  Undo with `autostart_windows.bat uninstall` or `./autostart_linux.sh uninstall`.
- **Stop the PC from sleeping** (Windows: *Settings → System → Power → Sleep: Never* when plugged in).
- **Autopilot switch:** the button at the bottom of the sidebar stops or starts all agents at once.
  Each company also has its own Pause button.
- **Check from your phone:** start with `HOLDCO_HOST=0.0.0.0` and `HOLDCO_PASSWORD=something-long`, then open
  `http://<your-pc's-ip>:8420` on your home Wi-Fi. For access away from home, use a private network such as
  Tailscale rather than opening a port on your router.

## Connecting the outside world (per company, in its Settings tab)

| What | How | Without it |
|---|---|---|
| Email | Any mailbox with SMTP + IMAP (Gmail with an app password, Zoho, Fastmail…). Inbound mail becomes tasks for the support agent. | Sales and support write drafts only |
| Website | A Netlify personal access token. Holdco creates the site on the first approved deploy. | The site still previews locally at `/preview/<company>/` |
| Social posts | A webhook URL (Zapier, Make, n8n, Discord…). Approved posts are sent there as JSON. | You get a one-click "post on X" link with the text filled in |
| Search | DuckDuckGo works with no key. For heavy use, run SearXNG locally or add a Brave Search API key in Settings. Optional: `pip install ddgs` makes DuckDuckGo searches more reliable. | |

## How it works

```
You (owner) ── chat, approvals, standing orders, tasks ─────────────┐
                                                                     ▼
Scheduler ── every few seconds ──► CEO planning cycle ──► task board
                                                           │
             idle agents pick up tasks ◄───────────────────┘
                      │
                      ▼
   agent session: system prompt (role, charter, standing orders, memory, metrics)
                  → model calls tools → results → … → finish
                      │
     files in the company folder · memory · new tasks · requests in your inbox
```

- **Standing orders** (company Settings) are injected into every agent's every session: brand rules,
  things to avoid, priorities.
- **Memory**: agents save facts, decisions and lessons. Your approvals, rejections and answers are saved
  too, so agents learn what you accept. Pin notes that must never be forgotten.
- **Approval policies**: per action type, globally and per company: *Ask me first* (default),
  *Let agents do it*, or *Never allow*. Purchases are always *Ask me first*.

## Safety notes

- Agents' web access is limited to the public internet; requests to this PC or your home network are blocked.
- Agent-built web pages are served sandboxed so they can't control the dashboard.
- **Run code** really runs Python or shell commands on your PC (inside the company's folder, with a time
  limit, but not in a sandbox). Leave it on *Ask me first* unless you're comfortable with that.
- Passwords and tokens you enter are stored in plain text in `data/holdco.db` on your PC. Protect that folder.
- You are the owner of these companies. Laws about marketing email (CAN-SPAM, GDPR), business registration,
  taxes and refunds apply to you, not the AI. Review outreach before you approve it.

## What to expect

Local 20–30B models are genuinely useful for research, writing, building simple sites and organizing
work, but they're weaker than the frontier cloud models that hosted services use. Expect some drafts you'll
want to edit, occasional tasks that need a second attempt, and plans that benefit from your judgment.
Session transcripts (Team → Sessions) show exactly what each agent did and why.

## Your data

Everything lives in the `data/` folder next to this README: the database (`holdco.db`) and one folder per
company under `data/companies/`. Back it up by copying the folder. Deleted companies are moved to
`data/trash/`. Set `HOLDCO_DATA` to keep data somewhere else.

## Troubleshooting

- **"Can't reach the model server"**: start Ollama (Windows: it lives in the tray; Linux: `systemctl status ollama`).
- **Model not installed**: `ollama pull <model>`, or pick an installed one in Settings.
- **Agents talk instead of acting**: use a model with tool support; Settings → *Test connection* checks it.
  For models without native tool calling, set *Tool calling* to *Prompt-based*.
- **Port 8420 in use**: Holdco is probably already running; open <http://127.0.0.1:8420>, or start with `--port 8421`.
- **Search returns nothing**: DuckDuckGo may be rate-limiting you. Install `ddgs`, or switch to SearXNG or Brave.

## Running the tests

```
python -m unittest discover -s tests -t . -v
```

No GPU or model needed: `tests/fake_ollama.py` is a scripted stand-in for Ollama (and the OpenAI-compatible
API) so the whole app runs end to end. The suite covers the quality checks, the model client, preset
integrity, file and network safety, tools and approvals, and a full run of the real server: founding from a
template, the 24/7 scheduler, approvals, chat, QA review with the deliverable check, recurring duties, restart
recovery, the prompt-based tool fallback, the OpenAI-compatible backend, pausing and deletion. GitHub Actions
runs it on Windows and Linux for Python 3.10, 3.12 and 3.13 on every push.

To click around the dashboard without a GPU, run `python tests/fake_ollama.py 11999`, start Holdco, and set the
server address in Settings to `http://127.0.0.1:11999`.

## Project layout

```
run.py              start the server + scheduler
autostart.py        start-at-login / systemd service helper
holdco/
  agents.py         agent loop, prompts, founding, chat, idea generation
  presets.py        the agent library: 19 roles and 6 team templates
  checks.py         audit_page, check_contrast and review_text
  tools.py          every tool agents can call
  actions.py        approval inbox and outbound actions (email, posts, deploys, code, purchases)
  scheduler.py      24/7 loop: CEO cycles, task dispatch, inbox polling
  llm.py            Ollama / OpenAI-compatible client with tool-call fallbacks
  roles.py          maps agents to presets and tool belts
  web.py, emailer.py, workspace.py, db.py, bus.py, server.py, config.py
  static/           the dashboard (plain HTML, CSS and JavaScript)
tests/              unit and end-to-end tests, plus the fake model server
```
