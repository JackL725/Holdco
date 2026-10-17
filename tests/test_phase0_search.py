"""Phase 0: the search fallback chain in holdco/web.py and the web_search tool's outage message.

No test touches the network: provider functions are swapped for stand-ins, and the DuckDuckGo block detection
runs against a fake server on 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOLDCO_DATA"] = tempfile.mkdtemp(prefix="holdco-search-")
os.environ.pop("HOLDCO_SEARCH_PROVIDERS", None)   # the chain must not be narrowed by the caller's shell

from holdco import db, roles, tools, web, workspace  # noqa: E402

db.init()


def make_company(name="SearchCo", members=("ceo", "research")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**8)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "An idea", "active", "building", "{}", db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def ctx_for(company, role, task=None, kind="task") -> tools.Ctx:
    agent = db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))
    c = tools.Ctx(company=company, agent=agent, kind=kind, run_id=0, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(agent, kind)}
    return c


def rows(name: str, count: int = 2) -> list[dict]:
    return [{"title": f"{name} result {i}", "url": f"https://{name}.example.com/{i}", "snippet": "text"}
            for i in range(1, count + 1)]


class Stub:
    """A stand-in provider that returns rows, returns [] or raises, and counts how often it was called."""

    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error, self.calls = result, error, 0

    def __call__(self, query, n, settings):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.result or [])


NONE = {"search_provider": "duckduckgo", "brave_api_key": "", "searxng_url": ""}
ALL = {"search_provider": "duckduckgo", "brave_api_key": "brave-key", "searxng_url": "http://127.0.0.1:1/"}


class ChainCase(unittest.TestCase):
    def setUp(self):
        web.reset_status()
        self.patch("_ddgs_installed", lambda: True)

    def patch(self, name: str, value):
        p = mock.patch.object(web, name, value)
        p.start()
        self.addCleanup(p.stop)
        return value

    def stubs(self, brave=None, searxng=None, ddgs=None, ddg_html=None) -> dict[str, Stub]:
        """Install one stub per provider (a missing one raises, so a test notices an unexpected call)."""
        out = {}
        for name, stub in (("brave", brave), ("searxng", searxng), ("ddgs", ddgs), ("ddg_html", ddg_html)):
            out[name] = self.patch("_" + name, stub or Stub(error=web.ProviderError(f"{name} should not be called")))
        return out


class TestSearchChain(ChainCase):
    def test_configured_provider_answers_first(self):
        st = self.stubs(brave=Stub(rows("brave")), ddgs=Stub(rows("ddgs")), ddg_html=Stub(rows("ddg")))
        got = web.search("q", 5, {**ALL, "search_provider": "brave"})
        self.assertEqual([r["url"] for r in got], [r["url"] for r in rows("brave")])
        self.assertEqual(web.LAST_PROVIDER, "brave")
        self.assertEqual((st["brave"].calls, st["ddgs"].calls, st["ddg_html"].calls), (1, 0, 0))
        self.assertTrue(web.PROVIDER_STATUS["brave"]["ok"])
        self.assertTrue(web.PROVIDER_STATUS["brave"]["last_ok"])
        self.assertIsNone(web.PROVIDER_STATUS["ddgs"]["ok"])   # untouched
        # duckduckgo means ddgs first, then ddg_html, then the keyed providers
        got = web.search("q", 5, ALL)
        self.assertEqual(got[0]["url"], rows("ddgs")[0]["url"])
        self.assertEqual(web.LAST_PROVIDER, "ddgs")
        self.assertEqual(st["ddgs"].calls, 1)

    def test_order(self):
        self.assertEqual(web._order({"search_provider": "brave"}), ["brave", "searxng", "ddgs", "ddg_html"])
        self.assertEqual(web._order({"search_provider": "searxng"}), ["searxng", "brave", "ddgs", "ddg_html"])
        self.assertEqual(web._order({"search_provider": "duckduckgo"}), ["ddgs", "ddg_html", "brave", "searxng"])
        self.assertEqual(web._order({}), ["ddgs", "ddg_html", "brave", "searxng"])
        with mock.patch.dict(os.environ, {"HOLDCO_SEARCH_PROVIDERS": "searxng, ddg_html"}):
            self.assertEqual(web._order({"search_provider": "brave"}), ["searxng", "ddg_html"])
            health = web.search_health({**ALL, "search_provider": "brave"})
            self.assertEqual([r["provider"] for r in health], ["searxng", "ddg_html"])

    def test_falls_back_past_empty_and_failing_providers(self):
        st = self.stubs(brave=Stub([]), searxng=Stub(error=web.ProviderError("HTTP 500")),
                        ddgs=Stub(error=RuntimeError("boom")), ddg_html=Stub(rows("ddg")))
        got = web.search("q", 5, {**ALL, "search_provider": "brave"})
        self.assertEqual(got[0]["url"], rows("ddg")[0]["url"])
        self.assertEqual(web.LAST_PROVIDER, "ddg_html")
        self.assertEqual([s.calls for s in st.values()], [1, 1, 1, 1])
        # an empty answer means the provider works; a raise means it does not and starts a cooldown
        self.assertTrue(web.PROVIDER_STATUS["brave"]["ok"])
        self.assertEqual(web.PROVIDER_STATUS["brave"]["cooldown_until"], 0)
        bad = web.PROVIDER_STATUS["searxng"]
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "HTTP 500")
        self.assertTrue(bad["last_error"])
        self.assertGreater(bad["cooldown_until"], time.time() + web.COOLDOWN_SECONDS - 5)
        self.assertIn("RuntimeError: boom", web.PROVIDER_STATUS["ddgs"]["error"])   # a bug is contained too
        self.assertEqual(web.SEARCH_FAIL_STREAK, 0)

    def test_empty_answer_is_not_an_outage(self):
        st = self.stubs(ddgs=Stub([]), ddg_html=Stub([]))
        self.assertEqual(web.search("nothing matches this", 5, NONE), [])
        self.assertEqual((st["ddgs"].calls, st["ddg_html"].calls), (1, 1))
        self.assertEqual(web.SEARCH_FAIL_STREAK, 0)
        self.assertTrue(web.PROVIDER_STATUS["ddg_html"]["ok"])

    def test_unconfigured_providers_are_skipped_without_a_call(self):
        self.patch("_ddgs_installed", lambda: False)
        st = self.stubs(ddg_html=Stub(error=web.ProviderError("blocked (HTTP 202 challenge)")))
        with self.assertRaises(web.WebError) as cm:
            web.search("q", 5, NONE)
        self.assertEqual(str(cm.exception), "All search providers failed: ddgs: not set up; "
                         "ddg_html: blocked (HTTP 202 challenge); brave: not set up; searxng: not set up")
        self.assertEqual([s.calls for s in st.values()], [0, 0, 0, 1])
        for name in ("brave", "searxng", "ddgs"):
            self.assertIsNone(web.PROVIDER_STATUS[name]["ok"], name)   # not counted as failed
        self.assertEqual(web.SEARCH_FAIL_STREAK, 1)

    def test_blocked_provider_cools_down_and_is_skipped(self):
        self.patch("_ddgs_installed", lambda: False)
        st = self.stubs(ddg_html=Stub(error=web.ProviderError("blocked (HTTP 202 challenge)")))
        with self.assertRaises(web.WebError):
            web.search("q", 5, NONE)
        status = web.PROVIDER_STATUS["ddg_html"]
        self.assertFalse(status["ok"])
        self.assertEqual(status["error"], "blocked (HTTP 202 challenge)")
        self.assertAlmostEqual(status["cooldown_until"], time.time() + web.COOLDOWN_SECONDS, delta=5)
        # the next call inside the cooldown does not touch the provider and says how long is left
        st["ddg_html"].error = None
        st["ddg_html"].result = rows("ddg")
        with self.assertRaises(web.WebError) as cm:
            web.search("q", 5, NONE)
        self.assertIn("ddg_html: blocked (HTTP 202 challenge) (cooling down, retry in 10 min)", str(cm.exception))
        self.assertEqual(st["ddg_html"].calls, 1)
        self.assertEqual(web.SEARCH_FAIL_STREAK, 2)
        # once the cooldown has passed it is tried again and recovers
        status["cooldown_until"] = time.time() - 1
        self.assertEqual(web.search("q", 5, NONE)[0]["url"], rows("ddg")[0]["url"])
        self.assertEqual(st["ddg_html"].calls, 2)
        self.assertTrue(status["ok"])
        self.assertEqual(status["cooldown_until"], 0)
        self.assertEqual(status["error"], "")
        self.assertTrue(status["last_error"])   # history is kept
        self.assertEqual(web.SEARCH_FAIL_STREAK, 0)

    def test_all_failing_lists_every_provider_in_chain_order(self):
        self.stubs(brave=Stub(error=web.ProviderError("rejected (HTTP 401, check brave_api_key)")),
                   searxng=Stub(error=web.ProviderError("network error: connection refused")),
                   ddgs=Stub(error=web.ProviderError("blocked (Ratelimit)")),
                   ddg_html=Stub(error=web.ProviderError("blocked (HTTP 202 challenge)")))
        with self.assertRaises(web.WebError) as cm:
            web.search("q", 5, {**ALL, "search_provider": "searxng"})
        self.assertEqual(str(cm.exception), "All search providers failed: searxng: network error: connection refused; "
                         "brave: rejected (HTTP 401, check brave_api_key); ddgs: blocked (Ratelimit); "
                         "ddg_html: blocked (HTTP 202 challenge)")
        self.assertNotIn("brave-key", str(cm.exception))
        for name in web.PROVIDERS:
            self.assertFalse(web.PROVIDER_STATUS[name]["ok"], name)
            self.assertGreater(web.PROVIDER_STATUS[name]["cooldown_until"], time.time(), name)

    def test_search_health_reflects_statuses(self):
        self.patch("_ddgs_installed", lambda: False)
        self.stubs(brave=Stub(rows("brave")), searxng=Stub(error=web.ProviderError("HTTP 500")),
                   ddg_html=Stub(error=web.ProviderError("blocked (HTTP 202 challenge)")))
        settings = {**ALL, "search_provider": "searxng"}
        health = {r["provider"]: r for r in web.search_health(settings)}
        self.assertEqual(list(health), ["searxng", "brave", "ddgs", "ddg_html"])
        self.assertEqual(health["brave"], {"provider": "brave", "configured": True, "status": "unknown",
                                           "detail": "not tried yet"})
        self.assertFalse(health["ddgs"]["configured"])
        self.assertEqual(health["ddgs"]["status"], "unknown")
        self.assertIn("not set up", health["ddgs"]["detail"])
        self.assertIn("pip install ddgs", health["ddgs"]["detail"])
        web.search("q", 3, settings)   # searxng fails, brave answers
        health = {r["provider"]: r for r in web.search_health(settings)}
        self.assertEqual(health["searxng"]["status"], "bad")
        self.assertEqual(health["searxng"]["detail"], "HTTP 500, retry in 10 min")
        self.assertEqual(health["brave"]["status"], "ok")
        self.assertEqual(health["brave"]["detail"], "ok just now")
        self.assertEqual(health["ddg_html"]["status"], "unknown")   # never reached
        # after the cooldown a failed provider is only a warning: it will be tried again
        web.PROVIDER_STATUS["searxng"]["cooldown_until"] = time.time() - 1
        health = {r["provider"]: r for r in web.search_health(settings)}
        self.assertEqual(health["searxng"]["status"], "warn")
        self.assertEqual(health["searxng"]["detail"], "HTTP 500 (just now), will be retried")
        # keys never show up in health rows
        self.assertNotIn("brave-key", json.dumps(list(health.values())))

    def test_live_search_test_ignores_cooldown(self):
        self.patch("_ddgs_installed", lambda: False)
        st = self.stubs(ddg_html=Stub(rows("ddg", 3)))
        web.PROVIDER_STATUS["ddg_html"].update({"ok": False, "error": "blocked (HTTP 202 challenge)",
                                                "cooldown_until": time.time() + 500})
        with self.assertRaises(web.WebError):
            web.search("q", 3, NONE)
        self.assertEqual(st["ddg_html"].calls, 0)
        self.assertEqual(web.live_search_test(NONE), (True, "ddg_html answered with 3 results"))
        self.assertEqual(st["ddg_html"].calls, 1)
        self.assertTrue(web.PROVIDER_STATUS["ddg_html"]["ok"])
        st["ddg_html"].error = web.ProviderError("timed out")
        ok, detail = web.live_search_test(NONE)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("All search providers failed: ddgs: not set up; ddg_html: timed out"))


class TestProviders(ChainCase):
    """The real provider functions with the HTTP layer swapped out."""

    def http(self, status: int, body: str, error: Exception | None = None):
        calls = []

        def fake(url, data=None, headers=None, timeout=20, direct=False):
            calls.append({"url": url, "data": data, "headers": headers or {}, "direct": direct})
            if error:
                raise error
            return status, body
        self.patch("_http", fake)
        return calls

    def test_brave_parses_results_and_keeps_the_key_out_of_errors(self):
        calls = self.http(200, json.dumps({"web": {"results": [
            {"title": "A", "url": "https://a.example.com/", "description": "da"},
            {"title": "B", "url": "https://b.example.com/", "description": "db"}]}}))
        got = web._brave("pricing", 1, ALL)
        self.assertEqual(got, [{"title": "A", "url": "https://a.example.com/", "snippet": "da"}])
        self.assertEqual(calls[0]["headers"]["X-Subscription-Token"], "brave-key")
        self.assertIn("count=1", calls[0]["url"])
        for status, text in ((401, "check brave_api_key"), (403, "rejected (HTTP 403"), (429, "rate limited"),
                             (500, "HTTP 500")):
            self.http(status, '{"error": "x"}')
            with self.assertRaises(web.ProviderError) as cm:
                web._brave("q", 3, ALL)
            self.assertIn(text, str(cm.exception))
            self.assertNotIn("brave-key", str(cm.exception))
        self.http(200, "<html>not json</html>")
        with self.assertRaises(web.ProviderError):
            web._brave("q", 3, ALL)
        self.http(0, "", web.ProviderError("network error: refused"))
        with self.assertRaises(web.ProviderError) as cm:
            web._brave("q", 3, ALL)
        self.assertEqual(str(cm.exception), "network error: refused")

    def test_searxng_parses_results_and_errors(self):
        calls = self.http(200, json.dumps({"results": [
            {"title": "T", "url": "https://t.example.com/", "content": "c"}]}))
        self.assertEqual(web._searxng("q", 3, ALL), [{"title": "T", "url": "https://t.example.com/", "snippet": "c"}])
        self.assertTrue(calls[0]["url"].startswith("http://127.0.0.1:1/search?"))
        self.assertIn("format=json", calls[0]["url"])
        self.assertTrue(calls[0]["direct"])   # your own instance never goes through the system proxy
        self.http(403, "Forbidden")
        with self.assertRaises(web.ProviderError) as cm:
            web._searxng("q", 3, ALL)
        self.assertIn("settings.yml", str(cm.exception))
        self.http(200, "<html>a web page</html>")
        with self.assertRaises(web.ProviderError) as cm:
            web._searxng("q", 3, ALL)
        self.assertIn("not JSON", str(cm.exception))

    def test_configured(self):
        self.assertFalse(web._configured("brave", NONE))
        self.assertTrue(web._configured("brave", ALL))
        self.assertFalse(web._configured("searxng", {"searxng_url": "   "}))
        self.assertTrue(web._configured("searxng", ALL))
        self.assertTrue(web._configured("ddg_html", NONE))
        self.patch("_ddgs_installed", lambda: False)
        self.assertFalse(web._configured("ddgs", ALL))


class FakeDDG:
    """A stand-in for html.duckduckgo.com and lite.duckduckgo.com: each path answers a scripted (status, body)."""

    def __init__(self):
        self.responses: dict[str, tuple[int, str]] = {}
        self.hits: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.hits.append(self.path)
                status, body = outer.responses.get(self.path, (404, "no such page"))
                b = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


RESULTS_PAGE = ('<html><body><div class="results">'
                '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fbots&rut=1">'
                'Chatbots</a>'
                '<a class="result__snippet">The challenge of building bots for support</a>'
                '<a class="result__a" href="https://other.example.com/">Other</a>'
                '<a class="result__snippet">Second</a></div></body></html>')
ANOMALY_PAGE = ('<html><body><div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>'
                '<p>Please complete the following challenge to confirm this search was made by a human.</p>'
                '</body></html>')


class TestDDGHtml(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeDDG().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        web.reset_status()
        self.server.responses.clear()
        self.server.hits.clear()
        p = mock.patch.object(web, "DDG_HTML_ENDPOINTS", (self.server.url + "/html/", self.server.url + "/lite/"))
        p.start()
        self.addCleanup(p.stop)

    def test_results_page_wins_even_when_snippets_mention_bots(self):
        self.server.responses["/html/"] = (200, RESULTS_PAGE)
        got = web._ddg_html("support bots", 5, NONE)
        self.assertEqual([r["url"] for r in got], ["https://example.com/bots", "https://other.example.com/"])
        self.assertEqual(got[0]["snippet"], "The challenge of building bots for support")
        self.assertEqual(self.server.hits, ["/html/"])

    def test_202_anomaly_page_is_a_block_on_both_endpoints(self):
        self.server.responses["/html/"] = (202, ANOMALY_PAGE)
        self.server.responses["/lite/"] = (202, ANOMALY_PAGE)
        with self.assertRaises(web.ProviderError) as cm:
            web._ddg_html("q", 5, NONE)
        self.assertEqual(str(cm.exception), "blocked (HTTP 202 challenge)")
        self.assertEqual(self.server.hits, ["/html/", "/lite/"])

    def test_challenge_text_without_results_and_429_are_blocks(self):
        self.server.responses["/html/"] = (200, ANOMALY_PAGE)
        self.server.responses["/lite/"] = (429, "Too Many Requests")
        with self.assertRaises(web.ProviderError) as cm:
            web._ddg_html("q", 5, NONE)
        self.assertTrue(str(cm.exception).startswith("blocked ("), str(cm.exception))
        self.assertIn("anomaly", str(cm.exception))
        self.assertEqual(self.server.hits, ["/html/", "/lite/"])

    def test_lite_endpoint_answers_when_html_is_blocked(self):
        self.server.responses["/html/"] = (403, "Forbidden")
        self.server.responses["/lite/"] = (200, RESULTS_PAGE.replace("result__a", "result-link")
                                           .replace("result__snippet", "result-snippet"))
        got = web._ddg_html("q", 1, NONE)
        self.assertEqual(got, [{"title": "Chatbots", "url": "https://example.com/bots",
                                "snippet": "The challenge of building bots for support"}])

    def test_real_empty_answer_and_unknown_page(self):
        self.server.responses["/html/"] = (200, '<html><body><div class="no-results">No results.</div></body></html>')
        self.assertEqual(web._ddg_html("zzqqxx", 5, NONE), [])
        self.server.responses["/html/"] = (200, "<html><body><p>Welcome to a page without results</p></body></html>")
        self.server.responses["/lite/"] = (500, "Server Error")
        with self.assertRaises(web.ProviderError) as cm:
            web._ddg_html("q", 5, NONE)
        self.assertEqual(str(cm.exception), "HTTP 500")

    def test_network_failure_is_reported_not_raised_elsewhere(self):
        with mock.patch.object(web, "DDG_HTML_ENDPOINTS", ("http://127.0.0.1:1/html/", "http://127.0.0.1:1/lite/")):
            with self.assertRaises(web.ProviderError) as cm:
                web._ddg_html("q", 5, NONE)
        self.assertIn("network error", str(cm.exception))

    def test_blocked_through_the_chain_sets_cooldown(self):
        self.server.responses["/html/"] = (202, ANOMALY_PAGE)
        self.server.responses["/lite/"] = (202, ANOMALY_PAGE)
        with mock.patch.object(web, "_ddgs_installed", lambda: False):
            with self.assertRaises(web.WebError) as cm:
                web.search("q", 3, NONE)
        self.assertIn("ddg_html: blocked (HTTP 202 challenge)", str(cm.exception))
        self.assertGreater(web.PROVIDER_STATUS["ddg_html"]["cooldown_until"], time.time())


class TestWebSearchTool(ChainCase):
    def setUp(self):
        super().setUp()
        self.patch("_ddgs_installed", lambda: False)
        self.company = make_company()

    def ctx(self, company=None) -> tools.Ctx:
        c = ctx_for(company or self.company, "ceo", kind="founding")   # founding runs always have web_search
        c.settings = dict(NONE)
        return c

    def errors(self, company) -> list[dict]:
        return db.q("SELECT * FROM events WHERE company_id=? AND kind='error' ORDER BY id", (company["id"],))

    def test_outage_message_and_single_error_event(self):
        self.stubs(ddg_html=Stub(error=web.ProviderError("blocked (HTTP 202 challenge)")))
        out = tools.execute(self.ctx(), "web_search", {"query": "dog food market"})
        first, rest = out.split("\n", 1)
        self.assertEqual(first, "ERROR: web search is unavailable right now (ddgs: not set up; "
                         "ddg_html: blocked (HTTP 202 challenge); brave: not set up; searxng: not set up).")
        self.assertIn("Do not retry web_search this session", rest)
        self.assertIn("fetch_url", rest)
        self.assertIn("note_progress", rest)
        self.assertIn("research was skipped", rest)
        self.assertNotIn("\u2014", out)   # no em dashes in agent-facing text
        events = self.errors(self.company)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], "Web search is unavailable: ddgs: not set up; "
                         "ddg_html: blocked (HTTP 202 challenge); brave: not set up; searxng: not set up")
        self.assertEqual(events[0]["agent_id"], self.ctx().agent["id"])
        # a second failure inside the half hour tells the agent again but not the owner
        out2 = tools.execute(self.ctx(), "web_search", {"query": "another"})
        self.assertTrue(out2.startswith("ERROR: web search is unavailable right now"))
        self.assertIn("cooling down", out2)
        self.assertEqual(len(self.errors(self.company)), 1)
        # another company gets its own single event
        other = make_company("OtherCo")
        tools.execute(self.ctx(other), "web_search", {"query": "x"})
        self.assertEqual(len(self.errors(other)), 1)
        self.assertEqual(len(self.errors(self.company)), 1)
        # after the interval the owner is told again
        web.LAST_SEARCH_ALERT[self.company["id"]] = time.time() - web.ALERT_INTERVAL - 1
        tools.execute(self.ctx(), "web_search", {"query": "y"})
        self.assertEqual(len(self.errors(self.company)), 2)

    def test_results_and_empty_answers_are_not_outages(self):
        st = self.stubs(ddg_html=Stub(rows("ddg")))
        out = tools.execute(self.ctx(), "web_search", {"query": "dog food market", "max_results": 1})
        self.assertIn("1. ddg result 1\nhttps://ddg.example.com/1\ntext", out)
        self.assertNotIn("2.", out)
        st["ddg_html"].result = []
        out = tools.execute(self.ctx(), "web_search", {"query": "zzqqxx"})
        self.assertEqual(out, "No results. Try different words.")
        self.assertEqual(self.errors(self.company), [])

    def test_alias_and_renamed_args_still_reach_the_chain(self):
        # gpt-oss calls its built-in browser search "browser.search" with q/topn. ("search" alone is Holdco's own
        # search over company knowledge since Phase 0.5; see tests/test_context.py for the old meaning.)
        st = self.stubs(ddg_html=Stub(rows("ddg")))
        out = tools.execute(self.ctx(), "browser.search", {"q": "pricing", "topn": 1})
        self.assertIn("ddg result 1", out)
        self.assertEqual(st["ddg_html"].calls, 1)


if __name__ == "__main__":
    unittest.main()
