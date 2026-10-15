"""Doctor: health checks for everything Holdco depends on (Phase 0).

Each check returns a row {key, label, status: ok|warn|bad, detail, fix, checked_at, run}. The scheduler
refreshes the rows hourly, the Doctor page shows them, and founding runs the preflight subset first.

Run numbers: every full run() takes the next integer and stamps it into each of its rows as "run" (summary()
repeats it). preflight() refreshes only the model rows and the search row; _merge() slots those into the cache
under the cache's current run number and leaves the other rows, including their checked_at, untouched. So a
consumer that remembers the last "run" it counted (the watchdog's connector streaks) sees a new number only
for a new full run, never for a founding preflight. Rows built outside a full run carry run 0.

Connector rows are keyed "connector:<name>:<company id>" for a company's connectors and "connector:telegram"
or "connector:ntfy" for the global ones; every connector row also carries "company_id" (None for the global
ones), so nobody has to parse the key.

The checks run in parallel daemon threads with a cap each, so one slow mail server never stalls the page or
the scheduler. run() never raises: a check that blows up becomes a "bad" row with the error in its detail.
Nothing here is shown to agents and no secret (token, password) is ever written into a row: error texts that
could quote a request URL go through notify.scrub, which masks the Telegram bot token.
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from . import backup, config, db, emailer, llm, notify, web
from .routing import route

LAST: list[dict] = []          # most recent rows, newest run
_lock = threading.Lock()
_refreshing = False            # a background refresh is in flight (refresh_if_stale never starts two)
_run_seq = 0                   # number of the newest full run(); its rows carry it as "run"
# Windows only: without this flag nvidia-smi flashes a console window every hour when Holdco was started
# hidden by the autostart. Zero everywhere else, which subprocess accepts.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Internal tunables (module constants; the owner never needs to edit them).
CHECK_TIMEOUT = 15             # seconds a normal check may take before its row says "timed out"
DEEP_TIMEOUT = 120             # the tool-call ping and a live search may wait for a cold model or a slow provider
NET_TIMEOUT = 8                # seconds for one network call inside a check
DISK_WARN_GB = 5
DISK_BAD_GB = 1
BACKUP_WARN_HOURS = 26
BACKUP_BAD_HOURS = 72
ERRORS_BAD = 10                # error events in 24 h that turn the row red (1 to 9 is amber)
# Consumer mail providers (Gmail app passwords) dislike hourly logins. A login that has been ok for a day is
# retested only every 6 hours; anything else is retested on every run.
CONNECTOR_STABLE_HOURS = 24
CONNECTOR_RECHECK_HOURS = 6
NETLIFY_API = "https://api.netlify.com"    # tests point this at a local fake
README_GPU = 'the README section "Getting the most out of an RTX 5090"'

_ok_since: dict[str, float] = {}           # connector key -> when its login first turned ok (cleared on failure)
_last_login: dict[str, tuple[float, dict]] = {}   # connector key -> (time of the last real login test, its row)


# ---------------------------------------------------------------- row helpers

def _row(key: str, label: str, status: str, detail: str, fix: dict | None = None) -> dict:
    return {"key": key, "label": label, "status": status, "detail": (detail or "")[:400], "fix": fix,
            "checked_at": db.now(), "run": 0}


def _link(label: str, href: str) -> dict:
    return {"kind": "link", "label": label, "href": href}


def _action(label: str, action: str) -> dict:
    return {"kind": "action", "label": label, "action": action}


def _text(label: str) -> dict:
    return {"kind": "text", "label": label}


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f}"


def _ago(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} min ago"
    if hours < 48:
        return f"{hours:.0f} h ago"
    return f"{hours / 24:.0f} days ago"


# ---------------------------------------------------------------- parallel runner

class _Job:
    """One check: `fn` returns a row or a list of rows; `metas` names the rows it is responsible for, so a
    crash or a timeout can still produce a row per key. `company_id` is the company a connector job belongs
    to (None for a global connector); it is stamped into every connector row the job produces."""

    def __init__(self, metas: list[tuple[str, str, dict | None]], fn, timeout: float = CHECK_TIMEOUT,
                 company_id: int | None = None):
        self.metas, self.fn, self.timeout, self.company_id = metas, fn, timeout, company_id


def _run_jobs(jobs: list[_Job]) -> list[dict]:
    results: list[list[dict] | None] = [None] * len(jobs)

    def work(i: int, job: _Job) -> None:
        try:
            out = job.fn()
            results[i] = out if isinstance(out, list) else [out]
        except Exception as e:  # a failing check is a bad row, never a crash
            # The error text may quote a request URL, so it is scrubbed before it becomes a row.
            detail = notify.scrub(f"Check failed: {e}")
            results[i] = [_row(k, label, "bad", detail, fix) for k, label, fix in job.metas]

    threads = [threading.Thread(target=work, args=(i, j), daemon=True, name=f"doctor-{j.metas[0][0]}")
               for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    start = time.monotonic()
    rows: list[dict] = []
    for i, (t, job) in enumerate(zip(threads, jobs)):
        t.join(max(0.0, job.timeout - (time.monotonic() - start)))
        got = results[i]
        if got is None:
            got = [_row(k, label, "bad", f"Timed out after {job.timeout:.0f} s", fix) for k, label, fix in job.metas]
        for r in got:
            if str(r.get("key") or "").startswith("connector:"):
                r["company_id"] = job.company_id   # also on crash and timeout rows, so every connector row has it
        rows.extend(got)
    return rows


# ---------------------------------------------------------------- the checks

def _installed(model: str, models: list[str]) -> bool:
    # Ollama lists "name:tag"; a setting without a tag means ":latest".
    return model in models or (":" not in model and f"{model}:latest" in models)


def _check_model(s: dict, deep: bool) -> list[dict]:
    """model_server and model rows (one request serves both). deep adds the tool-call ping to the model row."""
    base = (s.get("llm_base_url") or "").rstrip("/")
    backend = s.get("llm_backend") or "ollama"
    model = s.get("default_model") or ""
    fix = _link("Model settings", "#/settings")
    try:
        models = llm.list_models(s)
    except llm.LLMError as e:
        return [_row("model_server", "Model server", "bad", str(e), fix),
                _row("model", "Default model", "warn",
                     f"Could not check whether {model or 'the default model'} is installed: the model server is "
                     "unreachable.", fix)]
    srv = _row("model_server", "Model server", "ok", f"{backend} at {base} is reachable ({len(models)} models)")
    if not model:
        return [srv, _row("model", "Default model", "bad", "No default model is set.", fix)]
    if not _installed(model, models):
        have = (", ".join(models[:6]) + (", ..." if len(models) > 6 else "")) or "none"
        pull = _text(f"ollama pull {model}") if backend == "ollama" else fix
        return [srv, _row("model", "Default model", "bad", f"{model} is not installed. Installed: {have}.", pull)]
    if not deep:
        return [srv, _row("model", "Default model", "ok", f"{model} is installed.")]
    ok, message = llm.tool_ping(model, s)
    # tool_ping says ok=True both when the model called the tool and when it only answered in prose;
    # the message tells them apart, and only a real tool call counts as a green row.
    status = "ok" if ok and "can call tools" in message else ("warn" if ok else "bad")
    return [srv, _row("model", "Default model", status, f"{model} is installed. {message}", None if ok else fix)]


def _gpu_memory_note() -> str:
    """Detail suffix from nvidia-smi when it exists; empty otherwise (never a failure)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        line = (out.stdout or "").strip().splitlines()[0]
        used, total = [float(x.strip()) for x in line.split(",")[:2]]
        return f"; GPU memory {used / 1024:.1f} of {total / 1024:.1f} GB in use (nvidia-smi)"
    except Exception:
        return ""


def _check_gpu(s: dict) -> dict:
    key, label = "gpu_fit", "GPU memory fit"
    fix = _text(f"Lower Agents working at once or the context window; see {README_GPU}")
    if (s.get("llm_backend") or "ollama") != "ollama":
        return _row(key, label, "ok", "Not applicable: only Ollama reports what sits in GPU memory.")
    try:
        loaded = llm.loaded_models(s)
    except llm.LLMError as e:
        return _row(key, label, "ok", f"Could not check: {e}")
    note = _gpu_memory_note()
    if not loaded:
        return _row(key, label, "ok", "No model is loaded right now; Ollama loads it on first use" + note + ".")
    spilling = [m for m in loaded if m["size"] and m["size_vram"] < m["size"]]
    if spilling:
        m = spilling[0]
        pct = round(100 * m["size_vram"] / m["size"])
        return _row(key, label, "warn", f"{m['name']} is spilling into RAM: {pct}% on the GPU "
                                        f"({_gb(m['size_vram'])} of {_gb(m['size'])} GB){note}.", fix)
    names = ", ".join(f"{m['name']} ({_gb(m['size'])} GB)" for m in loaded)
    return _row(key, label, "ok", f"Fully on the GPU: {names}{note}.")


def _check_search(s: dict, deep: bool) -> dict:
    key, label = "search", "Web search"
    fix = _link("Search settings", "#/settings")
    if deep and hasattr(web, "live_search_test"):
        try:
            ok, detail = web.live_search_test(s)
        except Exception as e:
            ok, detail = False, f"Live search failed: {e}"
        return _row(key, label, "ok" if ok else "bad", detail, None if ok else fix)
    health = getattr(web, "search_health", None)
    if health is None:
        return _row(key, label, "ok", "Search providers have not been tested yet.")
    try:
        rows = list(health(s))
    except Exception as e:
        return _row(key, label, "bad", f"Could not read the provider status: {e}", fix)
    configured = [r for r in rows if r.get("configured")]
    statuses = [r.get("status") for r in configured]
    if not configured:
        status, head = "bad", "No search provider is configured"
    elif "ok" in statuses:
        status, head = "ok", f"{statuses.count('ok')} of {len(configured)} providers working"
    elif "warn" in statuses:
        status, head = "warn", "Providers are cooling down after failures"
    elif all(st == "bad" for st in statuses):
        status, head = "bad", "Every search provider failed its last try"
    else:
        status, head = "ok", "Not tested yet"
    detail = head + ": " + "; ".join(f"{r.get('provider')}: {r.get('detail') or r.get('status')}" for r in rows)
    return _row(key, label, status, detail, None if status == "ok" else fix)


def _login_row(key: str, label: str, fix: dict, fn, recheck: bool) -> dict:
    """Run a login test, with the backoff for connectors that have been fine for a day. recheck=True (a manual
    "Run checks" or a deep run) always logs in for real."""
    now = time.time()
    since, last = _ok_since.get(key), _last_login.get(key)
    if (since and last and not recheck and now - since >= CONNECTOR_STABLE_HOURS * 3600
            and now - last[0] < CONNECTOR_RECHECK_HOURS * 3600):
        row = dict(last[1])
        row["checked_at"] = db.now()
        row["detail"] = (f"{last[1]['detail']} (last tested {_ago((now - last[0]) / 3600)}; a login that has "
                         f"been fine for a day is retested every {CONNECTOR_RECHECK_HOURS} h)")
        return row
    try:
        row = _row(key, label, "ok", fn())
    except emailer.EmailError as e:
        row = _row(key, label, "bad", str(e), fix)
    if row["status"] == "ok":
        _ok_since.setdefault(key, now)
    else:
        _ok_since.pop(key, None)
    _last_login[key] = (now, dict(row))
    return row


def _netlify_row(key: str, label: str, fix: dict, token: str) -> dict:
    # The token travels only in the header; no error path below includes it.
    req = urllib.request.Request(NETLIFY_API.rstrip("/") + "/api/v1/user",
                                 headers={"Authorization": "Bearer " + token, "User-Agent": "Holdco"})
    try:
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        why = "rejected the token" if e.code in (401, 403) else "answered"
        return _row(key, label, "bad", f"Netlify {why} (HTTP {e.code}).", fix)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return _row(key, label, "bad", f"Could not reach Netlify: {getattr(e, 'reason', e)}", fix)
    who = (data.get("email") or data.get("full_name") or "account") if isinstance(data, dict) else "account"
    return _row(key, label, "ok", f"Token accepted (Netlify account: {who}).")


def _url_ok(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url or "")
        return p.scheme in ("http", "https") and bool(p.hostname)
    except ValueError:
        return False


def _telegram_row(s: dict) -> dict:
    key, label = "connector:telegram", "Telegram bot"
    fix = _link("Notification settings", "#/settings")
    token = s.get("telegram_bot_token") or ""
    chat = str(s.get("telegram_chat_id") or "").strip()
    base = (s.get("telegram_api_base") or "https://api.telegram.org").rstrip("/")
    # The request is built inside the try: a malformed api base or a token pasted with a newline makes urllib
    # raise with the whole URL, token included, in the message. Every such text is scrubbed before it is a row.
    try:
        req = urllib.request.Request(f"{base}/bot{token}/getMe", headers={"User-Agent": "Holdco"})
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        why = "rejected the bot token" if e.code in (401, 404) else "answered"
        return _row(key, label, "bad", f"Telegram {why} (HTTP {e.code}).", fix)
    except Exception as e:   # URLError, OSError, http.client.InvalidURL, ValueError: none of them is safe to show raw
        return _row(key, label, "bad",
                    notify.scrub(f"Could not reach Telegram: {getattr(e, 'reason', None) or e}", s), fix)
    if not isinstance(data, dict) or not data.get("ok"):
        return _row(key, label, "bad", "Telegram rejected the bot token.", fix)
    name = (data.get("result") or {}).get("username") or "bot"
    if not chat:
        return _row(key, label, "warn", f"Bot @{name} is reachable but no chat id is set, so nothing can be sent. "
                                        "Message the bot once, then set the chat id in Settings.", fix)
    return _row(key, label, "ok", f"Bot @{name} is reachable; messages go to chat {chat}.")


def _ntfy_row(s: dict) -> dict:
    key, label = "connector:ntfy", "ntfy"
    server = (s.get("ntfy_server") or "https://ntfy.sh").strip()
    topic = str(s.get("ntfy_topic") or "").strip()
    if not _url_ok(server):
        return _row(key, label, "bad", f"The ntfy server URL is malformed: {server}",
                    _link("Notification settings", "#/settings"))
    return _row(key, label, "ok", f"Topic {topic} on {server}.")


def _webhook_row(key: str, label: str, fix: dict, url: str) -> dict:
    if not _url_ok(url):
        return _row(key, label, "bad", "The webhook URL is malformed: it needs http(s):// and a host.", fix)
    host = urllib.parse.urlparse(url).hostname
    return _row(key, label, "ok", f"URL looks valid (posts go to {host}); nothing was sent.")


def _connector_jobs(s: dict, recheck: bool) -> list[_Job]:
    """One job per configured connector per company (archived companies skipped), plus the global ones.
    Each job carries its company id, which _run_jobs stamps into the rows as "company_id"."""
    jobs: list[_Job] = []
    for c in db.q("SELECT * FROM companies WHERE status!='archived' ORDER BY id"):
        cs = db.company_settings(c)
        cid, cname = c["id"], c["name"]
        fix = _link("Company settings", f"#/c/{cid}/settings")
        if emailer.smtp_configured(cs):
            key, label = f"connector:smtp:{cid}", f"Email sending ({cname})"
            jobs.append(_Job([(key, label, fix)], lambda k=key, lb=label, f=fix, cs=cs: _login_row(
                k, lb, f, lambda: emailer.test_smtp(cs), recheck), company_id=cid))
        if emailer.imap_configured(cs):
            key, label = f"connector:imap:{cid}", f"Email receiving ({cname})"
            jobs.append(_Job([(key, label, fix)], lambda k=key, lb=label, f=fix, cs=cs: _login_row(
                k, lb, f, lambda: emailer.test_imap(cs), recheck), company_id=cid))
        if cs.get("netlify_token"):
            key, label = f"connector:netlify:{cid}", f"Website deploys ({cname})"
            jobs.append(_Job([(key, label, fix)], lambda k=key, lb=label, f=fix, t=str(cs["netlify_token"]):
                             _netlify_row(k, lb, f, t), company_id=cid))
        if cs.get("webhook_url"):
            key, label = f"connector:webhook:{cid}", f"Posting webhook ({cname})"
            jobs.append(_Job([(key, label, fix)], lambda k=key, lb=label, f=fix, u=str(cs["webhook_url"]):
                             _webhook_row(k, lb, f, u), company_id=cid))
    if s.get("telegram_bot_token"):
        jobs.append(_Job([("connector:telegram", "Telegram bot", None)], lambda: _telegram_row(s)))
    if s.get("ntfy_topic"):
        jobs.append(_Job([("connector:ntfy", "ntfy", None)], lambda: _ntfy_row(s)))
    return jobs


def _disk_free_bytes() -> tuple[int, int]:
    """(free, total) for the drive that holds the data folder. Tests patch this."""
    d = config.DATA_DIR if config.DATA_DIR.exists() else config.DATA_DIR.parent
    u = shutil.disk_usage(d)
    return u.free, u.total


def _check_disk() -> dict:
    free, total = _disk_free_bytes()
    free_gb = free / 1024 ** 3
    status = "ok" if free_gb >= DISK_WARN_GB else ("warn" if free_gb >= DISK_BAD_GB else "bad")
    detail = f"{free_gb:.1f} GB free of {total / 1024 ** 3:.0f} GB on the drive holding {config.DATA_DIR}"
    fix = None if status == "ok" else _text("Free up space on that drive; backups and company files need room")
    return _row("disk", "Disk space", status, detail, fix)


def _check_backup() -> dict:
    fix = _action("Back up now", "backup")
    try:
        last = backup.last_backup()
    except NotImplementedError:
        last = None
    if not last:
        return _row("backup", "Last backup", "bad", "No backup has been made yet.", fix)
    age_h = db.minutes_since(last.get("created_at")) / 60
    status = "ok" if age_h < BACKUP_WARN_HOURS else ("warn" if age_h < BACKUP_BAD_HOURS else "bad")
    name = last.get("name") or last.get("path", "")
    detail = f"Last backup {_ago(age_h)} ({name}, {(last.get('size') or 0) / 1e6:.1f} MB)"
    return _row("backup", "Last backup", status, detail, fix)


def _check_errors() -> dict:
    since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = db.scalar("SELECT COUNT(*) FROM events WHERE kind='error' AND created_at >= ?", (since,)) or 0
    if not total:
        return _row("errors_24h", "Errors in the last 24 h", "ok", "No errors in the last 24 h.")
    top = db.q("SELECT message, COUNT(*) AS n FROM events WHERE kind='error' AND created_at >= ? "
               "GROUP BY message ORDER BY n DESC, message LIMIT 3", (since,))
    status = "bad" if total >= ERRORS_BAD else "warn"
    detail = f"{total} error{'s' if total != 1 else ''} in the last 24 h: " + "; ".join(
        f"{r['n']}x {(r['message'] or '')[:80]}" for r in top)
    return _row("errors_24h", "Errors in the last 24 h", status, detail, _link("Open the live feed", "#/"))


def _check_index() -> dict:
    """Phase 0.5: the search index agents and the dashboard search use. FTS5 missing means the plain LIKE
    fallback (search still answers, only slower and unranked); a company with nothing in the index yet means
    search misses its files until it is indexed."""
    from . import context   # imported here: context imports tools, which imports watchdog, which imports doctor
    key, label = "search_index", "Search index"
    st = context.index_status()
    if not st.get("fts"):
        return _row(key, label, "warn", "SQLite full-text search (FTS5) is not available in this Python, so search "
                                        "falls back to a slower plain-text scan. Search still works.",
                    _text("Use a Python whose SQLite includes FTS5 for ranked search"))
    archived = {r["id"] for r in db.q("SELECT id FROM companies WHERE status='archived'")}
    companies = [c for c in st.get("companies") or [] if c["id"] not in archived]
    missing = [c for c in companies if not (c.get("rows") or c.get("last_indexed") or c.get("last_checked"))]
    stamps = [c.get("last_indexed") for c in companies if c.get("last_indexed")] + \
        ([st["global_last_indexed"]] if st.get("global_last_indexed") else [])
    newest = max(stamps) if stamps else None
    when = f"; last indexed {_ago(db.minutes_since(newest) / 60)}" if newest else ""
    detail = (f"Full-text index: {st.get('rows', 0):,} entries for {len(companies)} "
              f"compan{'y' if len(companies) == 1 else 'ies'} and {st.get('global_files', 0)} of your context "
              f"files{when}.")
    model = st.get("embedding_model") or ""
    if model:
        detail += f" Meaning search with {model}: {st.get('vectors', 0):,} vectors."
    if missing:
        names = ", ".join(c["name"] for c in missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
        return _row(key, label, "warn", f"Not indexed yet: {names}. Search misses their files until they are "
                                        f"indexed. {detail}", _action("Index now", "reindex"))
    return _row(key, label, "ok", detail)


def _tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 100_000 else f"{n:,}"


def _check_budgets() -> dict:
    """Phase 0.5: companies over their daily token budget get no background work until midnight."""
    from . import planning   # imported here: planning imports tools (see _check_index)
    key, label = "token_budgets", "Token budgets"
    rows = planning.budget_status()
    if not rows:
        return _row(key, label, "ok", "No companies yet.")
    over = [r for r in rows if r["over"]]
    if over:
        names = "; ".join(f"{r['name']} ({_tokens(r['used'])} of {_tokens(r['budget'])} tokens)" for r in over[:5])
        more = f" and {len(over) - 5} more" if len(over) > 5 else ""
        return _row(key, label, "warn", f"Over today's budget, so background work is paused until midnight: "
                                        f"{names}{more}. Chat and tasks you create still run.",
                    _link("Company settings", f"#/c/{over[0]['company_id']}/settings"))
    used = "; ".join(f"{r['name']} {_tokens(r['used'])}" + (f" of {_tokens(r['budget'])}" if r["budget"] else
                                                           " (no limit)") for r in rows[:6])
    more = f"; and {len(rows) - 6} more" if len(rows) > 6 else ""
    return _row(key, label, "ok", f"No company is over its daily token budget. Today: {used}{more}.")


# ---------------------------------------------------------------- public API

def run(deep: bool = False, force: bool = False) -> list[dict]:
    """Run every check and cache the rows. deep=True also runs slow checks (model tool-call ping, live search).
    force=True (a manual "Run checks") retests every connector login even when the backoff would skip it.
    The rows get the next run number (see the module docstring)."""
    global _run_seq
    try:
        s = db.get_settings()
        long = DEEP_TIMEOUT if deep else CHECK_TIMEOUT
        settings_fix = _link("Model settings", "#/settings")
        jobs = [
            _Job([("model_server", "Model server", settings_fix), ("model", "Default model", settings_fix)],
                 lambda: _check_model(s, deep), long),
            _Job([("gpu_fit", "GPU memory fit", None)], lambda: _check_gpu(s)),
            _Job([("search", "Web search", _link("Search settings", "#/settings"))],
                 lambda: _check_search(s, deep), long),
            *_connector_jobs(s, deep or force),
            _Job([("disk", "Disk space", None)], _check_disk),
            _Job([("backup", "Last backup", _action("Back up now", "backup"))], _check_backup),
            _Job([("errors_24h", "Errors in the last 24 h", None)], _check_errors),
            _Job([("search_index", "Search index", None)], _check_index),
            _Job([("token_budgets", "Token budgets", None)], _check_budgets),
        ]
        rows = _run_jobs(jobs)
    except Exception as e:  # the Doctor must never take the scheduler or the page down with it
        traceback.print_exc()
        rows = [_row("doctor", "Doctor", "bad", notify.scrub(f"The health checks could not run: {e}"))]
    with _lock:
        _run_seq += 1
        for r in rows:
            r["run"] = _run_seq
        LAST[:] = rows
    return rows


def _merge(rows: list[dict]) -> None:
    """Refresh the cached copy of these rows so the sidebar dot agrees with what founding just saw.

    Only the given keys are replaced: every other cached row keeps its checked_at, and the merged rows take the
    cache's run number, so a consumer that counts Doctor runs by "run" does not mistake a preflight for a new
    run. An empty cache stays empty so the scheduler still runs the full set soon."""
    with _lock:
        if not LAST:
            return
        current = LAST[0].get("run", 0)
        by_key = {r["key"]: r for r in rows}
        for r in by_key.values():
            r["run"] = current
        for i, r in enumerate(LAST):
            if r["key"] in by_key:
                LAST[i] = by_key.pop(r["key"])
        LAST.extend(by_key.values())


def preflight() -> dict:
    """Checks that must pass before founding a company. Returns {"ok": bool, "checks": [rows], "soft": [rows]}.

    Hard: model_server and model (run fresh). Soft: the search row (cached when younger than an hour, else the
    provider status without a live query); a bad search never blocks founding, the founding prompt adapts."""
    try:
        s = db.get_settings()
        fix = _link("Model settings", "#/settings")
        hard = _run_jobs([_Job([("model_server", "Model server", fix), ("model", "Default model", fix)],
                               lambda: _check_model(s, False))])
        cached = search_status()
        if cached and db.minutes_since(cached.get("checked_at")) <= 60:
            soft = [cached]
        else:
            soft = _run_jobs([_Job([("search", "Web search", None)], lambda: _check_search(s, False))])
        _merge(hard + soft)
        return {"ok": all(r["status"] != "bad" for r in hard), "checks": hard, "soft": soft}
    except Exception:  # a broken health check must not stop the owner from founding
        traceback.print_exc()
        return {"ok": True, "checks": [], "soft": []}


def summary(rows: list[dict] | None = None) -> dict:
    """Counts per status plus the first row's checked_at and the run number the rows belong to (0 = none yet)."""
    rows = LAST if rows is None else rows
    return {"ok": sum(r["status"] == "ok" for r in rows), "warn": sum(r["status"] == "warn" for r in rows),
            "bad": sum(r["status"] == "bad" for r in rows), "checked_at": rows[0]["checked_at"] if rows else None,
            "run": rows[0].get("run", 0) if rows else 0}


def _cache_age_minutes() -> float:
    """Age of the oldest cached row. A preflight merge refreshes a few rows only; the rest must still be redone
    on time, so the hourly refresh looks at the oldest row rather than the first one."""
    ages = [db.minutes_since(r.get("checked_at")) for r in LAST if isinstance(r, dict)]
    return max(ages, default=1e9)


def search_status() -> dict | None:
    """The cached search row, or None before the first run."""
    with _lock:
        return next((dict(r) for r in LAST if r.get("key") == "search"), None)


def search_down() -> bool:
    """True when the last check found every search provider failing (the founding prompt adapts)."""
    row = search_status()
    return bool(row and row.get("status") == "bad")


def row_status(key: str) -> str | None:
    """Status of one cached row ("ok" | "warn" | "bad") or None when it has not been checked."""
    with _lock:
        return next((r.get("status") for r in LAST if r.get("key") == key), None)


def refresh_if_stale(max_age_minutes: float = 60) -> None:
    """Called by the scheduler every tick; reruns the checks in a background thread when the cache is older
    than max_age_minutes. Never blocks the tick and never starts two refreshes at once."""
    global _refreshing
    with _lock:
        if _refreshing:
            return
        if LAST and _cache_age_minutes() < max_age_minutes:
            return
        _refreshing = True

    def work() -> None:
        global _refreshing
        try:
            run()
        finally:
            with _lock:
                _refreshing = False

    threading.Thread(target=work, daemon=True, name="doctor-refresh").start()


@route("GET", "/api/doctor")
def api_doctor(h, m, body):
    if not LAST:
        run()   # the page never shows nothing
    return {"checks": LAST, "summary": summary()}


@route("POST", "/api/doctor/run")
def api_doctor_run(h, m, body):
    rows = run(deep=bool((body or {}).get("deep")), force=True)   # the owner pressed the button: test for real
    return {"checks": rows, "summary": summary(rows)}


_reindexing = threading.Event()   # set while a rebuild started from the dashboard is running


def _reindex_work() -> None:
    from . import bus, context
    try:
        context.reindex_global()
        context.reindex_all()
        _merge([_check_index()])
    except Exception:
        traceback.print_exc()
    finally:
        _reindexing.clear()
        bus.notify("doctor")


@route("POST", "/api/index/rebuild")
def api_index_rebuild(h, m, body):
    """The Doctor's "Index now": bring the search index up to date for every company and the owner's library in
    a background thread (a big data folder can take a while). The Doctor page reloads when it is done."""
    with _lock:   # two clicks at once start one rebuild
        if _reindexing.is_set():
            return {"ok": True, "started": False, "message": "The search index is already being updated."}
        _reindexing.set()
    threading.Thread(target=_reindex_work, daemon=True, name="reindex-rebuild").start()
    return {"ok": True, "started": True, "message": "Updating the search index in the background."}
