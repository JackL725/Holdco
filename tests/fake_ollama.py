"""A fake Ollama / OpenAI-compatible server that scripts plausible agent behaviour.

Lets the whole app run end to end without a GPU. Each agent (recognised by the name in its system prompt)
follows a short script of tool calls; the step is the number of assistant turns so far.

    server = FakeModelServer(delay=0.05); server.start(); ...; server.stop()
    python tests/fake_ollama.py 11999   # standalone
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SITE = """<!doctype html><html><head><meta charset="utf-8"><title>PawPlan</title></head>
<body><h1>Meal plans your dog will actually eat</h1><p>Tell us about your dog.</p>
<form><input placeholder="you@email.com"><button>Join the waitlist</button></form></body></html>"""


def tc(_n, **args):
    return {"function": {"name": _n, "arguments": args}}


def script(messages: list[dict], tools: list[dict]):
    system = messages[0]["content"]
    user = messages[1]["content"]
    step = sum(1 for m in messages if m["role"] == "assistant")
    names = {t["function"]["name"] for t in tools}
    stamp = int(time.time() * 1000) % 100000
    if user.startswith("You are founding"):
        if "[partial]" in user:
            # A thin founding, the way the first real run went: the charter lands in a file through write_file,
            # update_charter is never called, no tasks are created and the CEO finishes with status partial.
            seq = [tc("write_file", path="docs/charter.md",
                      content="# PawPlan\n\n## One-liner\nWeekly meal plans for dogs.\n\n## Problem\nOwners guess.\n"),
                   tc("finish", summary="Drafted the charter in docs/charter.md; stopped before creating tasks.",
                      status="partial")]
            return seq[min(step, len(seq) - 1)]
        seq = [tc("web_search", query="dog meal plan subscription market"),
               tc("write_file", path="research/market.md", content="# Market\n\n- FACT: example (source: https://example.com)\n"),
               tc("update_charter", charter="# PawPlan\n\n## One-liner\nWeekly meal plans for dogs.\n",
                  company_name="PawPlan", one_liner="Weekly meal plans for dog owners", stage="building"),
               tc("create_task", title="Build landing page", assign_to="engineering", priority=1,
                  description="Waitlist page", deliverable="site/index.html", acceptance="- labelled form"),
               tc("create_task", title="Write launch post", assign_to="marketing", deliverable="marketing/launch.md"),
               tc("create_task", title="Find 20 leads", assign_to="sales", deliverable="sales/leads.md"),
               tc("create_task", title="Competitor pricing scan", assign_to="research", deliverable="research/competitors.md"),
               tc("create_task", title="Set up KPI tracking", assign_to="ops"),
               tc("report_to_owner", message="**Welcome!** PawPlan is founded. I need a company email account."),
               tc("finish", summary="Founded PawPlan: research, charter, 5 tasks.")]
        return seq[min(step, len(seq) - 1)]
    if user.startswith("Run a planning cycle"):
        seq = [tc("list_tasks"),
               tc("create_task", title=f"Follow up on leads batch {stamp}", assign_to="sales",
                  deliverable=f"sales/followups-{stamp}.md", priority=3),
               tc("finish", summary="Reviewed progress, queued follow-ups.")]
        return seq[min(step, len(seq) - 1)]
    if user.startswith("The owner is talking"):
        if step == 0 and "create_task" in names:
            return tc("create_task", title=f"Owner request {stamp}", assign_to="marketing", priority=1,
                      deliverable=f"marketing/request-{stamp}.md")
        return "Got it. I've queued that for Echo in marketing as a priority task."
    role = system.split(",")[0].replace("You are ", "")
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
                   tc("send_email", to="jo@happypaws.example", subject="Quick idea for Happy Paws",
                      body="Hi Jo,\n\nWe built PawPlan. Worth a look? Reply 'no' and I won't follow up.\n\nPawPlan"),
                   tc("finish", summary="Leads saved; first email queued.", deliverable="sales/leads.md")],
        "Scout": [tc("web_search", query="dog meal plan app pricing"),
                  tc("write_file", path="research/competitors.md", content="# Competitors\n\n| Name | Price |\n|---|---|\n"),
                  tc("remember", content="Competitors charge $9-15/month", kind="fact"),
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
