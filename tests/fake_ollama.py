"""A fake Ollama / OpenAI-compatible server that scripts plausible agent behaviour.

Lets the whole app run end to end without a GPU. Each agent (recognised by the name in its system prompt)
follows a short script of tool calls; the step is the number of assistant turns so far.

    server = FakeModelServer(delay=0.05); server.start(); ...; server.stop()
    python tests/fake_ollama.py 11999   # standalone
"""
from __future__ import annotations

import datetime
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SITE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PawPlan</title></head>
<body><h1>Meal plans your dog will actually eat</h1><p>Tell us about your dog.</p>
<form><input placeholder="you@email.com"><button>Join the waitlist</button></form></body></html>"""


def tc(_n, **args):
    return {"function": {"name": _n, "arguments": args}}


# A charter with every section planning.validate_charter requires (Phase 0.5).
CHARTER = """# PawPlan

## One-liner
Weekly meal plans for dogs.

## Customer
Dog owners who cook for their dogs, or want to, and feel unsure about portions and recipes.

## Problem
Owners guess portions and recipes, switch foods often and worry about allergies.

## Offer and price
A personalised weekly meal plan with a shopping list. Price to test: $9 per month.

## How we reach customers
Dog owner communities and groups, local dog parks, and a waitlist landing page shared where owners gather.

## Competitors
| Name | What they sell | How we differ |
|---|---|---|
| Ollie | fresh dog food delivered | we sell plans, not food |
| Spot and Tango | fresh dog food subscription | owners cook it themselves |
| Recipe blogs | free recipes | personal and weekly |

## Business model
Owners pay a monthly subscription for their plans; the margin per plan is an estimate until the first sales.

## Riskiest assumption
At least 5% of landing page visitors join the waitlist (pass threshold, an estimate to test).

## North Star metric
Meal plans that paying owners use each week.

## 30-day plan
- Week 1: landing page and conversations with dog owners
- Week 2: a first sample plan
- Week 3: waitlist push in two owner communities
- Week 4: compare signups with the threshold
"""

CRITIQUE = """# Critique

## Three strongest reasons this fails
1. Owners may not pay for plans while free recipes exist. Evidence: owner conversations and waitlist signups.
2. Reaching owners may cost more than a plan earns. Evidence: signups from two communities.
3. Vets may advise against home cooking. Evidence: what vets say in the first conversations.

## What to test first
The waitlist landing page with the price shown.
"""


# Phase 2: the brain the charter stage writes after update_charter (set_strategy needs all seven fields there).
STRATEGY = {"one_liner": "Weekly meal plans for dog owners, delivered as a printable plan.",
            "icp": "Dog owners who cook for their dogs and feel unsure about portions.",
            "problem": "Owners guess portions and recipes and worry about allergies.",
            "offer": "A personalised weekly meal plan with a shopping list.",
            "price": "$9 per month (to test).",
            "positioning": "The plan your vet would write, without the vet bill.",
            "voice": "Warm, practical, no hype."}


def _in_days(n: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=n)).isoformat()


def goal_ids(user: str) -> list[int]:
    """The goal numbers a prompt lists under its goals heading (founding plan stage, planning cycle)."""
    m = re.search(r"^(?:## )?Goals[^\n]*\n((?:#\d+ [^\n]*\n?)+)", user, re.M)
    return [int(x) for x in re.findall(r"^#(\d+) ", m.group(1), re.M)] if m else []


def with_goals(call: dict, user: str, n: int = 0, expected: str = "") -> dict:
    """Phase 2: a create_task call gains goal_id (the n-th goal the prompt lists) and expected. Holdco drops the
    arguments a tool does not declare (tools.clean_args), so the calls also work where create_task lacks them."""
    ids = goal_ids(user)
    args = call["function"]["arguments"]
    if ids:
        args["goal_id"] = ids[min(n, len(ids) - 1)]
    args["expected"] = expected or "moves its goal"
    return call


def founding_step(user: str, step: int):
    """Staged founding (Phase 0.5): each stage is its own run, recognised by "Stage: <name>" in the prompt."""
    stage = (re.search(r"Stage: (\w+)", user[:200]) or [None, "research"])[1]
    if "[partial]" in user:
        # A thin founding, the way the first real run went: the charter lands in a file through write_file during
        # research, update_charter is never called, no tasks are created and every stage finishes partial.
        seqs = {
            "research": [tc("write_file", path="docs/charter.md", content=CHARTER),
                         tc("finish", summary="Drafted the charter in docs/charter.md; stopped before creating tasks.",
                            status="partial")],
            "charter": [tc("finish", summary="The charter in docs/charter.md is saved; nothing to add.",
                           status="partial")],
            "critic": [tc("finish", summary="No critique written.", status="partial")],
            "plan": [tc("finish", summary="Stopped before creating tasks.", status="partial")],
        }
    else:
        seqs = {
            "research": [tc("web_search", query="dog meal plan subscription market"),
                         tc("write_file", path="research/market.md",
                            content="# Market\n\n- Dog owners who cook want simple weekly plans "
                                    "(source: https://www.akc.org/expert-advice/nutrition/)\n\n## Gaps\n- Prices\n"),
                         tc("finish", summary="Market research saved to research/market.md.")],
            "charter": [tc("read_file", path="research/market.md"),
                        tc("update_charter", charter=CHARTER, company_name="PawPlan",
                           one_liner="Weekly meal plans for dog owners", stage="building"),
                        tc("set_strategy", **STRATEGY),
                        tc("set_goal", title="200 waitlist signups", metric="waitlist_signups", target=200,
                           deadline=_in_days(45), owner_role="marketing"),
                        tc("set_goal", title="10 paying customers", metric="stripe.customers", target=10,
                           deadline=_in_days(60), owner_role="sales"),
                        tc("finish", summary="Charter saved; the strategy and two goals are in the brain.")],
            "critic": [tc("read_file", path="docs/charter.md"),
                       tc("write_file", path="docs/critique.md", content=CRITIQUE),
                       tc("finish", summary="Critique saved to docs/critique.md.", deliverable="docs/critique.md")],
            "plan": [with_goals(tc("create_task", title="Build landing page", assign_to="engineering", priority=1,
                                   description="Waitlist page", deliverable="site/index.html",
                                   acceptance="- labelled form"), user, 0, "a page that collects waitlist signups"),
                     with_goals(tc("create_task", title="Write launch post", assign_to="marketing",
                                   deliverable="marketing/launch.md"), user, 0, "30 waitlist signups"),
                     with_goals(tc("create_task", title="Find 20 leads", assign_to="sales",
                                   deliverable="sales/leads.md"), user, 1, "20 pet shops to contact"),
                     with_goals(tc("create_task", title="Competitor pricing scan", assign_to="research",
                                   deliverable="research/competitors.md"), user, 1, "a price we can defend"),
                     with_goals(tc("create_task", title="Set up KPI tracking", assign_to="ops"), user, 0,
                                "signups counted every day"),
                     tc("report_to_owner", message="**Welcome!** PawPlan is founded. I need a company email account."),
                     tc("finish", summary="Founded PawPlan: research, charter, critique, 5 tasks.")],
        }
    seq = seqs.get(stage) or seqs["research"]
    return seq[min(step, len(seq) - 1)]


PAY_LINK_RE = re.compile(r"https?://[^\s)]+/pay/plink_\w+")


def payment_link_step(messages: list[dict], step: int):
    """Phase 1 gate (docs/phases/phase-1-connect.md, the test_22 walkthrough, numbered test_27 in test_e2e.py):
    Forge loads the Stripe tool, creates the link, opens it with browser_check and hands it off."""
    tool_texts = [str(m.get("content") or "") for m in messages if m.get("role") == "tool"]
    link = next((m.group(0) for t in reversed(tool_texts) for m in [PAY_LINK_RE.search(t)] if m), "")
    seq = [tc("find_tool", query="stripe payment link"),
           tc("stripe__create_payment_link", name="Starter plan", amount_usd=19, recurring_interval="month"),
           tc("browser_check", path_or_url=link or "no link came back"),
           tc("finish", summary="Starter plan payment link created", status="done", deliverable=link,
              evidence="stripe__create_payment_link returned the link; browser_check returned 200")]
    return seq[min(step, len(seq) - 1)]


# Phase 2 rhythms: a brief may only use numbers from its data block (write_brief refuses others), so the bodies
# are built from the "## Data for this brief" JSON in the task prompt and state no number of their own.
BRIEF_KINDS = {"Evening brief": "daily", "Weekly review": "weekly", "Monthly strategy check": "monthly"}
DATA_RE = re.compile(r"^## Data for this brief \(the only numbers you may use\)\n(.*?)\n\n### What to do\n",
                     re.M | re.S)


def data_block(user: str) -> dict:
    """The rhythm task's data block (rhythms.task_data_section), or {}."""
    m = DATA_RE.search(user)
    try:
        return json.loads(m.group(1)) if m else {}
    except ValueError:
        return {}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())}


def brief_body(kind: str, data: dict) -> str:
    """A brief in the five headings, every number copied from the data block."""
    moved = [f"- {m.get('name')}: {m['value']} [{m.get('source')}]" for m in (data.get("metrics") or [])[:6]
             if isinstance(m, dict) and m.get("value") is not None]
    moved += [f"- {g.get('title')}: {g['current']} of {g['target']}" for g in data.get("goals") or []
              if isinstance(g, dict) and g.get("current") is not None and g.get("target") is not None]
    shipped = [f"- {t.get('title')}" + (f" -> {t['deliverable']}" if t.get("deliverable") else "")
               for t in (data.get("shipped") or [])[:5] if isinstance(t, dict)]
    for group in (data.get("done_per_goal") or [])[:3]:
        shipped += [f"- {t.get('title')}" for t in (group.get("tasks") or [])[:3] if isinstance(t, dict)]
    stuck = (data.get("stuck") or {}).get("tasks") or []
    needed = [f"- {d.get('question')}" + (f" (recommended: {d['recommended']})" if d.get("recommended") else "")
              for d in (data.get("decisions_needed") or [])[:3] if isinstance(d, dict)]
    focus = {"daily": "Tomorrow's focus", "weekly": "Next week's focus", "monthly": "Next month's focus"}[kind]
    parts = ["## What moved", "\n".join(moved) or "No connector numbers moved.",
             "## What shipped", "\n".join(shipped) or "Nothing new.",
             "## What is stuck", f"{len(stuck)} tasks are blocked or waiting." if stuck else "Nothing.",
             "## Decisions needed", "\n".join(needed) or "None.",
             f"## {focus}", "Talk to dog owners."]
    owner = [f"- #{t.get('id')} {t.get('title')}" for t in (data.get("owner_tasks_without_goal") or [])
             if isinstance(t, dict)]
    if owner:
        parts += ["## Owner tasks without a goal", "\n".join(owner)]
    return "\n".join(parts)


def weekly_calls(data: dict) -> list[dict]:
    """The weekly review: conclude each experiment that is due, give each goal-less task the goal its title is
    about or cancel it, then the board memo."""
    calls = [tc("conclude_experiment", experiment_id=e["id"], verdict="inconclusive",
                result="No connector metric moved for it this week.")
             for e in (data.get("experiments_due") or [])[:3] if isinstance(e, dict)]
    goals = [g for g in data.get("goals") or [] if isinstance(g, dict)]
    for t in (data.get("goalless_tasks") or [])[:6]:
        if not isinstance(t, dict):
            continue
        g = next((g for g in goals if _words(t.get("title")) & (_words(g.get("title")) | _words(g.get("metric")))),
                 None)
        if g:
            calls.append(tc("update_task", task_id=t["id"], goal_id=g["id"], note=f"Serves goal #{g['id']}."))
        else:
            calls.append(tc("update_task", task_id=t["id"], status="cancelled", note="Serves no goal."))
    return calls + [tc("write_brief", kind="weekly", body=brief_body("weekly", data))]


def rhythm_step(user: str, step: int):
    """The CEO's and the analyst's rhythm tasks (Phase 2): the morning plan writes docs/daily-plan.md; the briefs
    are stored with write_brief; the weekly review cleans up goals first; the monthly check files its keep, change
    or kill decision first."""
    title = (re.search(r"^Your assignment: task #\d+: (.+)$", user, re.M) or [None, ""])[1].strip()
    if title == "Morning plan":
        seq = [tc("write_file", path="docs/daily-plan.md",
                  content="# Daily plan\n\n## Today\n- Talk to dog owners\n- Ship the waitlist page\n"),
               tc("finish", summary="Morning plan written.", deliverable="docs/daily-plan.md")]
    elif title in BRIEF_KINDS:
        kind = BRIEF_KINDS[title]
        data = data_block(user)
        if kind == "weekly":
            seq = weekly_calls(data)
        elif kind == "monthly":
            name = (re.search(r"Keep, change or kill ([^?\n]+)\?", user) or [None, "the company"])[1]
            seq = [tc("ask_decision", question=f"Keep, change or kill {name}?",
                      options="Keep\nChange: focus on dog walkers\nKill (pause the company)", recommended="Keep",
                      reversible=False, context="The month's numbers are in the monthly brief."),
                   tc("write_brief", kind=kind, body=brief_body(kind, data))]
        else:
            seq = [tc("write_brief", kind=kind, body=brief_body(kind, data))]
        seq.append(tc("finish", summary=f"{title} written.", status="done"))
    else:
        return None
    return seq[min(step, len(seq) - 1)]


DECIDE_TITLE = "Ask the owner where to launch first"


def decision_step(user: str, step: int):
    """Phase 2 inbox walkthrough: the CEO asks the owner to decide (options, a recommendation, a deadline) and waits;
    once the answer reopens the task ("[Update] Owner decided"), it finishes done."""
    if "[Update] Owner decided" in user:
        return tc("finish", summary="The owner made the call; the team follows it.", status="done")
    seq = [tc("ask_decision", question="Where should PawPlan launch first: the r/dogs community or the PawTalk forum?",
              options="r/dogs\nPawTalk forum", recommended="r/dogs", deadline_hours=48, reversible=True,
              context="Picking one community keeps the first week focused."),
           tc("finish", summary="Asked the owner where to launch first.", status="waiting")]
    return seq[min(step, len(seq) - 1)]


def script(messages: list[dict], tools: list[dict]):
    system = messages[0]["content"]
    user = messages[1]["content"]
    step = sum(1 for m in messages if m["role"] == "assistant")
    names = {t["function"]["name"] for t in tools}
    stamp = int(time.time() * 1000) % 100000
    if user.startswith("You are founding"):
        return founding_step(user, step)
    if user.startswith("Run a planning cycle"):
        seq = [tc("list_tasks"),
               with_goals(tc("create_task", title=f"Follow up on leads batch {stamp}", assign_to="sales",
                             deliverable=f"sales/followups-{stamp}.md", priority=3), user, 0, "2 replies"),
               tc("finish", summary="Reviewed progress, queued follow-ups.")]
        return seq[min(step, len(seq) - 1)]
    if user.startswith("The owner is talking"):
        if step == 0 and "create_task" in names:
            return tc("create_task", title=f"Owner request {stamp}", assign_to="marketing", priority=1,
                      deliverable=f"marketing/request-{stamp}.md")
        return "Got it. I've queued that for Echo in marketing as a priority task."
    role = system.split(",")[0].replace("You are ", "")
    rhythm = rhythm_step(user, step)
    if rhythm:
        return rhythm
    if role == "Atlas" and f": {DECIDE_TITLE}\n" in user:
        return decision_step(user, step)
    if role == "Forge" and "Create a payment link for the Starter plan" in user:
        return payment_link_step(messages, step)
    if "Echo through the test MCP server" in user:   # Phase 1: a tool loaded from an OAuth-signed-in MCP server
        seq = [tc("find_tool", query="echo"),
               tc("fakemcp__echo", text="hello from holdco"),
               tc("finish", summary="Echoed a line through the test MCP server.")]
        return seq[min(step, len(seq) - 1)]
    if role == "Forge" and "Fix form labels" in user:
        seq = [tc("finish", summary="Fixed labels"),  # lazy: should be pushed back by the deliverable check
               tc("write_file", path="site/index.html",
                  content=SITE.replace("<input", '<label for="e">Email</label><input id="e"')),
               tc("finish", summary="Fixed labels", deliverable="site/index.html")]
        return seq[min(step, len(seq) - 1)]
    seqs = {
        "Forge": [tc("write_file", path="site/index.html", content=SITE), tc("deploy_site"),
                  tc("finish", summary="Landing page built; deploy queued.", deliverable="site/index.html")],
        "Echo": [tc("write_file", path="marketing/launch.md", content="# Launch post\n\nMeet PawPlan, weekly meal plans."),
                 tc("social_post", platform="x", content="Meet PawPlan: weekly meal plans your dog will love."),
                 tc("finish", summary="Launch post drafted and queued.", deliverable="marketing/launch.md")],
        "Hunter": [tc("write_file", path="sales/leads.md", content="| Name | Company | Source |\n|---|---|---|\n| Jo | Happy Paws | https://example.com |\n"),
                   tc("send_email", to="jo@happypaws.co", subject="Quick idea for Happy Paws",
                      body="Hi Jo,\n\nWe built PawPlan. Worth a look? Reply 'no' and I won't follow up.\n\nPawPlan"),
                   tc("finish", summary="Leads saved; first email queued.", deliverable="sales/leads.md")],
        "Scout": [tc("web_search", query="dog meal plan app pricing"),
                  tc("write_file", path="research/competitors.md", content="# Competitors\n\n| Name | Price |\n|---|---|\n"),
                  tc("add_lesson", text="Competitors charge $9-15/month; lead with the plan, not the price."),
                  tc("finish", summary="Competitor scan saved.", deliverable="research/competitors.md")],
        "Ledger": [tc("record_metric", name="waitlist_signups", value=0, note="baseline"),
                   tc("request_purchase", item="pawplan.co domain", amount_usd=12, vendor="Namecheap", reason="Brand domain"),
                   tc("run_code", language="python", code="print(2+2)"),
                   tc("finish", summary="KPIs set up; domain purchase requested.")],
        "Sentry": [tc("audit_page", path="site/index.html"), tc("review_text", path="site/index.html", kind="landing"),
                   tc("check_contrast", foreground="#888888", background="#ffffff"),
                   tc("write_file", path="docs/reviews.md", content="# Reviews\n\nLanding page: needs changes.\n"),
                   tc("create_task", title="Fix form labels on landing page", assign_to="engineering",
                      deliverable="site/index.html", acceptance="- every input has a label"),
                   tc("finish", summary="Reviewed landing page.", deliverable="docs/reviews.md")],
        "Repeater": [tc("web_search", query="same thing")] * 5,
    }
    seq = seqs.get(role, [tc("finish", summary="Done.")])
    return seq[min(step, len(seq) - 1)]


# Phase 0.5: deterministic embeddings for POST /api/embed. A bag of words hashed into 64 buckets, with a few
# synonyms folded together so a query can match a document that shares no words with it ("fee" finds "price").
EMBED_DIM = 64
EMBED_FAIL_MODEL = "broken-embed"
EMBED_SYNONYMS = {"fee": "price", "fees": "price", "cost": "price", "costs": "price", "pricing": "price",
                  "prices": "price", "charge": "price", "charges": "price", "tariff": "price",
                  "customers": "customer", "clients": "customer", "client": "customer", "buyers": "customer"}


def embed(text: str) -> list[float]:
    import math
    import re
    import zlib
    v = [0.0] * EMBED_DIM
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        w = EMBED_SYNONYMS.get(w, w)
        v[zlib.crc32(w.encode()) % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


IDEAS = {"ideas": [
    {"name": "PawPlan", "one_liner": "Meal plans for dogs", "customer": "Dog owners", "offer": "$9/mo",
     "why_it_fits": "You love dogs", "first_week": "Landing page + waitlist"},
    {"name": "ShelfLife", "one_liner": "Pantry tracker for cafés", "customer": "Café owners", "offer": "$19/mo",
     "why_it_fits": "Low build cost", "first_week": "Interviews + mockups"},
    {"name": "QuietHours", "one_liner": "Focus-music newsletter", "customer": "Remote workers", "offer": "$5/mo",
     "why_it_fits": "Content-led", "first_week": "Write 3 issues"}]}


class FakeModelServer:
    """models: gpt-oss:20b (native tools), notools:latest (rejects tools -> JSON mode), local-model (OpenAI API)."""

    def __init__(self, port: int = 0, delay: float = 0.05):
        self.delay = delay
        self.calls: list[dict] = []
        # What GET /api/ps reports (Ollama's "loaded models"). size_vram == size means fully on the GPU;
        # tests lower size_vram to simulate a model spilling into system RAM.
        self.ps: list[dict] = [{"name": "gpt-oss:20b", "model": "gpt-oss:20b",
                                "size": 14_000_000_000, "size_vram": 14_000_000_000}]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                try:
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the app was restarted mid-request (restart test)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": "gpt-oss:20b"}, {"name": "notools:latest"}]})
                if self.path == "/v1/models":
                    return self._json(200, {"data": [{"id": "local-model"}]})
                if self.path == "/api/ps":
                    return self._json(200, {"models": outer.ps})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append({"path": self.path, "model": body.get("model"), "think": body.get("think"),
                                    "options": body.get("options"), "temperature": body.get("temperature"),
                                    "ntools": len(body.get("tools") or [])})
                if self.path == "/api/embed":   # Phase 0.5: meaning-based search
                    if body.get("model") == EMBED_FAIL_MODEL:
                        return self._json(500, {"error": "embedding model crashed"})
                    inputs = body.get("input")
                    inputs = [inputs] if isinstance(inputs, str) else list(inputs or [])
                    return self._json(200, {"model": body.get("model"), "embeddings": [embed(t) for t in inputs]})
                time.sleep(outer.delay)
                msgs, tools = body["messages"], body.get("tools") or []
                ping = "Call the ping tool" in msgs[-1]["content"]
                if self.path == "/v1/chat/completions":
                    r = tc("ping", message="ok") if ping else script(msgs, tools)
                    if isinstance(r, str):
                        msg = {"role": "assistant", "content": r}
                    else:
                        msg = {"role": "assistant", "content": None, "tool_calls": [
                            {"id": f"call_{len(msgs)}", "type": "function",
                             "function": {"name": r["function"]["name"], "arguments": json.dumps(r["function"]["arguments"])}}]}
                    return self._json(200, {"choices": [{"message": msg}], "usage": {"total_tokens": 500}})
                if body.get("format") == "json" and not tools:
                    return self._json(200, {"message": {"role": "assistant", "content": json.dumps(IDEAS)},
                                            "prompt_eval_count": 50, "eval_count": 120})
                if body["model"] == "notools:latest" and tools:
                    return self._json(400, {"error": "registry.ollama.ai/library/notools does not support tools"})
                r = tc("ping", message="ok") if ping else script(msgs, tools)
                if isinstance(r, str):
                    msg = {"role": "assistant", "content": r}
                elif body["model"] == "notools:latest":   # prompt-based tool calling
                    msg = {"role": "assistant", "content": "Sure.\n```json\n" + json.dumps(
                        {"tool": r["function"]["name"], "args": r["function"]["arguments"]}) + "\n```"}
                else:
                    msg = {"role": "assistant", "content": "", "thinking": "planning...", "tool_calls": [r]}
                self._json(200, {"message": msg, "prompt_eval_count": 900, "eval_count": 80, "done": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    srv = FakeModelServer(int(sys.argv[1]) if len(sys.argv) > 1 else 11999, delay=0.25)
    print(f"Fake model server on {srv.url}")
    srv.httpd.serve_forever()
