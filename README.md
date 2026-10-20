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
- **Founding from scratch, in stages.** The CEO researches the market (saving verified facts with their
  sources), writes a charter that must include a price, named competitors, how customers will be reached and a
  riskiest assumption with a pass threshold, has a "skeptical investor" pass attack it, then plans the first week
  around the strongest objections. Each stage has its own step budget, so research can't eat the whole founding.
- **24/7 autopilot that doesn't spin.** The CEO re-plans when something has changed (an answer from you, a
  finished task, an email, new settings) or on its hourly schedule, and backs off while nothing changes and the
  work waits on you. Idle agents pick up tasks automatically. A daily token budget per company caps the effort.
- **Real output.** Agents write real files: a website in `site/` (previewable instantly), research,
  marketing drafts, lead lists, an FAQ, a ledger. Every file is a normal file you can open.
- **Inbox (you're the layer on top).** Emails, social posts, website deploys, outside API calls, code
  execution and purchases wait for your OK. You can edit an email or post before approving it. Reject with
  a note and the agent gets a task to revise it. Spending money always needs you; Holdco never pays for anything.
- **Chat with your team.** Message the CEO or any agent; they act on it (create tasks, change plans) and reply.
- **Live feed** of everything every agent does, plus full transcripts of every work session.
- **Thinking tab.** Watch each agent's reasoning, the tool it picks and what the tool returns, live, per agent.
  Shows the model's own reasoning when it shares it (gpt-oss, Qwen3 and other thinking models).
- **Doctor page.** A green, amber or red row with a fix for everything your agents depend on: model server and
  model, GPU memory fit, web search, each connector, disk space, the last backup and the last 24 hours of
  errors. Founding runs the same checks first and refuses to start while the model server is down, telling you why.
- **Watchdog.** When something is stuck (a task that failed twice, the same error three times in an hour, a
  task bouncing between agents, a connector that stays down, stalled agents, a founding that ended early) you
  get one question with a recommended answer, in the dashboard and on your phone. Your answer is applied at once.
- **Working notes and retry-differently.** Each task keeps notes on what was tried, what failed and what is
  left. A second attempt starts from those notes and is told why the first one failed, so it tries another way.
- **Phone notifications.** A Telegram bot sends requests and questions with buttons, so you approve, reject or
  answer from your phone; ntfy works as a notify-only option. Nothing on your PC is opened to the internet.
- **Kill switch.** One button stops every agent at its next step and freezes outbound actions until you
  resume, from the sidebar or by sending `/stop` to the bot.
- **Nightly backups and an audit log.** Once a day Holdco zips the database and every company folder and keeps
  the newest 14. Every outbound action that ran is recorded with who proposed it and who allowed it.
- **Agents see the truth.** Every prompt carries a facts block built from Holdco's own records: whether the
  site is deployed, how many emails really went out, which accounts the company has, what waits on you, what is
  down. A read-only `records/` folder holds the same history. When an agent writes a file, messages you or
  finishes a task claiming a deploy, a sent email, a signup or a payment that the records don't show, Holdco
  refuses it and tells the agent what really happened. Tasks to send or deploy are done only when that happened.
- **One question per topic.** An agent can't ask you something that is already waiting; questions come with
  options and a recommendation as buttons, and at most three from the team wait at once.
- **Your context, everywhere.** Drop text files about you (skills, budget, brand rules) in *Your context*, or
  about one company in its *Context* tab, and mark the essential ones "Always include". Agents find everything
  else with one search over files, your context, memory, tasks, emails, chat, decisions, past sessions, verified
  facts and pages they already fetched, and read long files by line range or outline. You can use the same
  search from the dashboard.
- **Business playbooks.** Holdco recognises the type of business (lead generation, newsletter, SaaS, services,
  digital products, e-commerce, local services) and gives the CEO that model's riskiest assumptions, first
  experiments with real customers, metrics and traps.
- **Search that keeps working.** Brave, SearXNG, the `ddgs` package and DuckDuckGo's HTML pages are tried in
  order; a provider that fails or is blocked is skipped for ten minutes while the others carry on.
- **Connected services.** Stripe (payment links, payments, revenue), Resend (sending email), a Playwright browser,
  Buffer (social posting, beta) and Brave search connect through setup cards, plus any MCP server. Agents load a
  service's tools only when they need them with `find_tool`, so their tool lists stay short. Every tool carries a
  risk tag: reading runs freely, while changing, publishing, contacting people, spending and anything irreversible
  follow your approval policies, and spending and irreversible actions always ask you.
- **Numbers nobody typed.** Connected services report metrics on their own every hour: Stripe revenue, payments,
  customers and MRR, emails sent and replies. Each number shows where it came from.
- **Keys in a vault.** Passwords, API keys and tokens live in Windows DPAPI, the Linux keyring or an encrypted file,
  never in the database, and are scrubbed from the feed, transcripts, the audit log and `records/`.
- **A browser check.** `browser_check` renders a page in a real browser (Playwright) with console errors, links,
  forms and phone-size screenshots, or runs a labelled static check when Playwright isn't installed.
- **A company brain.** Every company keeps its strategy (one-liner, customer, problem, offer, price, positioning,
  voice), up to three measured goals, contacts, decisions with a review date, experiments and lessons as structured
  records. Every agent reads a one-page digest of it first, with each number's source. Other agents propose changes
  for the CEO to review; a proposed change to price, offer, positioning or customer comes to you as a decision.
  Edit any of it on the **Brain** tab.
- **Top-down planning.** The CEO plans from the goals: the gap to each target, one or two bets as experiments with a
  stop date, and tasks that name the goal they serve and the effect they should have. Tasks can depend on other
  tasks, carry a due date, a step and token budget and the checks that will run when they finish. Two agents can't
  overwrite the same file: the first one holds a 30-minute lease.
- **Daily and weekly rhythms.** A morning plan, an evening brief, a weekly board memo and a monthly keep, change or
  kill check, built from numbers Holdco gathers itself. A brief can use only numbers from its data, and the weekly
  review scores goals only with numbers from connected services, never with numbers agents typed in. Briefs arrive
  on your phone, the company Overview and, as one portfolio brief, at the top of the Portfolio page. After each
  weekly review, tasks an agent created that serve no goal are cancelled.
- **Decisions, not just approvals.** When the CEO needs you to choose, it sends a decision with options, its
  recommendation and a deadline. Reversible decisions take the recommendation when the deadline passes; irreversible
  ones wait for you. What you chose goes into the brain and comes back for review two weeks later.
- **Budgets.** GPU hours per day, tool calls per task, posts per week and emails per day, per company, in its
  Settings tab. The Overview shows how much of today's GPU budget is used.
- **Holdco Bench: scores for your model.** *Settings → Test this model* runs 33 scenarios of Holdco's own work
  against stand-in services (found a company, fix a page that fails QA, write a compliant cold email, resist a
  prompt injection, recover from a tool error, plan top down...) and shows the pass rate, how many tool calls were
  valid, tokens per second and steps per scenario. Test two models and the *Model scores* page puts them side by
  side, so you pick by score. Also from the command line: `python -m holdco.bench --model hf.co/bartowski/huihui-ai_Huihui-Qwen3.8-27B-abliterated-GGUF:Q6_K_L`.
- **Work is checked before it counts as done.** A page must pass a browser check (or a labelled static check when
  the Playwright browser isn't connected), code must pass its tests, a payment link must exist in Stripe, research
  may cite only pages the agent actually read, and every email must pass the outreach rules. A task that fails is
  sent back once and otherwise ends partial; the CEO's review sees the results, not just the agent's word.
- **Outside content can't steer your agents.** After an agent reads a web page, search results or an inbound email,
  that text is marked as information, not instructions, and the agent's emails, posts and other outward actions
  wait for you, whatever your approval settings say; the Inbox card says why.
- **A trust ladder instead of on/off.** Approve 15 of the same kind of request without edits and Holdco asks
  whether agents may do it on their own, up to a daily cap. An edit resets the count; a rejection steps it back.
  Spending money, irreversible actions, legal wording and a first email to a new person always ask.
- **Code runs in a sandbox** when Docker Desktop or Podman is installed: no network, capped memory and processes,
  only the company's folder visible. Without one, the Doctor page says so and code requests say they run on this PC.
- **Smarter use of the GPU.** Tool calls in prompt-based mode are shaped by a schema so they always parse, and a
  wrong argument gets an exact repair message. An optional small model sorts inbound email, summarises old tool
  results and spots duplicate memories. The owner's follow-ups and tasks due soon go first; low-value research
  waits for the quiet hours. Settings shows how full the context window gets.
- **Also built in:** company email over SMTP/IMAP (inbound mail becomes support tasks), website publishing
  (Netlify), and a posting webhook for X/LinkedIn/Discord via Zapier, Make or n8n.

## Quick start

### 1. Install the two prerequisites

- **Python 3.10+**: <https://www.python.org/downloads/>. On Windows, tick *Add python.exe to PATH*.
  On Linux it's usually already there (`python3 --version`).
- **Ollama**: <https://ollama.com/download>.

### 2. Download a model

```
ollama pull hf.co/bartowski/huihui-ai_Huihui-Qwen3.8-27B-abliterated-GGUF:Q6_K_L
```

The default is the Huihui Qwen3.8 27B (Q6_K_L, about 22 GB). On Holdco Bench it scored 24 of 33 with 100% valid
tool calls, against 22 to 29 for `gpt-oss:20b`, but it runs at about 48 tokens per second on an RTX 5090 where
gpt-oss runs at about 200, so agents take roughly four times as long per reply. If you'd rather have speed,
`ollama pull gpt-oss:20b` and pick it in **Settings**. This model has had its refusals removed: see
[docs/safety.md](docs/safety.md#the-abliterated-model) for why the guardrails don't depend on it. You can switch models any time in **Settings**, which lists everything you have installed. Check
<https://ollama.com/library> for newer tool-capable models (look for the *tools* tag).

### 3. Start Holdco

- **Windows:** double-click `start_windows.bat`
- **Linux:** `./start_linux.sh`

The dashboard opens at <http://127.0.0.1:8420>. Click **Start a company**.

When something does not work, open **Doctor** in the sidebar: every failing row says what is wrong and how to
fix it. Founding refuses to start while the model server is down or the model is missing, and the New company
page shows the failing check with its fix (you can still press *Found anyway*).

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
  - `OLLAMA_NUM_PARALLEL` = `1`
  - `OLLAMA_KEEP_ALIVE` = `30m`
  - `OLLAMA_FLASH_ATTENTION` = `1` (smaller memory use for long contexts)
- **Linux:** `sudo systemctl edit ollama` and add:
  ```
  [Service]
  Environment="OLLAMA_NUM_PARALLEL=1" "OLLAMA_KEEP_ALIVE=30m" "OLLAMA_FLASH_ATTENTION=1"
  ```
  then `sudo systemctl restart ollama`.

With the default 27B model and a 32k context, one agent at a time fits in 32 GB of VRAM; a second would push the
model partly into system RAM. With the smaller `gpt-oss:20b`, two parallel agents fit with room to spare: set
`OLLAMA_NUM_PARALLEL` and *Agents working at once* to 2. If you see slowdowns, run `ollama ps`: anything not
100% on GPU is spilling into system RAM, so lower *Agents working at once* or the context window.

To choose between models, press **Test this model** in Settings for each one and compare them on the *Model
scores* page. Settings also shows *Context use*: when the 95th percentile gets near the window, raise the context
size or trim pinned context.

## Keeping companies running

Agents work while Holdco is running and your PC is awake.

- **Start automatically at login:** Windows `autostart_windows.bat`; Linux `./autostart_linux.sh`
  (installs a systemd user service; run `sudo loginctl enable-linger $USER` once so it runs while you're logged out).
  Undo with `autostart_windows.bat uninstall` or `./autostart_linux.sh uninstall`.
- **Stop the PC from sleeping** (Windows: *Settings → System → Power → Sleep: Never* when plugged in).
- **Autopilot switch:** the button at the bottom of the sidebar stops or starts all agents at once.
  Each company also has its own Pause button.
- **Kill switch:** **Stop everything** under the autopilot switch (or `/stop` to your Telegram bot) stops every
  agent at its next step and freezes outbound actions: nothing is sent, published, run or bought, and requests
  cannot be approved until you press **Resume** (or send `/resume`). Questions can still be answered.
- **Backups:** once a day Holdco zips the database and every company folder into `data/backups/` and keeps the
  newest 14 (Settings → Reliability changes the number; **Back up now** on the Doctor page writes one at once).
  To restore by hand: stop Holdco, unzip the archive over the `data/` folder, start Holdco again.
- **Check from your phone:** start with `HOLDCO_HOST=0.0.0.0` and `HOLDCO_PASSWORD=something-long`, then open
  `http://<your-pc's-ip>:8420` on your home Wi-Fi. For access away from home, use a private network such as
  Tailscale rather than opening a port on your router. Approvals alone need none of this: see the next section.

## Running it from your phone

1. In Telegram, open **@BotFather**, send `/newbot` and follow the prompts. Copy the token it gives you.
2. In **Settings → Phone notifications**, paste the token and your chat id, save, and press **Send test
   message**. No chat id yet? Leave it empty, send your bot any message, and press the button: it reports the
   chat id for you to paste in. Once both are saved, a test message with an OK button arrives.

What arrives: every request waiting for your approval, with **Approve** and **Reject** buttons; every question
from an agent or the watchdog, with one button per option and the recommended one marked; and errors, once per
distinct error per hour (*Notify me about* widens this to everything). A button press decides it as the Inbox would.

Commands: `/status` (one line per company with open tasks, agents working and requests waiting, plus the kill
switch and the Doctor summary), `/stop` (kill switch on) and `/resume` (kill switch off).

**ntfy** is the notify-only option: install the ntfy app, pick a topic name nobody would guess, enter it in
Settings and subscribe to it in the app. The same messages arrive as plain text, without buttons.

Nothing on your PC is exposed to the network: Holdco asks Telegram for new messages (long polling), so no port
is opened and no address is published. Messages from any chat other than yours are ignored.

## Connecting the outside world

Services live on **connector cards**: per company in its Settings tab (Stripe, Resend, Buffer) and for every company
on the Settings page (Playwright, Brave). Each card says where to get the key, which permissions to tick, what agents
will be able to do and in which mode (Stripe starts in test mode with a test key). *Save and test* runs a health
check; the card then shows the result and the latest numbers it reported. A connector that breaks turns red on the
Doctor page, sends one phone message a day and is listed to the agents as down so they plan around it. Hosted MCP
servers that use OAuth get a *Sign in* button.

- **Stripe**: a test key (or, for live use, a restricted key with the Agent tag, which Stripe's MCP server requires
  from December 2026). Agents can create payment links and read payments; refunds always ask you.
- **Resend**: an API key and a verified sending domain. Set *Send email with* to auto or Resend in the Email section.
- **Playwright**: needs Node.js. The first test downloads a browser, so allow a minute.
- **Buffer**: beta; saved turned off until you switch it on.

The older settings still work:

| What | How | Without it |
|---|---|---|
| Email | Any mailbox with SMTP + IMAP (Gmail with an app password, Zoho, Fastmail…). Inbound mail becomes tasks for the support agent. | Sales and support write drafts only |
| Website | A Netlify personal access token. Holdco creates the site on the first approved deploy. | The site still previews locally at `/preview/<company>/` |
| Social posts | A webhook URL (Zapier, Make, n8n, Discord…). Approved posts are sent there as JSON. | You get a one-click "post on X" link with the text filled in |
| Search | Works with no key. Holdco tries the provider you picked in Settings first, then Brave (needs an API key), SearXNG (needs its address), the optional `ddgs` package and DuckDuckGo's HTML pages, in that order. A provider that fails or is blocked is skipped for ten minutes while the others carry on. The Doctor's *Web search* row shows which provider works. | Agents are told search is down and carry on with what they know |

## How it works

```
You (owner) ── chat, approvals, standing orders, tasks ─────────────┐
                                                                     ▼
Scheduler ── every few seconds ──► CEO planning cycle ──► task board
                                                           │
             idle agents pick up tasks ◄───────────────────┘
                      │
                      ▼
   agent session: system prompt (role, standing orders, facts, brain digest, playbook, your pinned context,
                  what changed, what waits on you, relevant memory, what can be opened)
                  → model calls tools → results → … → finish
                      │
     files in the company folder · memory · new tasks · requests in your inbox
```

- **Standing orders** (company Settings) are injected into every agent's every session: brand rules,
  things to avoid, priorities.
- **Memory**: agents save facts, decisions and lessons. Your approvals, rejections and answers are saved
  too, so agents learn what you accept. Pin notes that must never be forgotten.
- **Company brain**: strategy, goals, contacts, decisions, experiments and lessons in their own tables, shown to
  every agent as a digest of at most 6,000 characters (about 1,500 tokens) with each metric's source. The CEO writes
  it directly; other agents propose changes. Goals refresh their current value from the newest metric of the same
  name, so a goal tied to `stripe.customers` moves when Stripe reports a customer.
- **Task contracts**: a task names its goal, the expected effect, files to read first, the tasks it waits on, a due
  date, a tool-call and token budget, and the checks to run on the result. The scheduler starts a task only when
  everything it depends on is done or cancelled, and a file being written by one agent is leased to it.
- **Rhythms**: the morning plan (7:00), the evening brief (18:00) and the weekly review (9:00, once a week from the
  end of the first week) follow your PC's clock; the data for each is gathered by code and handed to the agent as
  the only numbers it may use. "Run now" on the Overview starts any of them at once.
- **Approval policies**: per action type, globally and per company: *Ask me first* (default),
  *Let agents do it*, or *Never allow*. Purchases are always *Ask me first*.
- **Watchdog**: a task that fails twice is blocked and becomes a question in your Inbox and on your phone (skip
  it, retry once with your note, or pause the company). The second attempt is told what failed the first time
  and must try something else. It also catches repeated errors, bouncing tasks, dead connectors and stalled agents.
- **Facts and records**: before every session Holdco rewrites `records/` and the facts block from its database.
  Agents can read them but not write them, and claims that contradict them are refused before they reach a file,
  your chat or a task handoff. Charters are validated whichever tool writes them.
- **Context and search**: an incremental full-text index (SQLite FTS5) covers files, your context library,
  memory, tasks, emails, chat, decisions, sessions, facts and fetched pages. Optional meaning-based search uses an
  Ollama embedding model (set it in Settings).
- **Working notes**: agents save what they tried, what failed and what is left with `note_progress`, and Holdco
  adds a note of its own when a run ends badly. The next attempt starts from those notes instead of from scratch.

## Safety notes

- Agents' web access is limited to the public internet; requests to this PC or your home network are blocked.
- Agent-built web pages are served sandboxed so they can't control the dashboard.
- **Run code** runs inside a Docker or Podman container when one is installed (no network unless you tick *Allow
  network* on the request, 1 GB of memory, one CPU, the company folder only). Without one it really runs Python or
  shell commands on your PC (inside the company's folder, with a time limit); the Doctor page and the request card
  say so. Leave it on *Ask me first* unless you're comfortable with that.
- **Taint and trust.** Outward actions after an agent read outside content always ask you, and the trust ladder
  (company Settings → Trust) can only let routine requests through up to their daily cap; locked actions never.
- **Audit log** (Inbox page): every outbound action that ran, with who proposed it, who allowed it (you, or a
  *Let agents do it* policy), the exact payload and the result, failures included. Nothing in Holdco edits it.
- **Kill switch.** One press (sidebar, or `/stop` from your phone) stops every agent and freezes outbound actions.
- **Outreach rules.** An email to a prospect is queued only with a real-looking address (no placeholder or
  reserved domains), not on the suppression list (anyone who replied "stop" to any of your companies), and with
  your postal address and an opt-out line, which Holdco adds when missing. Set the postal address in company
  Settings. Pages with placeholders (an analytics id like G-XXXX, lorem ipsum, example.com) can't be deployed.
- **Relay services blocked.** Agents can't fetch pages through services that hide where requests go.
- **Secrets** (mailbox passwords, API keys, the Telegram token, OAuth tokens) are kept in the vault: Windows DPAPI,
  the Linux keyring via `secret-tool`, or `data/vault.json` encrypted with `data/vault.key` (or with a passphrase in
  `HOLDCO_VAULT_PASSPHRASE`). Existing plain-text keys move there on the first start. Back up `data/vault.key`
  with your data; on another computer, DPAPI and keyring secrets have to be entered again.
- **Connected services** follow the same approval policies as email and posts, by risk tag. A service tool Holdco
  doesn't know asks you first. The agents' browser runs with its own profile and can't open addresses on your PC
  or home network.
- You are the owner of these companies. Laws about marketing email (CAN-SPAM, GDPR), business registration,
  taxes and refunds apply to you, not the AI. Review outreach before you approve it.

## What to expect

Local 20–30B models are genuinely useful for research, writing, building simple sites and organizing
work, but they're weaker than the frontier cloud models that hosted services use. Expect some drafts you'll
want to edit, occasional tasks that need a second attempt, and plans that benefit from your judgment.
Session transcripts (Team → Sessions) show exactly what each agent did and why.

## Your data

Everything lives in the `data/` folder next to this README: the database (`holdco.db`), one folder per
company under `data/companies/` and nightly backups under `data/backups/`. Copy the folder or one of the zips
to back it up elsewhere. Deleted companies go to `data/trash/`. Set `HOLDCO_DATA` to keep data somewhere else.

## Troubleshooting

- **Start with the Doctor page** (sidebar). Each failing row says what is wrong and how to fix it. *Run checks*
  retests at once; *Deep check* also asks the model to call a tool and runs a live search.
- **"Can't reach the model server"**: start Ollama (Windows: it lives in the tray; Linux: `systemctl status ollama`).
- **Model not installed**: `ollama pull <model>`, or pick an installed one in Settings.
- **Agents talk instead of acting**: use a model with tool support; Settings → *Test connection* checks it.
  For models without native tool calling, set *Tool calling* to *Prompt-based*.
- **Port 8420 in use**: Holdco is probably already running; open <http://127.0.0.1:8420>, or start with `--port 8421`.
- **Search returns nothing**: Brave, SearXNG, `ddgs` and DuckDuckGo are tried in turn and a blocked one is skipped
  for ten minutes; the Doctor's *Web search* row shows each provider's state. Add a Brave key or run SearXNG.
- **Nothing reaches my phone**: press *Send test message* under Settings → Phone notifications. With a token but
  no chat id it reports your chat id once you have messaged the bot. Check *Notify me about*: at the default
  level only requests, questions and errors are sent. The Doctor's *Telegram bot* row shows whether the token works.

## Running the tests

```
python -m unittest discover -s tests -t . -v
```

No GPU, model or network needed: `tests/fake_ollama.py` is a scripted stand-in for Ollama (and the
OpenAI-compatible API), and `holdco/bench/mocks.py` holds the stand-ins for Telegram, Stripe, an MCP server (stdio
and HTTP, both protocol generations) and web search that the tests and the Bench share, next to fakes for Resend,
Buffer and an OAuth server. The suite has about 860 tests: unit suites for each module of every phase, replays of
what real models did in real runs (founding, briefs, the Bench gate), and a full run of the real server from
founding to the autopilot, approvals, the kill switch, phone notifications, connected services, the brain and its
rhythms, the verification gate, taint and the trust ladder. Holdco Bench runs in the suite twice: the scripted model
must pass all 33 scenarios, and a deliberately lazy one must fail exactly the scenarios it should, which proves
the checks can fail. GitHub Actions runs it on Windows and Linux for Python 3.10, 3.12 and 3.13 on every push.

To click around the dashboard without a GPU, run `python tests/fake_ollama.py 11999`, start Holdco, and set the
server address in Settings to `http://127.0.0.1:11999`.

## Roadmap

Holdco is being built against the plan in [docs/plan.md](docs/plan.md), in six phases, each its own pull
request. Each phase document under `docs/phases/` says what was built and how to try it on your PC.

1. **Phase 0: Stabilize** (built): Doctor and founding preflight, the search fallback chain,
   the watchdog, working notes, Telegram and ntfy, the kill switch, nightly backups and the audit log.
2. **Phase 0.5: Ground truth and context** (built): facts and records agents can't
   edit, a claims gate, one question per topic, change-driven planning, staged founding, charter validation,
   business playbooks, verified research facts, and one search over everything including your own context files.
3. **Phase 1: Connect** (built): an MCP client for both protocol generations, OAuth, a
   credential vault, risk tags on every tool, connector cards with health checks and metric pipes, `find_tool`,
   the first connectors (Stripe, Resend, a Playwright browser, Buffer, Brave) and `browser_check`.
4. **Phase 2: Organize** (built): the company brain with measured goals, top-down planning,
   task contracts with dependencies and file leases, daily and weekly rhythms with briefs built from real numbers,
   a decisions queue with deadlines, and budgets for GPU time, steps and posts.
5. **Phase 3: Verify** (built in this pull request): Holdco Bench to score models on Holdco's own work, the
   verification gate, structured tool calls with repair messages, a code sandbox, taint tracking, a trust ladder
   for approvals, a small model for routine jobs, dispatch priorities and the context-use display.
6. **Phase 4: Scale** (planned): Holdco as an MCP server, portfolio capital allocation, the remaining connectors.

## Project layout

```
run.py              start the server + scheduler
autostart.py        start-at-login / systemd service helper
holdco/
  agents.py         agent loop, prompts, founding, chat, idea generation
  presets.py        the agent library: 19 roles and 6 team templates
  checks.py         audit_page, check_contrast and review_text
  tools.py          every tool agents can call
  actions.py        approval inbox, outbound actions (email, posts, deploys, code, purchases), audit log
  scheduler.py      24/7 loop: CEO cycles, task dispatch, inbox polling, housekeeping
  doctor.py         health checks, the Doctor page and the founding preflight
  watchdog.py       stuck-pattern detection, questions for you, working notes and retry guidance
  notify.py         Telegram bot (long polling, buttons, commands) and ntfy
  backup.py         nightly zip backups of the data folder
  routing.py        route registry shared by server.py and the feature modules
  truth.py          facts block, records/, claims gate, action tasks, outreach checks, verified facts
  context.py        search index, unified search, line reads, edit_file, your context library
  planning.py       one question per topic, change-driven cycles, budgets, staged founding, charter checks
  playbooks.py      business-type playbooks
  brain.py          the company brain: strategy, goals, contacts, decisions, experiments, lessons, the digest
  contracts.py      task contracts, the dependency graph, budgets (GPU hours, steps, tokens, posts)
  rhythms.py        morning plan, evening brief, weekly review and monthly check data; briefs; portfolio brief
  decisions.py      the decisions queue: options, a recommendation, deadlines and auto-resolution
  bench/            Holdco Bench: scenarios, the runner, the stand-in services and the command line
  verify.py         the verification gate by kind of work, outreach rules, the do-not-email list
  sandbox.py        run_code in a Docker or Podman container
  taint.py          outside content: fencing and the "this session read outside content" rule
  trust.py          the trust ladder: levels, streaks, daily caps and promotion questions
  embeddings.py     meaning-based duplicate check for memory
  vault.py          the credential vault (DPAPI, keyring, encrypted file) and redaction
  mcp.py, oauth.py  MCP client (stdio and Streamable HTTP, both protocol generations) and OAuth with PKCE
  connectors.py     connector cards, health checks, metric pipes, find_tool, risk-gated calls
  conn_*.py         Stripe, Resend, Playwright, Buffer and Brave
  browser.py        browser_check
  llm.py            Ollama / OpenAI-compatible client with tool-call fallbacks
  roles.py          maps agents to presets and tool belts
  web.py            search fallback chain and page fetching
  emailer.py, workspace.py, db.py, bus.py, server.py, config.py
  static/           the dashboard (plain HTML, CSS and JavaScript)
tests/              unit and end-to-end tests, plus the fake model server and fake Telegram
docs/               the improvement plan and design documents; see docs/README.md
```
