"""The verification gate (Phase 3, docs/phases/phase-3-verify.md section 3): before a task may be marked done, the
work is checked by its kind and by the tool checks listed in task.checks. Also the compliance rules every email must
pass when it is queued, and the global do-not-email list.

- web page: the deliverable is a page under site/ (or a checks line says browser_check). browser_check must report
  HTTP 200, no console errors, a target on every form, no broken link and the phone screenshot. Its static fallback
  counts only while the playwright connector is not enabled: 0 audit blockers, a target on every form and no broken
  same-site link. judge_browser_check is the one copy of this rule; Holdco Bench's browser_check_pass uses it too.
- code: the deliverable is under code/, a test file under code/ was written in this task, or a checks line says
  run_tests. The tests must exit 0: in the sandbox, else through the run_code policy (approve queues them, and the
  owner's run of them within 24 h counts).
- payment link: the deliverable, summary or evidence names a Stripe payment link; conn_stripe retrieves it, active.
- outreach: the run queued or sent an email; every one passes outreach_check again.
- research: the deliverable is under research/; it cites URLs, and this task fetched every one.

tools.send_email runs outreach_check before actions.gate, so no trust level or auto policy skips the rules; the gate
re-checks at finish because the owner can edit a queued email and the do-not-email list can change. Results go to
tasks.verification as a JSON list of {kind, passed, detail, mode}; summary_line turns them into the "[gate: ...]"
note of the CEO's review. A 20B model reads every failure, so each one says what is wrong and what to do. A bug in a
check never stops work: it is recorded as "not checked" (fail open, like truth.py's guards). outreach_check is not
wrapped: when it breaks, send_email reports the error and nothing is queued.
"""
from __future__ import annotations

import email.utils
import json
import re

from . import actions, checks, config, db, web, workspace

WEB_SUFFIXES = (".html", ".htm")
TEST_TIMEOUT = 180                 # seconds the gate's own test run may take
APPROVED_RUN_MINUTES = 24 * 60     # an owner-approved test run this recent still counts
DEFAULT_TESTS = "python -m unittest discover -s code -t ."
# The default test command as a script, so it runs the same way in the container and on this PC (whatever the
# interpreter is called there). It exits 5 when no test ran, like unittest on Python 3.12+ and pytest.
TEST_SCRIPT = (f"# Holdco verification gate: {DEFAULT_TESTS}\n"
               "import sys\n"
               "import unittest\n"
               "\n"
               "argv = ['python -m unittest', 'discover', '-s', 'code', '-t', '.']\n"
               "program = unittest.main(module=None, argv=argv, exit=False)\n"
               "result = program.result\n"
               "sys.exit(5 if not result.testsRun else (0 if result.wasSuccessful() else 1))\n")
OPT_OUT_LINE = "Reply 'stop' and we won't email you again."
# review_text issues that stop an email (the opt-out line only for commercial email; replies are exempt)
HARD_ISSUES = {
    "More than one link": "more than one link: keep one link or none",
    "Looks like a testimonial": "it looks like a testimonial or a quote with attribution: remove it unless it is "
                                "real and the person agreed to be quoted",
    "Compliance claims": "compliance claims ({hits}): remove them unless the company has evidence",
}
# Tool checks (task.checks) also stop on these review_text issues.
CHECK_HARD = ("No opt-out line", "More than one link", "Looks like a testimonial", "Compliance claims",
              "Marketing emails need an unsubscribe line")
# EU country-code TLDs (and .eu). contacts has no country column, so the address's TLD and the consent field are
# the only signals the gate has; the sales preset's prompt rules cover the rest.
EU_TLDS = {
    "at": "Austria", "be": "Belgium", "bg": "Bulgaria", "hr": "Croatia", "cy": "Cyprus", "cz": "Czechia",
    "dk": "Denmark", "ee": "Estonia", "fi": "Finland", "fr": "France", "de": "Germany", "gr": "Greece",
    "hu": "Hungary", "ie": "Ireland", "it": "Italy", "lv": "Latvia", "lt": "Lithuania", "lu": "Luxembourg",
    "mt": "Malta", "nl": "Netherlands", "pl": "Poland", "pt": "Portugal", "ro": "Romania", "sk": "Slovakia",
    "si": "Slovenia", "es": "Spain", "se": "Sweden", "eu": "the EU",
}
_NO_CONSENT = {"", "no", "none", "false", "0", "n/a", "na", "unknown", "-", "not given", "pending"}
_URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+", re.I)


# ---------------------------------------------------------------- small helpers

def _addr(s: str | None) -> str:
    return (email.utils.parseaddr(s or "")[1] or "").strip().lower()


def _one_line(s, n: int = 160) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def _result(kind: str, passed: bool, detail: str, mode: str = "") -> dict:
    return {"kind": kind, "passed": bool(passed), "detail": detail, "mode": mode}


def _rel(target: str) -> str:
    """A deliverable that names a path, normalised (site/index.html), or "" when it is not a path."""
    d = (target or "").strip().strip("`'\"").replace("\\", "/")
    d = re.sub(r"^(?:\./)+", "", d).lstrip("/")
    if not d or " " in d or not re.fullmatch(r"[\w./-]+", d):
        return ""
    return d


def _url_key(url: str) -> str:
    """A cited URL in comparable form: trailing punctuation, the fragment and a trailing slash do not count."""
    from . import truth   # imported here: truth imports tools, which imports this module
    return truth._url_key((url or "").rstrip(".,;:!?*_'\"”’"))


def _cited_urls(text: str) -> list[str]:
    out: list[str] = []
    for u in _URL_RE.findall(text or ""):
        u = u.rstrip(".,;:!?*_'\"”’")
        if u not in out:
            out.append(u)
    return out


def _written_paths(ctx) -> set[str]:
    """Files written while working on this task: this session and earlier attempts (as tools._written_for_task)."""
    paths = set(ctx.written)
    task = ctx.task or {}
    if task.get("id"):
        for e in db.q("SELECT data FROM events WHERE company_id=? AND kind='file' AND agent_id IS NOT NULL "
                      "AND data LIKE ?", (ctx.company["id"], f'%"task_id": {task["id"]}}}')):
            path = (db.loads(e["data"], {}) or {}).get("path", "")
            if path:
                paths.add(path)
    return paths


def _run_started(ctx) -> str:
    row = db.one("SELECT started_at FROM runs WHERE id=?", (ctx.run_id,)) if ctx.run_id else None
    return (row or {}).get("started_at") or ""


def _check_lines(text: str) -> list[tuple[str, str]]:
    """(tool, arguments) for each line of task.checks, e.g. ('browser_check', 'site/index.html')."""
    out = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip("`").strip()
        if not s:
            continue
        tool, _, rest = s.partition(" ")
        out.append((tool.rstrip(":").lower(), rest.strip()))
    return out


def _args(rest: str) -> list[str]:
    return [a.strip("`'\"") for a in rest.split() if a.strip("`'\"")]


# ---------------------------------------------------------------- the gate

def run(ctx, target: str = "", summary: str = "", evidence: str = "") -> list[dict]:
    """Every kind gate that applies, then the task's checks lines. Each result: {kind, passed, detail, mode}."""
    task = ctx.task or {}
    if task.get("id"):
        task = db.get_task(task["id"]) or task
    lines = _check_lines(task.get("checks") or "")
    results: list[dict] = []
    pages: set[str] = set()   # pages already checked, so a browser_check line for the deliverable runs once
    kinds = (("web page", lambda: _web_kind(ctx, target, pages)),
             ("code", lambda: _code_kind(ctx, task, target, lines)),
             ("payment link", lambda: _payment_kind(ctx, target, summary, evidence)),
             ("outreach", lambda: _outreach_kind(ctx, task)),
             ("research", lambda: _research_kind(ctx, target)))
    for kind, fn in kinds:
        try:
            results += fn()
        except Exception as e:   # a bug in a check never stops the work
            results.append(_result(kind, True, f"not checked: {type(e).__name__}: {e}", "not checked"))
    for tool, rest in lines:
        if tool == "run_tests":
            continue   # the code kind ran it
        try:
            r = _check_line(ctx, tool, rest, pages)
        except Exception as e:
            r = _result(tool, True, f"not checked: {type(e).__name__}: {e}", "not checked")
        if r:
            results.append(r)
    return results


def finish_problem(ctx, target: str, summary: str, evidence: str) -> str | None:
    """tools.finish(status="done") on a task: run the gate, store the results in tasks.verification and return
    "<kind>: <detail>" for the first failure, or None when everything passes."""
    task = ctx.task or {}
    if not task.get("id"):
        return None
    ctx.verify_waiting_on = None
    results = run(ctx, target, summary, evidence)
    try:
        db.update("tasks", task["id"], verification=json.dumps(results, ensure_ascii=False))
    except Exception as e:   # the stored copy is for the dashboard and the CEO; the answer below still counts
        print(f"verification not stored: {e}")
    failed = [r for r in results if not r["passed"]]
    ctx.verify_failed = failed
    if not failed:
        return None
    first = failed[0]
    others = list(dict.fromkeys(r["kind"] for r in failed[1:]))
    tail = f" (also failing: {', '.join(others)})" if others else ""
    return f"{first['kind']}: {first['detail']}{tail}"


def waiting_request(ctx) -> int | None:
    """The run_code request the gate queued for the owner in its last check (the tests need their approval), so
    tools.finish can end the task 'waiting' on it instead of 'partial'. None when the gate queued nothing."""
    return getattr(ctx, "verify_waiting_on", None)


def only_waiting(ctx) -> bool:
    """The gate's last check failed only because the tests wait for the owner's approval: nothing the agent can fix,
    so tools.finish ends the task 'waiting' at once instead of pushing back."""
    failed = getattr(ctx, "verify_failed", None) or []
    return bool(failed) and waiting_request(ctx) is not None and all(r.get("mode") == "waiting" for r in failed)


def results_of(task: dict) -> list[dict]:
    raw = task.get("verification") if "verification" in task else \
        (db.one("SELECT verification FROM tasks WHERE id=?", (task.get("id"),)) or {}).get("verification")
    out = db.loads(raw, []) if raw else []
    return [r for r in out if isinstance(r, dict)] if isinstance(out, list) else []


def _short(detail: str) -> str:
    s = _one_line(detail, 400)
    cut = min([i for i in (s.find(". "), s.find("; ")) if i > 0] or [len(s)])
    s = s[:cut].rstrip(". ")
    return s if len(s) <= 90 else s[:87].rstrip() + "..."


def summary_line(task: dict) -> str:
    """'[gate: web page PASS (browser); outreach FAIL: no opt-out line]' for the CEO's review, or ''."""
    results = results_of(task or {})
    if not results:
        return ""
    parts = []
    for r in results[:8]:
        kind, mode = r.get("kind") or "check", r.get("mode") or ""
        if mode == "not checked":
            parts.append(f"{kind} not checked")
        elif r.get("passed"):
            parts.append(f"{kind} PASS" + (f" ({mode})" if mode else ""))
        else:
            parts.append(f"{kind} FAIL: {_short(r.get('detail') or '')}")
    if len(results) > 8:
        parts.append(f"+{len(results) - 8} more")
    return "[gate: " + "; ".join(parts) + "]"


# ---------------------------------------------------------------- web page

def _playwright_enabled(company: dict) -> bool:
    try:
        from . import connectors   # imported here: connectors imports tools, which imports this module
        inst = connectors.instance("playwright", company)
    except Exception:
        return False
    return bool(inst and inst.get("enabled", 1))


def _page_key(target: str) -> str:
    """site/<path> for a page of the company site (browser_check reads 'index.html' as site/index.html), else the
    URL itself."""
    t = (target or "").strip()
    if re.match(r"^https?://", t, re.I):
        return t
    rel = _rel(t)
    return rel if rel.lower().startswith("site/") else "site/" + rel


def _web_kind(ctx, target: str, pages: set) -> list[dict]:
    rel = _rel(target)
    if not rel.lower().startswith("site/") or not rel.lower().endswith(WEB_SUFFIXES):
        return []
    pages.add(_page_key(rel))
    return [_web_page(ctx, rel)]


def _web_page(ctx, target: str) -> dict:
    from . import browser   # imported here: browser imports tools, which imports this module
    try:
        out = browser.browser_check(ctx, target, True)
    except Exception as e:   # ToolError, PathError, WebError: the page cannot be opened at all
        return _result("web page", False, f"browser_check could not open {target}: {e}")
    ok, problems, mode, facts = judge_browser_check(ctx.company, out)
    if ok:
        return _result("web page", True, f"{target}: {facts}", mode)
    return _result("web page", False, f"{target}: {'; '.join(problems).rstrip('.')}. browser_check {target} shows "
                                      "the full result", mode)


def judge_browser_check(company: dict, out: str) -> tuple[bool, list[str], str, str]:
    """The section 3 web page rule on a browser_check result: (passed, problems, mode, what passed)."""
    lines = [ln.rstrip() for ln in str(out or "").replace("\r\n", "\n").split("\n")]
    first = lines[0] if lines else ""
    site = first.startswith("Browser check (Playwright) of site/") or any(ln.startswith("Audit:") for ln in lines)
    get = re.search(r"^GET \S+ -> (\d{3})\b", out or "", re.M)
    status = int(get.group(1)) if get else 0
    broken = [ln[len("- broken: "):] for ln in lines if ln.startswith("- broken: ")]
    problems: list[str] = []
    facts: list[str] = []
    if first.startswith("Static check only"):
        mode = "static"
        why = first.partition(":")[2].strip()
        if _playwright_enabled(company):
            return False, [f"browser_check fell back to a static check ({why}), which does not count while the "
                           "Playwright connection is set up. Run browser_check again; if Playwright keeps failing, "
                           "finish with status 'partial' and say so"], mode, ""
        if site:
            audit = next((ln for ln in lines if ln.startswith("Audit:")), "")
            m = re.match(r"Audit: (\d+) blocker\(s\)", audit)
            if not m:
                problems.append(f"the page audit did not run ({audit[6:].strip() or 'no audit in the result'})")
            elif int(m.group(1)):
                blockers = [ln[len("BLOCKER: "):] for ln in lines if ln.startswith("BLOCKER: ")]
                problems.append(f"{m.group(1)} audit blocker(s): " + " ".join(blockers[:4]))
            else:
                facts.append("0 audit blockers")
        elif status != 200:
            problems.append(f"the page answered HTTP {status or 'nothing'}, not 200")
        # The Forms line is read from the page's HTML, so the static check sees a form that sends its entries nowhere
        # as well as the browser does: the rule holds in both modes (a page must not pass only because Playwright is
        # not set up).
        _forms(out, lines, problems, facts)
    elif first.startswith("Browser check (Playwright)"):
        mode = "browser"
        if status != 200:
            problems.append(f"the page did not load: {get.group(0) if get else 'no GET line in the result'}")
        else:
            facts.append("HTTP 200")
        console = re.search(r"^Console errors: (\d+|none)", out or "", re.M)
        n = 0 if not console or console.group(1) == "none" else int(console.group(1))
        if n:
            at = next(i for i, ln in enumerate(lines) if ln.startswith("Console errors:"))
            errs = [ln[2:] for ln in lines[at + 1:at + 4] if ln.startswith("- ") and not ln.startswith("- broken")]
            problems.append(f"{n} console error(s): " + " | ".join(errs) if errs else f"{n} console error(s)")
        else:
            facts.append("no console errors")
        _forms(out, lines, problems, facts)
        if site:
            shots = re.search(r"^Screenshots: (.*)$", out or "", re.M)
            entries = [s.strip() for s in shots.group(1).split(",")] if shots else []
            mobile = next((s for s in entries if s.endswith("-mobile.png")), "")
            if not mobile or not (workspace.root(company) / mobile).is_file():
                said = next((s for s in entries if "phone" in s or "mobile" in s), "")
                if not said.startswith("no phone-sized screenshot"):
                    said = "no phone-sized screenshot" + (f" ({said})" if said else "")
                problems.append(f"{said}: run browser_check again with mobile true")
            else:
                facts.append(f"phone screenshot {mobile}")
    else:
        return False, [f"browser_check gave no result the gate can read ({_one_line(first, 120)})"], "", ""
    if broken:
        problems.append(f"{len(broken)} broken link(s): " + ", ".join(broken[:5]))
    elif site:
        facts.append("no broken links")
    if mode == "static":
        facts.append(f"static check: {first.partition(':')[2].strip()}")
    return not problems, problems, mode, ", ".join(facts)


def _forms(out: str, lines: list[str], problems: list[str], facts: list[str]) -> None:
    """Every form on the page has a target (an action or data-netlify), from browser_check's Forms line."""
    forms = re.search(r"^Forms: (\d+) found, (\d+) with a target", out or "", re.M)
    if forms and forms.group(1) != forms.group(2):
        missing = [re.sub(r": submissions go nowhere$", "", ln[2:]) for ln in lines if re.match(r"- form \d+", ln)]
        problems += [f"{m} (submissions go nowhere)" for m in missing] or \
            [f"{int(forms.group(1)) - int(forms.group(2))} form(s) have no action or data-netlify attribute"]
    elif forms and forms.group(1) != "0":
        facts.append(f"{forms.group(1)} form(s) with a target")


# ---------------------------------------------------------------- code

def _code_kind(ctx, task: dict, target: str, lines: list) -> list[dict]:
    rel = _rel(target)
    commands = [rest for tool, rest in lines if tool == "run_tests" and rest]
    applies = bool(commands) or rel.lower() == "code" or rel.lower().startswith("code/") or _tests_written(ctx)
    if not applies:
        return []
    return [_run_tests(ctx, task, cmd) for cmd in (commands[:3] or [None])]


def _tests_written(ctx) -> bool:
    """A test file under code/ (code/**/test_*.py or code/**/*_test.py) was written in this task."""
    written = _written_paths(ctx)
    return any(p.startswith("code/") and re.search(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$", p) for p in written)


def _gate_runs(ctx, task: dict, lang: str, code: str) -> list[dict]:
    """This task's run_code requests for exactly this test run, newest first."""
    out = []
    for r in db.q("SELECT * FROM approvals WHERE company_id=? AND task_id=? AND action='run_code' ORDER BY id DESC",
                  (ctx.company["id"], task["id"])):
        p = db.loads(r["payload"], {}) or {}
        if p.get("code") == code and (p.get("language") or "python") == lang:
            out.append(r)
    return out


def _run_tests(ctx, task: dict, command: str | None) -> dict:
    from . import sandbox
    lang, code, label = ("python", TEST_SCRIPT, DEFAULT_TESTS) if command is None else ("shell", command, command)
    if db.effective_policy(ctx.company, "run_code") == "off":   # the owner's "off" holds even for the container
        return _off(label)
    runtime = sandbox.available()
    if runtime:
        return _test_outcome(sandbox.run(ctx.company, lang, code, TEST_TIMEOUT), label, runtime)
    # No container: the owner's run_code policy decides, as for any code an agent wants to run on this PC.
    prior = _gate_runs(ctx, task, lang, code)
    last = prior[0] if prior else None
    if last and last["status"] in ("pending", "running"):
        return _waiting(ctx, last["id"], label)
    recent = last and db.minutes_since(last.get("decided_at")) <= APPROVED_RUN_MINUTES
    if recent and last["status"] == "done":
        r = _test_outcome(last["result"], label, f"approved #{last['id']}")
        if r["passed"] or not any(p.startswith("code/") for p in ctx.written):
            return r   # a failed run counts until this session changes the code; then the tests run again
    if recent and last["status"] == "rejected":
        return _result("code", False, f"the owner rejected request #{last['id']} to run the tests ({label}). "
                                      "Finish with status 'partial' and say the tests have not run")
    payload = {"language": lang, "code": code, "timeout": TEST_TIMEOUT, "network": False}
    res = str(actions.gate(ctx.company, ctx.agent, task["id"], "run_code", payload) or "")
    if res.startswith("Not allowed"):
        return _off(label)
    m = re.search(r"request #(\d+)", res)
    if m and not re.search(r"^Exit code", res, re.M):
        return _waiting(ctx, int(m.group(1)), label)
    return _test_outcome(res, label, "this PC")


def _off(label: str) -> dict:
    return _result("code", False, f"the owner has turned off running code for this company, so the tests ({label}) "
                                  "cannot run. Finish with status 'partial' and say the tests have not run")


def _waiting(ctx, approval_id: int, label: str) -> dict:
    ctx.verify_waiting_on = approval_id
    return _result("code", False, f"tests need to run: approve request #{approval_id}. The owner runs {label} on "
                                  "this PC when they approve it, so finish with status 'waiting'; the task reopens "
                                  "when they decide", "waiting")


def _test_outcome(out: str | None, label: str, mode: str) -> dict:
    text = str(out or "").replace("\r\n", "\n")
    m = re.search(r"^Exit code (-?\d+)", text, re.M)
    if not m:
        what = "timed out" if "Timed out after" in text else "did not run"
        return _result("code", False, f"the tests ({label}) {what}: {_one_line(text, 300)}", mode)
    code = int(m.group(1))
    if code == 0:
        ran = re.search(r"^Ran \d+ tests? in [\d.]+s", text, re.M)
        return _result("code", True, f"{label} passed" + (f" ({ran.group(0)})" if ran else ""), mode)
    if code == 5:
        return _result("code", False, f"no tests ran ({label}). Add unittest tests under code/ in files named "
                                      "test_*.py, with an empty code/__init__.py", mode)
    return _result("code", False, f"the tests failed (exit code {code}): {_test_digest(text)}. Fix the code under "
                                  "code/ (or a test that is wrong)", mode)


def _test_digest(text: str) -> str:
    """The lines of a test run that say what broke, short enough for the model's next step."""
    if "Start directory is not importable" in text:
        return "unittest cannot import code/ as a package; add an empty code/__init__.py"
    keep = []
    for ln in text.split("\n"):
        s = ln.strip()
        if re.match(r"^(?:FAIL|ERROR):\s", s) or re.match(r"^\w*(?:Error|Exception)\b", s) or \
                re.match(r"^(?:FAILED|Ran \d+)", s) or re.match(r"^=+ .*(?:failed|passed|error)", s, re.I):
            if s not in keep:
                keep.append(s)
    return _one_line(" | ".join(keep[-8:]) or text[-400:], 500)


# ---------------------------------------------------------------- payment link

def _stripe(company: dict) -> dict | None:
    try:
        from . import connectors
        inst = connectors.instance("stripe", company)
    except Exception:
        return None
    return inst if inst and inst.get("enabled", 1) else None


def _payment_urls(text: str, inst: dict | None) -> list[str]:
    hosts = ["buy.stripe.com"]
    if inst:
        from . import conn_stripe
        base = conn_stripe._base(inst)
        host = re.sub(r"^https?://", "", base, flags=re.I).split("/")[0]
        if host and host.lower() != "api.stripe.com":
            hosts.append(host)   # a test server (the fake Stripe serves its links at <host>/pay/<id>)
    rx = re.compile(r"https?://(?:" + "|".join(re.escape(h) for h in hosts) + r")/[^\s<>\"'`)\]]+", re.I)
    out: list[str] = []
    for u in rx.findall(text or ""):
        u = u.rstrip(".,;:!?*_'\"”’")
        u = u.split("#")[0].split("?")[0]   # ?prefilled_email=... and the like are not part of the link
        if u not in out:
            out.append(u)
    return out


def _payment_kind(ctx, target: str, summary: str, evidence: str) -> list[dict]:
    # The handoff itself (deliverable, summary, evidence), not the files it names: a research file that quotes a
    # competitor's checkout link is not this company's payment link.
    inst = _stripe(ctx.company)
    urls = _payment_urls("\n".join([target or "", summary or "", evidence or ""]), inst)
    if not urls:
        return []
    if not inst:
        return [_result("payment link", False, "no Stripe connector is set up")]
    from . import conn_stripe
    mode = f"{conn_stripe.mode(inst)} mode"
    good = []
    for url in urls[:3]:
        try:
            link = conn_stripe.find_payment_link(inst, url)
        except conn_stripe.StripeError as e:
            return [_result("payment link", False, f"could not check {url} with Stripe: {e}", mode)]
        if not link:
            return [_result("payment link", False, f"{url} is not a payment link in this Stripe account ({mode}). "
                                                   "Hand off the URL that stripe__create_payment_link returned", mode)]
        if not link.get("active"):
            return [_result("payment link", False, f"{url} ({link.get('id')}) is turned off in Stripe (active: false). "
                                                   "Create a new link, or ask the owner to turn it back on", mode)]
        good.append(f"{url} ({link.get('id')}) is active")
    return [_result("payment link", True, "; ".join(good), mode)]


# ---------------------------------------------------------------- outreach

def _suppression_reason(address: str) -> str | None:
    """Why this address must not be emailed, or None: the global do-not-email list, a stop reply to any company
    (Phase 0.5), or a contact marked unsubscribed in any company (Phase 2)."""
    a = _addr(address)
    if not a:
        return None
    row = db.one("SELECT reason FROM suppression WHERE email=?", (a,))
    if row:
        return row["reason"] or "on the list"
    from . import truth
    if truth.suppressed(a):
        return "they replied stop or unsubscribe"
    c = db.one("SELECT id FROM contacts WHERE lower(email)=? AND unsubscribed=1 ORDER BY id LIMIT 1", (a,))
    if c:
        return f"unsubscribed (contact #{c['id']})"
    return None


def suppress(email: str, reason: str = "", company_id: int | None = None) -> None:
    """Add an address to the global do-not-email list (unsubscribe, a stop reply, or the owner's button)."""
    a = _addr(email)
    if "@" not in a:
        return
    try:
        db.run("INSERT OR IGNORE INTO suppression(email, reason, company_id, created_at) VALUES(?,?,?,?)",
               (a, (reason or "").strip()[:200], company_id, db.now()))
    except Exception as e:   # the caller's own change (an unsubscribe, a handled reply) must still go through
        print(f"suppression list update failed: {e}")


def is_suppressed(email: str) -> bool:
    return _suppression_reason(email) is not None


def emails_today(cid: int) -> int:
    """Emails sent today (UTC) plus those waiting for the owner: what the daily limit counts. An email is counted
    once: a sent one is an outgoing row of the emails table, a waiting one a pending request."""
    sent = db.scalar("SELECT COUNT(*) FROM emails WHERE company_id=? AND direction='out' AND created_at >= ?",
                     (cid, db.now()[:10]))
    pending = db.scalar("SELECT COUNT(*) FROM approvals WHERE company_id=? AND action='send_email' "
                        "AND status='pending'", (cid,))
    return (sent or 0) + (pending or 0)


def daily_email_limit(company: dict) -> int:
    """The company's daily_email_limit (Phase 2 budgets: 0 means agents send none); the default when unreadable."""
    default = int(config.DEFAULT_COMPANY_SETTINGS["daily_email_limit"])
    try:
        value = db.company_settings(company).get("daily_email_limit")
        return default if value in (None, "") else max(0, int(value))
    except (TypeError, ValueError):
        return default


def cap_problem(company: dict, counted: bool = False) -> str | None:
    """The daily-limit failure for one more email, or None. counted: the email is already among today's (a pending
    request the finish-time gate re-checks), so it may bring the count up to the limit but not over. tools.send_email
    and outreach_check both use this, so the cap reads the same everywhere."""
    limit = daily_email_limit(company)
    n = emails_today(company["id"])
    if (n > limit) if counted else (n >= limit):
        return (f"daily email limit reached: {n} sent or queued today and the limit is {limit}; send the rest "
                "tomorrow")
    return None


def _is_reply(company: dict, payload: dict, a: str) -> bool:
    """Someone who wrote to the company first gets a reply, not commercial email (truth.outreach_guard's rule). The
    recipient must have written in: a reply_to_email_id alone proves nothing (any number, another company's email or
    another sender's), so it would let a cold email skip the opt-out and EU consent rules."""
    return bool(a) and any(_addr(r["from_addr"]) == a for r in db.q(
        "SELECT from_addr FROM emails WHERE company_id=? AND direction='in' AND lower(from_addr) LIKE ?",
        (company["id"], f"%{a}%")))


def _sender_names(company: dict, cs: dict) -> list[str]:
    names = [company.get("name") or "", str(cs.get("smtp_from_name") or "")]
    try:
        from . import conn_resend, connectors
        inst = connectors.instance("resend", company)
        if inst:
            names.append(conn_resend.from_name(inst))
    except Exception:
        pass
    return [n.strip() for n in names if n and n.strip()]


def _issues(report: str) -> list[str]:
    """The '- ' lines under 'Issues:' in a checks.review_text report."""
    lines = report.split("\n")
    at = next((i for i, ln in enumerate(lines) if ln.strip() == "Issues:"), None)
    return [ln.strip()[2:] for ln in lines[at + 1:] if ln.strip().startswith("- ")] if at is not None else []


def _outreach(ctx, payload: dict, queued: bool = False, decided: bool = False) -> tuple[list[str], list[str]]:
    """(failures, warnings) for one email. queued: the email is a pending request, already counted among today's
    (the finish check), so the daily cap does not count it twice. decided: it was sent or decided already, so the cap
    no longer applies to it (the other rules still say whether it should have gone)."""
    company = db.get_company(ctx.company["id"]) or ctx.company
    cs = db.company_settings(company)
    to = str(payload.get("to") or "")
    body = str(payload.get("body") or "")
    a = _addr(to)
    fails: list[str] = []
    why = _suppression_reason(a)
    if why:
        fails.append(f"recipient is on the do-not-email list: {a} ({why}); never email them again")
    names = _sender_names(company, cs)
    flat = " ".join(body.lower().split())
    if names and not any(" ".join(n.lower().split()) in flat for n in names):
        fails.append(f"the email does not name the sender: sign it with the company name ({names[0]})")
    commercial = not _is_reply(company, payload, a)
    report = checks.review_text(body, "cold_email") if body.strip() else ""
    issues = _issues(report)
    warnings = []
    for issue in issues:
        if issue.startswith("No opt-out line"):
            if commercial:
                fails.append(f"no opt-out line: end the email with a line such as \"{OPT_OUT_LINE}\"")
            continue
        hard = next((k for k in HARD_ISSUES if issue.startswith(k)), None)
        if hard:
            hits = issue.split(": ", 1)[1] if ": " in issue else ""
            fails.append(HARD_ISSUES[hard].format(hits=hits))
        else:
            warnings.append(issue)
    if not decided:   # an email that already went out (or was decided) can no longer be held back by the cap
        cap = cap_problem(company, counted=queued)
        if cap:
            fails.append(cap)
    tld = a.rpartition(".")[2] if "@" in a else ""
    if commercial and tld in EU_TLDS:
        row = db.one("SELECT consent FROM contacts WHERE company_id=? AND lower(email)=? ORDER BY id DESC LIMIT 1",
                     (company["id"], a))
        if str((row or {}).get("consent") or "").strip().lower() in _NO_CONSENT:
            fails.append(f"EU contact without recorded consent: {a} is a .{tld} address ({EU_TLDS[tld]}) and the "
                         "contact has no consent recorded. Email EU contacts only when they agreed to hear from the "
                         "company")
    return fails, warnings


def outreach_check(ctx, payload: dict, queued: bool = False, decided: bool = False) -> list[str]:
    """The compliance failures of one email payload (to, subject, body); an empty list means it may be queued.
    queued=True re-checks a pending request, already counted among today's (the finish-time gate); decided=True an
    email that was sent or decided, which the daily cap no longer concerns."""
    return _outreach(ctx, payload, queued, decided)[0]


def _run_emails(ctx, task: dict) -> list[tuple[str, dict, bool]]:
    """(label, payload, waiting) of every email this run queued or sent: send_email requests of this task created
    since the run started, and emails an auto policy sent at once (audit rows without a request). waiting: the
    request still waits for the owner (the only state in which the daily cap can hold it back)."""
    since = _run_started(ctx)
    out = []
    for r in db.q("SELECT id, payload, status FROM approvals WHERE company_id=? AND task_id=? AND action='send_email' "
                  "AND status IN ('pending','running','done','failed') AND created_at>=? ORDER BY id",
                  (ctx.company["id"], task["id"], since)):
        p = db.loads(r["payload"], {}) or {}
        out.append((f"request #{r['id']} to {p.get('to')}", p, r["status"] == "pending"))
    if since:   # without the run's start every email this agent ever sent would count
        for r in db.q("SELECT payload FROM audit WHERE company_id=? AND action='send_email' AND approval_id IS NULL "
                      "AND proposed_by=? AND created_at>=? ORDER BY id",
                      (ctx.company["id"], (ctx.agent or {}).get("name", ""), since)):
            p = db.loads(r["payload"], {}) or {}
            out.append((f"the email sent to {p.get('to')}", p, False))
    return out


def _outreach_kind(ctx, task: dict) -> list[dict]:
    emails = _run_emails(ctx, task)
    if not emails:
        return []
    fails, warnings = [], []
    for label, payload, waiting in emails[:20]:
        f, w = _outreach(ctx, payload, queued=waiting, decided=not waiting)
        for problem in f:
            short, _, fix = problem.partition(": ")
            fails.append(f"{short} in {label}: {fix}" if fix else f"{problem} in {label}")
        warnings += [f"{w_} ({label})" for w_ in w[:2]]
    if fails:
        return [_result("outreach", False, "; ".join(fails[:4]).rstrip(".") + ". A queued email cannot be changed "
                                           "from here: finish with status 'partial' and say which request the owner "
                                           "should reject or fix")]
    detail = f"{len(emails)} email(s) checked"
    if warnings:
        detail += "; advice: " + "; ".join(warnings[:3])
    return [_result("outreach", True, detail)]


# ---------------------------------------------------------------- research

def _research_kind(ctx, target: str) -> list[dict]:
    rel = _rel(target)
    if not rel.lower().startswith("research/"):
        return []
    try:
        p = workspace.safe_path(ctx.company, rel)
    except workspace.PathError:
        return []
    if p.is_file():
        files = [(rel, p)]
    elif p.is_dir():   # a folder: the files this task wrote in it
        written = _written_paths(ctx)
        files = [(w, workspace.safe_path(ctx.company, w)) for w in sorted(written)
                 if w.startswith(rel.rstrip("/") + "/")][:10]
        files = [(w, f) for w, f in files if f.is_file()]
    else:
        return []
    if not files:
        return []
    fetched = {_url_key(r["url"]) for r in db.q("SELECT url FROM fetched WHERE company_id=? AND task_id=?",
                                                (ctx.company["id"], ctx.task["id"]))}
    problems, count = [], 0
    for name, f in files:
        urls = _cited_urls(f.read_text(encoding="utf-8", errors="replace"))
        if not urls:
            problems.append(f"{name} cites no source URL. Fetch the pages you used with fetch_url and cite each "
                            "page's URL next to what it supports")
            continue
        count += len(urls)
        missing = [u for u in urls if _url_key(u) not in fetched]
        if missing:
            shown = ", ".join(missing[:4]) + (f" and {len(missing) - 4} more" if len(missing) > 4 else "")
            problems.append(f"{name} cites {len(missing)} URL(s) this task never fetched: {shown}. Fetch each with "
                            "fetch_url, or remove it from the file")
    if problems:
        return [_result("research", False, "; ".join(problems))]
    return [_result("research", True, f"{count} cited URL(s), all fetched in this task")]


# ---------------------------------------------------------------- tool checks (task.checks)

def _check_line(ctx, tool: str, rest: str, pages: set) -> dict | None:
    args = _args(rest)
    if tool == "browser_check":
        if not args:
            return _result("web page", False, "the check 'browser_check' names no page")
        key = _page_key(args[0])
        if key in pages:
            return None   # the deliverable's own check covered it
        pages.add(key)
        return _web_page(ctx, args[0])
    if tool == "audit_page":
        return _audit_check(ctx, args)
    if tool == "review_text":
        return _review_check(ctx, args)
    if tool == "check_contrast":
        return _contrast_check(args)
    if tool == "fetch_url":
        return _fetch_check(args)
    return None   # contracts.CHECK_TOOLS refuses anything else when the task is created


def _file(ctx, path: str):
    p = workspace.safe_path(ctx.company, path)
    return p if p.is_file() else None


def _audit_check(ctx, args: list[str]) -> dict:
    if not args:
        return _result("audit_page", False, "the check 'audit_page' names no file")
    path = args[0]
    p = _file(ctx, path)
    if not p:
        return _result("audit_page", False, f"{path} doesn't exist: write it first")
    site = workspace.safe_path(ctx.company, "site")
    in_site = site == p.parent or site in p.parents
    report = checks.audit_page(p, site if in_site else None)
    m = re.search(r"(\d+) blocker\(s\)", report.split("\n", 1)[0])
    if m and m.group(1) == "0":
        return _result("audit_page", True, f"{path}: 0 blockers")
    blockers = [ln[len("BLOCKER: "):] for ln in report.split("\n") if ln.startswith("BLOCKER: ")]
    n = m.group(1) if m else len(blockers)
    return _result("audit_page", False, f"{path} has {n} audit blocker(s): {' '.join(blockers[:4])}")


def _review_check(ctx, args: list[str]) -> dict:
    if not args:
        return _result("review_text", False, "the check 'review_text' names no file")
    path, kind = args[0], (args[1] if len(args) > 1 else "general").lower()
    p = _file(ctx, path)
    if not p:
        return _result("review_text", False, f"{path} doesn't exist: write it first")
    report = checks.review_text(p.read_text(encoding="utf-8", errors="replace"), kind,
                                p.suffix.lower() in WEB_SUFFIXES)
    hard = [i for i in _issues(report) if i.startswith(CHECK_HARD) or re.search(r"characters: X allows 280", i)]
    if hard:
        return _result("review_text", False, f"{path} ({kind}): " + " ".join(hard[:4]))
    return _result("review_text", True, f"{path} ({kind}): no hard issues")


def _contrast_check(args: list[str]) -> dict:
    colours = [a for a in args if a.lower() not in ("on", "vs", "over", "and", "/")]
    if len(colours) < 2:
        return _result("check_contrast", False, "the check 'check_contrast' needs a text colour and a background "
                                                "colour, e.g. check_contrast #555555 #ffffff")
    fg, bg = colours[0], colours[1]
    try:
        ratio = checks.contrast(fg, bg)
    except (ValueError, TypeError, IndexError):
        return _result("check_contrast", False, f"{fg} or {bg} is not a colour Holdco can read (use #rrggbb)")
    if ratio >= 4.5:   # WCAG does not round: 4.499 fails
        return _result("check_contrast", True, f"{fg} on {bg} is {ratio:.2f}:1")
    return _result("check_contrast", False, f"{fg} on {bg} is {ratio:.2f}:1, below 4.5:1 for normal text: darken the "
                                            "text or lighten the background")


def _fetch_check(args: list[str]) -> dict:
    if not args:
        return _result("fetch_url", False, "the check 'fetch_url' names no URL")
    url = args[0]
    try:
        web.check_public_url(url)
        status, _, _, final = web.http_get(url, timeout=15)
        if final and final != url:
            web.check_public_url(final)
    except web.WebError as e:
        return _result("fetch_url", False, f"{url} could not be fetched: {e}")
    if status < 400:
        return _result("fetch_url", True, f"{url} answered HTTP {status}")
    return _result("fetch_url", False, f"{url} answered HTTP {status}")
