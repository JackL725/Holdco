"""Phase 0.5 context system: the index, search, line-based reading, grep, edit_file, list_files summaries, the
owner's context library, the prompt sections and the session trimming that keeps references.

Run with:  python -m unittest tests.test_context -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HOLDCO_DATA", tempfile.mkdtemp(prefix="holdco-context-"))

from holdco import agents, config, context, db, planning, roles, routing, tools, truth, watchdog, workspace  # noqa: E402
from holdco.routing import HTTPError  # noqa: E402

from tests.fake_ollama import EMBED_FAIL_MODEL, FakeModelServer  # noqa: E402

db.init()


def make_company(name="CtxCo", members=("ceo", "engineering", "marketing", "research")) -> dict:
    slug = workspace.slugify(name) + str(int(time.time() * 1e6) % 10**9)
    cid = db.run("INSERT INTO companies(name, slug, idea, status, stage, settings, created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, slug, "Lead generation for roofers in Phoenix", "active", "building", "{}", db.now()))
    company = db.get_company(cid)
    workspace.create(company)
    for m in members:
        roles.add_agent(cid, preset=m)
    return company


def ctx_for(company, role="engineering", task=None, kind="task") -> tools.Ctx:
    agent = db.one("SELECT * FROM agents WHERE company_id=? AND role=?", (company["id"], role))
    c = tools.Ctx(company=company, agent=agent, kind=kind, run_id=0, task=task, settings=db.get_settings())
    c.allowed = {t["name"] for t in tools.role_tools(agent, kind)}
    return c


def clear_global() -> None:
    """The global library and settings are shared by every test module in this process: leave them empty."""
    shutil.rmtree(context.global_dir(), ignore_errors=True)
    db.set_settings({"pinned_context": [], "embedding_model": ""})
    context.reindex_global()


class Handler:
    """Stands in for the HTTP handler: route functions only read .query."""

    def __init__(self, query: dict | None = None):
        self.query = {k: [v] for k, v in (query or {}).items()}


def quiet():
    """Hide the log lines of failures a test causes on purpose."""
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    return stack


def call(method: str, path: str, body=None, query: dict | None = None):
    """Dispatch like server.Handler does, so the route patterns are tested too."""
    for mth, rx, fn in routing.ROUTES:
        if mth == method and (m := rx.match(path)):
            return fn(Handler(query), m, body)
    raise AssertionError(f"no route for {method} {path}")


class Base(unittest.TestCase):
    def setUp(self):
        clear_global()
        self.c = make_company()
        self.root = workspace.root(self.c)
        self.ctx = ctx_for(self.c)

    def tearDown(self):
        clear_global()

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
        return p

    def tool(self, name: str, **args) -> str:
        return tools.execute(self.ctx, name, args)

    def agent_id(self, role="engineering") -> int:
        return db.one("SELECT id FROM agents WHERE company_id=? AND role=?", (self.c["id"], role))["id"]

    def stamps(self, source="file") -> dict:
        return context._states(self.c["id"], source)


# ====================================================================== chunking and summaries

class TestChunking(unittest.TestCase):
    def test_markdown_splits_at_headings_and_keeps_line_numbers(self):
        lines = ["Intro", "", "# Alpha", "alpha text", "```", "# not a heading inside code", "```", "",
                 "## Beta"] + [f"beta line {i}" for i in range(70)]
        chunks = context.chunk_file("notes.md", "\n".join(lines) + "\n")
        self.assertEqual([(c[0], c[1]) for c in chunks], [(1, ""), (3, "Alpha"), (9, "Beta"), (69, "Beta")])
        self.assertIn("# not a heading inside code", chunks[1][2])
        self.assertTrue(chunks[2][2].startswith("## Beta"))

    def test_code_is_split_in_60_line_windows_with_the_nearest_definition(self):
        lines = [f"x{i} = {i}" for i in range(130)]
        lines[64] = "def price_for(lead):"
        chunks = context.chunk_file("code/app.py", "\n".join(lines))
        self.assertEqual([c[0] for c in chunks], [1, 61, 121])
        self.assertEqual(chunks[1][1], "def price_for(lead):")
        self.assertEqual(chunks[2][1], "def price_for(lead):")   # the window after it keeps the last definition
        self.assertEqual(chunks[0][1], "")

    def test_csv_keeps_header_and_first_50_rows(self):
        rows = ["name,price"] + [f"roofer{i},{i}" for i in range(120)]
        chunks = context.chunk_file("sales/leads.csv", "\n".join(rows))
        self.assertEqual(len(chunks), 1)
        line, heading, body = chunks[0]
        self.assertEqual((line, heading), (1, "columns: name,price"))
        self.assertIn("roofer49,49", body)
        self.assertNotIn("roofer50,50", body)
        self.assertIn("(70 more rows)", body)

    def test_html_becomes_text_with_headings_at_their_source_lines(self):
        page = ("<!doctype html>\n<html><head><title>Roof Leads</title>\n<style>.x{color:red}</style></head>\n"
                "<body>\n<h1>Find a roofer</h1>\n<p>Free quotes in Phoenix.</p>\n<script>var secret=1;</script>\n"
                "\n<h2 class='big'>Pricing</h2>\n<p>Plans start at $9.</p>\n</body></html>\n")
        chunks = context.chunk_file("site/index.html", page)
        by_heading = {c[1]: c for c in chunks}
        self.assertEqual(by_heading["Find a roofer"][0], 5)
        self.assertEqual(by_heading["Pricing"][0], 9)
        self.assertIn("Plans start at $9.", by_heading["Pricing"][2])
        joined = "\n".join(c[2] for c in chunks)
        self.assertIn("Roof Leads", joined)
        self.assertNotIn("secret", joined)
        self.assertNotIn("color:red", joined)

    def test_summaries(self):
        self.assertEqual(context.summarize("a.md", "Some intro\n\n# Market for roofers\n"), "Market for roofers")
        self.assertEqual(context.summarize("i.html", "<html><head><title>Roof &amp; Co</title>"), "Roof & Co")
        self.assertEqual(context.summarize("l.csv", "name,email\nJo,jo@x.co"), "Table with columns: name,email")
        self.assertEqual(context.summarize("c.json", '{"plan": 1, "price": 2}'), "JSON with keys: plan, price")
        self.assertEqual(context.summarize("t.py", '"""Price leads for roofers. More."""\nimport os\n'),
                         "Price leads for roofers.")
        long = context.summarize("n.txt", "word " * 100)
        self.assertLessEqual(len(long), 80)
        self.assertTrue(long.endswith("..."))

    def test_no_em_dashes_in_agent_or_owner_text(self):
        source = (ROOT / "holdco" / "context.py").read_text(encoding="utf-8")
        self.assertNotIn("\u2014", source)


# ====================================================================== the index

class TestIndex(Base):
    def seed_everything(self) -> dict:
        cid, aid = self.c["id"], self.agent_id()
        self.write("research/market.md", "# Market\n\n## Competitors\n\nZebracorp charges roofers $40 per lead.\n")
        mid = db.run("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(?,?,?,?,?,?)",
                     (cid, aid, "decision", "Never call homeowners after 8pm (quokka rule).", 0, db.now()))
        tid = db.run("INSERT INTO tasks(company_id, title, description, role, status, created_by, acceptance, "
                     "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (cid, "Build the narwhal calculator", "A page that estimates roof cost", "engineering", "todo",
                      "test", "- shows a price", db.now(), db.now()))
        eid = db.run("INSERT INTO emails(company_id, direction, from_addr, to_addr, subject, body, status, created_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (cid, "in", "jo@roofs.example", "us@x.co", "Partnership idea",
                                                 "We could send you axolotl leads weekly.", "new", db.now()))
        chat = db.run("INSERT INTO messages(company_id, agent_id, sender, content, created_at) VALUES(?,?,?,?,?)",
                      (cid, aid, "owner", "Please focus on the pelican campaign first.", db.now()))
        aprid = db.run("INSERT INTO approvals(company_id, agent_id, action, summary, payload, status, created_at) "
                       "VALUES(?,?,?,?,?,?,?)", (cid, aid, "deploy_site", "Deploy the flamingo landing page",
                                                 json.dumps({"folder": "site"}), "pending", db.now()))
        rid = db.run("INSERT INTO runs(company_id, agent_id, kind, status, summary, started_at, finished_at) "
                     "VALUES(?,?,?,?,?,?,?)", (cid, aid, "task", "done", "Wrote the walrus pricing table.",
                                               db.now(), db.now()))
        fid = db.run("INSERT INTO facts(company_id, agent_id, claim, url, quote, created_at) VALUES(?,?,?,?,?,?)",
                     (cid, aid, "Roofers pay for platypus leads", "https://example.com/p", "pay per lead", db.now()))
        return {"memory": mid, "task": tid, "email": eid, "chat": chat, "approval": aprid, "run": rid, "fact": fid}

    def test_every_source_is_indexed_and_searchable_by_scope(self):
        ids = self.seed_everything()
        context.reindex(self.c)
        cases = [("files", "zebracorp", "file", "research/market.md"), ("memory", "quokka", "memory", ids["memory"]),
                 ("tasks", "narwhal", "task", ids["task"]), ("emails", "axolotl", "email", ids["email"]),
                 ("chat", "pelican", "chat", ids["chat"]), ("decisions", "flamingo", "approval", ids["approval"]),
                 ("sessions", "walrus", "run", ids["run"]), ("facts", "platypus", "fact", ids["fact"])]
        for scope, word, source, ref in cases:
            rows = context.search_rows(self.c, word, scope)
            self.assertTrue(rows, f"{scope}: nothing found for {word}")
            self.assertEqual(rows[0]["source"], source)
            if source == "file":
                self.assertEqual((rows[0]["path"], rows[0]["line"]), (ref, 5))
            else:
                self.assertEqual(rows[0]["id"], ref)
            self.assertIn(word, (rows[0]["heading"] + rows[0]["snippet"]).lower())
            # a scope never returns other sources
            other = "memory" if scope != "memory" else "tasks"
            self.assertFalse([r for r in context.search_rows(self.c, word, other) if r["source"] == source])
        out = self.tool("search", query="narwhal")
        self.assertIn(f"task #{ids['task']} | Build the narwhal calculator [todo]", out)
        self.assertIn("list_tasks status=all", out)
        self.assertIn(f"session #{ids['run']}", self.tool("search", query="walrus", scope="sessions"))
        self.assertIn(f"approval #{ids['approval']}", self.tool("search", query="flamingo"))

    def test_changed_file_is_reindexed_and_others_are_left_alone(self):
        self.write("docs/a.md", "# A\n\nThe old wording about otters.\n")
        self.write("docs/b.md", "# B\n\nNothing changes here.\n")
        context.reindex(self.c)
        before = self.stamps()
        p = self.write("docs/a.md", "# A\n\nThe new wording about beavers, longer now.\n")
        os.utime(p, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
        context.reindex(self.c)
        after = self.stamps()
        self.assertEqual(before["docs/b.md"], after["docs/b.md"])
        self.assertNotEqual(context._parse(before["docs/a.md"])[0], context._parse(after["docs/a.md"])[0])
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM search_index WHERE company_id=? AND ref='docs/a.md'",
                                   (self.c["id"],)), 1)   # the old chunk was replaced, not added to
        self.assertTrue(context.search_rows(self.c, "beavers"))
        self.assertFalse(context.search_rows(self.c, "otters"))

    def test_deleted_file_is_forgotten(self):
        p = self.write("docs/gone.md", "# Gone\n\nmarmoset facts\n")
        context.reindex(self.c)
        self.assertTrue(context.search_rows(self.c, "marmoset"))
        p.unlink()
        context.reindex(self.c)
        self.assertFalse(context.search_rows(self.c, "marmoset"))
        self.assertNotIn("docs/gone.md", self.stamps())

    def test_new_memory_and_task_updates_are_found(self):
        context.reindex(self.c)
        tools.execute(self.ctx, "remember", {"content": "The owner prefers the capybara brand name."})
        self.assertIn("capybara", self.tool("search", query="capybara", scope="memory"))   # the tool reindexes first
        ceo = ctx_for(self.c, "ceo")
        out = tools.execute(ceo, "create_task", {"title": "Survey roofers", "assign_to": "engineering"})
        tid = int(out.split("#")[1].split(" ")[0])
        context.reindex(self.c)
        self.assertFalse(context.search_rows(self.c, "wombat", "tasks"))
        tools.execute(ceo, "update_task", {"task_id": tid, "note": "Ask about wombat insurance"})
        context.reindex(self.c)
        self.assertEqual(context.search_rows(self.c, "wombat", "tasks")[0]["id"], tid)
        context.reindex(self.c)
        watchdog.add_note(tid, "Tried the gecko list, it was empty", "auto")   # does not touch updated_at
        context.reindex(self.c)
        self.assertEqual(context.search_rows(self.c, "gecko", "tasks")[0]["id"], tid)

    def test_memory_edits_and_deletes_follow(self):
        mid = db.run("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(?,?,?,?,?,?)",
                     (self.c["id"], self.agent_id(), "note", "Use the heron logo.", 0, db.now()))
        context.reindex(self.c)
        db.update("memory", mid, content="Use the egret logo.")
        context.reindex(self.c)
        self.assertFalse(context.search_rows(self.c, "heron", "memory"))
        self.assertTrue(context.search_rows(self.c, "egret", "memory"))
        db.run("DELETE FROM memory WHERE id=?", (mid,))
        context.reindex(self.c)
        self.assertFalse(context.search_rows(self.c, "egret", "memory"))

    def test_skipped_folders_binaries_and_big_files(self):
        for rel in (".runs/1.json", ".trash/x/old.md", ".cache/pages/a.txt", "node_modules/pkg/readme.md",
                    "code/.git/notes.txt"):
            self.write(rel, "ocelot\n")
        (self.root / "site" / "logo.png").write_bytes(b"\x89PNG ocelot")
        (self.root / "docs" / "blob.dat").write_bytes(b"ocelot\x00\x01\x02")
        self.write("docs/huge.txt", "ocelot " + "x" * 2_100_000)
        self.write("records/STATUS.md", "# Status\n\nNo ocelot deployed yet.\n")
        context.reindex(self.c)
        paths = {r["path"] for r in context.search_rows(self.c, "ocelot", limit=20)}
        self.assertEqual(paths, {"records/STATUS.md"})
        self.assertNotIn(".runs/1.json", self.stamps())
        context.reindex(self.c)   # binaries keep a stamp, so they are not read again
        self.assertIn("docs/blob.dat", self.stamps())

    def test_sessions_are_indexed_when_they_finish_even_out_of_order(self):
        cid, aid = self.c["id"], self.agent_id()
        first = db.run("INSERT INTO runs(company_id, agent_id, kind, status, started_at) VALUES(?,?,?,?,?)",
                       (cid, aid, "task", "running", db.now()))
        second = db.run("INSERT INTO runs(company_id, agent_id, kind, status, summary, started_at, finished_at) "
                        "VALUES(?,?,?,?,?,?,?)", (cid, aid, "task", "done", "Built the toucan page", db.now(),
                                                  db.now()))
        context.reindex(self.c)
        self.assertEqual(context.search_rows(self.c, "toucan", "sessions")[0]["id"], second)
        db.update("runs", first, status="done", summary="Drafted the lemur emails", finished_at=db.now())
        context.reindex(self.c)
        self.assertEqual(context.search_rows(self.c, "lemur", "sessions")[0]["id"], first)
        self.assertEqual(len(context.search_rows(self.c, "toucan", "sessions")), 1)

    def test_a_reused_company_id_starts_with_an_empty_index(self):
        self.write("docs/a.md", "# A\n\nmongoose\n")
        context.reindex(self.c)
        db.run("UPDATE index_state SET stamp='someone-else' WHERE company_id=? AND source='meta' AND ref='company'",
               (self.c["id"],))
        db.run("INSERT INTO search_index(title, heading, body, company_id, source, ref, line, updated_at) "
               "VALUES('x','x','stale meerkat',?,'chat','999',NULL,'')", (self.c["id"],))
        context.reindex(self.c)
        self.assertFalse(context.search_rows(self.c, "meerkat"))
        self.assertTrue(context.search_rows(self.c, "mongoose"))

    def test_reindex_never_raises(self):
        self.write("docs/a.md", "# A\n\nmanatee\n")
        original = context._sync_memory
        context._sync_memory = lambda cid: 1 / 0
        try:
            with quiet():
                context.reindex(self.c)   # the failing source is logged; the others still index
        finally:
            context._sync_memory = original
        self.assertTrue(context.search_rows(self.c, "manatee"))
        context.reindex({"id": 999999, "slug": "no-such-company-folder", "name": "x"})   # no folder: no error

    def test_write_file_and_edit_file_index_right_away(self):
        context.reindex(self.c)
        tools.execute(self.ctx, "write_file", {"path": "docs/plan.md", "content": "# Plan\n\nCall the iguana list.\n"})
        self.assertIn("docs/plan.md", self.stamps())   # after_write, before any reindex
        rows = context._fts_rows(self.c["id"], '"iguana"', ("file",), 5)
        self.assertEqual(rows[0]["ref"], "docs/plan.md")
        tools.execute(self.ctx, "edit_file", {"path": "docs/plan.md", "find": "iguana", "replace": "chameleon"})
        self.assertFalse(context._fts_rows(self.c["id"], '"iguana"', ("file",), 5))
        self.assertTrue(context._fts_rows(self.c["id"], '"chameleon"', ("file",), 5))


# ====================================================================== search

class TestSearch(Base):
    def test_hit_points_at_the_exact_line(self):
        body = ["# Notes", ""] + [f"filler line {i}" for i in range(34)] + ["Roofers in Mesa want gutter quotes."]
        self.write("research/notes.md", "\n".join(body) + "\n")
        out = self.tool("search", query="gutter quotes")
        self.assertIn("research/notes.md:37 | Notes |", out)
        self.assertIn("[gutter] [quotes]", out)
        self.assertIn("read_file <path> start_line=<line>", out)
        self.assertIn("37: Roofers in Mesa want gutter quotes.",
                      self.tool("read_file", path="research/notes.md", start_line=37))

    def test_all_words_rank_first_then_any_word(self):
        self.write("docs/both.md", "# Both\n\nalpaca and bison together\n")
        self.write("docs/one.md", "# One\n\nonly alpaca here\n")
        context.reindex(self.c)
        rows = context.search_rows(self.c, "alpaca bison")
        self.assertEqual([r["path"] for r in rows if r["source"] == "file"][:2], ["docs/both.md", "docs/one.md"])

    def test_scopes_limits_and_errors(self):
        for i in range(30):
            self.write(f"docs/n{i}.md", f"# Note {i}\n\nkoala {i}\n")
        db.run("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(?,?,?,?,?,?)",
               (self.c["id"], self.agent_id(), "note", "koala note", 0, db.now()))
        context.reindex(self.c)
        self.assertEqual(len(context.search_rows(self.c, "koala")), 8)
        self.assertEqual(len(context.search_rows(self.c, "koala", limit=50)), 20)
        self.assertEqual({r["source"] for r in context.search_rows(self.c, "koala", "memory")}, {"memory"})
        self.assertEqual({r["source"] for r in context.search_rows(self.c, "koala", "files, memory", 20)},
                         {"file", "memory"})
        self.assertTrue(self.tool("search", query="koala", scope="planets").startswith("ERROR: unknown scope"))
        self.assertTrue(self.tool("search", query="  ").startswith("ERROR"))
        self.assertIn('No results for "zzzqqq"', self.tool("search", query="zzzqqq"))
        self.assertIn("koala", self.tool("search_files", q="koala"))   # alias and argument rename

    def test_odd_queries_do_not_break_the_query_syntax(self):
        self.write("docs/a.md", "# A\n\nthe NEAR clause and price (per lead)\n")
        context.reindex(self.c)
        for q in ('"price', "price AND (", "NEAR(price", "price*", "-price", "col:price", "price OR", "'; DROP"):
            out = self.tool("search", query=q)
            self.assertFalse(out.startswith("ERROR"), f"{q}: {out}")

    def test_owner_context_is_found_and_readable(self):
        g = context.global_dir()
        g.mkdir(parents=True, exist_ok=True)
        (g / "about-me.md").write_text("# About me\n\nI am a roofer.\nMy budget is $500 a month for heliotrope ads.\n",
                                       encoding="utf-8")
        self.write("context/interviews.md", "# Interviews\n\n## Call 1\n\nSam wants heliotrope shingles.\n")
        out = self.tool("search", query="heliotrope")
        self.assertIn("global:about-me.md:4", out)
        self.assertIn("context:interviews.md:5", out)
        self.assertIn("Owner's context (read-only)", out)
        self.assertIn("4: My budget is $500 a month for heliotrope ads.",
                      self.tool("read_file", path="global:about-me.md"))
        self.assertIn("5: Sam wants heliotrope shingles.", self.tool("read_file", path="context:interviews.md"))
        other = make_company("OtherCo")   # global files belong to every company, company files only to theirs
        refs = {r["ref"] for r in context.search_rows(other, "heliotrope")}
        self.assertEqual(refs, {"global:about-me.md"})
        for bad in ("global:../holdco.db", "global:../../x", "context:../site/index.html", "global:C:/x.md"):
            self.assertTrue(self.tool("read_file", path=bad).startswith("ERROR"), bad)


# ====================================================================== read_file

class TestReadFile(Base):
    def test_default_range_numbers_and_pointer(self):
        self.write("docs/big.md", "\n".join(f"line {i}" for i in range(1, 451)) + "\n")
        out = self.tool("read_file", path="docs/big.md")
        self.assertTrue(out.startswith("docs/big.md: lines 1-200 of 450\n1: line 1\n"))
        self.assertIn("200: line 200", out)
        self.assertNotIn("201: line 201", out)
        self.assertTrue(out.endswith("[lines 201-400 next: read_file docs/big.md start_line=201]"))
        out = self.tool("read_file", path="docs/big.md", start_line=201, end_line=205)
        self.assertTrue(out.startswith("docs/big.md: lines 201-205 of 450\n201: line 201"))
        self.assertIn("[lines 206-405 next: read_file docs/big.md start_line=206]", out)
        self.assertTrue(self.tool("read_file", path="docs/big.md", start_line=440).endswith("[end of file]"))
        self.assertIn("only 450 lines", self.tool("read_file", path="docs/big.md", start_line=999))
        self.assertIn("201: line 201", self.tool("read_file", file="docs/big.md", start=201))   # renamed arguments

    def test_long_reads_stay_under_the_tool_result_limit(self):
        self.write("site/wide.html", "\n".join("<p>" + "w" * 100 + f" {i}</p>" for i in range(1, 301)))
        out = self.tool("read_file", path="site/wide.html")
        self.assertLess(len(out), tools.MAX_RESULT)
        self.assertNotIn("[truncated", out)
        last = int(out.split("lines 1-")[1].split(" ")[0])
        self.assertLess(last, 200)
        self.assertIn(f"next: read_file site/wide.html start_line={last + 1}]", out)
        self.write("site/min.js", "var a=1;" * 1000)
        out = self.tool("read_file", path="site/min.js")
        self.assertIn("[line cut:", out)
        self.assertIn("offset=1500", out)

    def test_outline(self):
        self.write("docs/plan.md", "# Plan\n\ntext\n```\n# code\n```\n## Week 1\n\n### Day 1\n")
        out = self.tool("read_file", path="docs/plan.md", outline=True)
        self.assertIn("docs/plan.md: 9 lines, 3 headings", out)
        self.assertIn("1: # Plan\n7: ## Week 1\n9: ### Day 1", out)
        self.write("site/index.html", "<html>\n<body>\n<h1>Hi</h1>\n<p>x</p>\n<h2 id='p'>Pricing\n plans</h2>\n")
        out = self.tool("read_file", path="site/index.html", outline=True)
        self.assertIn("3: h1: Hi", out)
        self.assertIn("5: h2: Pricing plans", out)
        self.write("code/app.py", "import os\n\nclass Quote:\n    def total(self):\n        return 1\n\n"
                                  "async def main():\n    pass\n")
        out = self.tool("read_file", path="code/app.py", outline=True)
        self.assertIn("3: class Quote:", out)
        self.assertIn("4:     def total(self):", out)
        self.assertIn("7: async def main():", out)
        self.write("site/app.js", "const x = 1;\nfunction go() {}\nexport const add = (a, b) => a + b;\nclass A {}\n")
        out = self.tool("read_file", path="site/app.js", outline=True)
        for needle in ("2: function go() {}", "3: export const add", "4: class A {}"):
            self.assertIn(needle, out)
        self.write("docs/plain.txt", "no headings\nat all\n")
        self.assertIn("No headings found", self.tool("read_file", path="docs/plain.txt", outline=True))

    def test_old_offset_still_works_exactly(self):
        text = "".join(f"row {i}\n" for i in range(3000))
        self.write("docs/old.txt", text)
        out = context.read_file(self.ctx, "docs/old.txt", offset=10)
        self.assertEqual(out, text[10:12010] + f"\n[{len(text) - 12010} more characters: read again with offset=12010]")
        self.assertEqual(context.read_file(self.ctx, "docs/old.txt", offset=len(text) - 5), text[-5:])

    def test_errors(self):
        (self.root / "docs" / "bin.dat").write_bytes(b"abc\x00def")
        self.assertEqual(self.tool("read_file", path="docs/bin.dat"), "ERROR: That's a binary file.")
        self.write("docs/huge.txt", "x" * 2_000_001)
        self.assertEqual(self.tool("read_file", path="docs/huge.txt"), "ERROR: File is too large to read.")
        self.assertIn("doesn't exist", self.tool("read_file", path="nope.md"))
        self.assertIn("outside the company folder", self.tool("read_file", path="../../x"))
        self.assertIn("is a folder", self.tool("read_file", path="site"))
        self.write("docs/empty.md", "")
        self.assertEqual(self.tool("read_file", path="docs/empty.md"), "docs/empty.md is empty.")

    def test_windows_line_ends(self):
        self.write("docs/crlf.txt", "one\r\ntwo\r\nthree\r\n")
        out = self.tool("read_file", path="docs/crlf.txt")
        self.assertIn("lines 1-3 of 3\n1: one\n2: two\n3: three\n[end of file]", out)


# ====================================================================== grep

class TestGrep(Base):
    def test_literal_regex_paths_and_limits(self):
        self.write("site/index.html", "<h1>Roof Quotes</h1>\n<p>roof quotes in Phoenix</p>\n")
        self.write("docs/notes.md", "nothing\nTODO: call Sam\nFIXME later\n")
        self.write(".runs/log.txt", "Roof Quotes\n")
        out = self.tool("grep", pattern="roof quotes")
        self.assertEqual(out.splitlines(), ["site/index.html:1: <h1>Roof Quotes</h1>",
                                            "site/index.html:2: <p>roof quotes in Phoenix</p>"])
        out = self.tool("grep", pattern=r"todo|fixme", regex=True)
        self.assertEqual(out.splitlines(), ["docs/notes.md:2: TODO: call Sam", "docs/notes.md:3: FIXME later"])
        self.assertTrue(self.tool("grep", pattern="(", regex=True).startswith("ERROR: that regular expression"))
        self.assertIn("No matches for 'roof' in docs", self.tool("grep", pattern="roof", path="docs"))
        self.assertEqual(self.tool("grep", pattern="sam", path="docs/notes.md"), "docs/notes.md:2: TODO: call Sam")
        self.assertIn("docs/notes.md:2", self.tool("grep", query="Sam"))   # renamed argument
        self.write("docs/many.txt", "\n".join(f"hit {i}" for i in range(80)))
        out = self.tool("grep", pattern="hit", path="docs/many.txt")
        self.assertEqual(len([x for x in out.splitlines() if x.startswith("docs/many.txt:")]), 50)
        self.assertIn("[stopped at 50 matches", out)
        self.assertTrue(self.tool("grep", pattern="x", path="../").startswith("ERROR"))

    def test_owner_library(self):
        g = context.global_dir()
        g.mkdir(parents=True, exist_ok=True)
        (g / "brand.md").write_text("Never use the word cheap.\n", encoding="utf-8")
        self.assertEqual(self.tool("grep", pattern="cheap", path="global:"), "global:brand.md:1: Never use the word cheap.")


# ====================================================================== edit_file

class TestEditFile(Base):
    PAGE = "\n".join([f"<p>line {i}</p>" for i in range(1, 400)]) + "\n"

    def setUp(self):
        super().setUp()
        self.tid = db.run("INSERT INTO tasks(company_id, title, role, status, created_by, created_at, updated_at) "
                          "VALUES(?,?,?,?,?,?,?)", (self.c["id"], "Fix copy", "engineering", "in_progress", "t",
                                                    db.now(), db.now()))
        self.ctx = ctx_for(self.c, task=db.get_task(self.tid))
        self.page = self.write("site/index.html", self.PAGE)

    def test_one_line_changes_in_a_large_page(self):
        out = self.tool("edit_file", path="site/index.html", find="<p>line 200</p>", replace="<p>Line two hundred</p>")
        self.assertTrue(out.startswith("Edited site/index.html: 1 change."), out)
        self.assertIn("198: <p>line 198</p>\n199: <p>line 199</p>\n200: <p>Line two hundred</p>\n201: <p>line 201</p>",
                      out)
        new = self.page.read_text(encoding="utf-8").split("\n")
        old = self.PAGE.split("\n")
        self.assertEqual([i for i, (a, b) in enumerate(zip(old, new)) if a != b], [199])
        self.assertIn("site/index.html", self.ctx.written)
        ev = db.one("SELECT * FROM events WHERE company_id=? AND kind='file' ORDER BY id DESC", (self.c["id"],))
        self.assertEqual(json.loads(ev["data"]), {"path": "site/index.html", "task_id": self.tid})

    def test_not_found_ambiguous_and_count(self):
        out = self.tool("edit_file", path="site/index.html", find="<p>line 7</p>\n<p>line 9</p>", replace="x")
        self.assertTrue(out.startswith("ERROR: the text to change was not found; read the file around the place "
                                       "you mean with read_file and copy the text exactly."), out)
        self.assertIn("line 7", out)
        out = self.tool("edit_file", path="site/index.html", find="line 1", replace="x")
        self.assertIn("appears 111 times in site/index.html (lines 1, 10, 11, 12, 13, ...)", out)
        self.assertIn("count=111", out)
        self.write("docs/a.md", "cat\ncat\ndog\n")
        self.assertIn("2 changes", self.tool("edit_file", path="docs/a.md", find="cat", replace="cow", count=2))
        self.assertEqual((self.root / "docs/a.md").read_text(encoding="utf-8"), "cow\ncow\ndog\n")
        self.assertTrue(self.tool("edit_file", path="docs/a.md", find="", replace="x").startswith("ERROR: find is empty"))
        self.assertIn("doesn't exist", self.tool("edit_file", path="docs/none.md", find="a", replace="b"))
        self.assertIn("missing required argument(s): replace", self.tool("edit_file", path="docs/a.md", find="dog"))
        self.assertIn("No change", self.tool("edit_file", path="docs/a.md", find="dog", replace="dog"))

    def test_owner_context_is_read_only(self):
        p = self.write("context/notes.md", "owner notes\n")
        for path in ("context/notes.md", "context:notes.md", "global:notes.md", "./context/notes.md"):
            self.assertEqual(self.tool("edit_file", path=path, find="owner", replace="agent"),
                             "ERROR: context/ is the owner's. Save your notes elsewhere, for example docs/.")
        self.assertEqual(p.read_text(encoding="utf-8"), "owner notes\n")

    def test_guards_run_and_can_refuse(self):
        seen = []
        orig_guard, orig_hook = truth.write_guard, planning.charter_write_hook

        def refuse(ctx, rel, content, is_edit=False):
            seen.append(("guard", rel, is_edit, "Line two hundred" in content))
            return "ERROR: records say no."

        def hook(ctx, rel, content):
            seen.append(("hook", rel))
            return None

        truth.write_guard, planning.charter_write_hook = refuse, hook
        try:
            out = self.tool("edit_file", path="site/index.html", find="<p>line 200</p>",
                            replace="<p>Line two hundred</p>")
            self.assertEqual(out, "ERROR: records say no.")
            self.assertEqual(self.page.read_text(encoding="utf-8"), self.PAGE)
            self.assertEqual(seen, [("guard", "site/index.html", True, True)])
            self.assertNotIn("site/index.html", self.ctx.written)
            truth.write_guard = lambda ctx, rel, content: None   # an older signature without is_edit still works
            self.assertTrue(self.tool("edit_file", path="site/index.html", find="<p>line 200</p>",
                                      replace="<p>ok</p>").startswith("Edited"))
            self.assertEqual(seen[-1], ("hook", "site/index.html"))
        finally:
            truth.write_guard, planning.charter_write_hook = orig_guard, orig_hook

    def test_forgiving_inputs(self):
        out = self.tool("edit_file", path="site/index.html", find="5: <p>line 5</p>\n6: <p>line 6</p>",
                        replace="5: <p>five</p>\n6: <p>six</p>")
        self.assertTrue(out.startswith("Edited"), out)
        self.assertIn("<p>five</p>\n<p>six</p>", self.page.read_text(encoding="utf-8"))
        p = self.write("docs/win.txt", "alpha\r\nbeta\r\ngamma\r\n")
        self.assertTrue(self.tool("edit_file", path="docs/win.txt", find="alpha\nbeta", replace="one\ntwo")
                        .startswith("Edited"))
        self.assertEqual(p.read_bytes(), b"one\r\ntwo\r\ngamma\r\n")
        self.assertTrue(self.tool("str_replace", file_path="docs/win.txt", old_str="gamma", new_str="three")
                        .startswith("Edited"))
        self.assertTrue(self.tool("edit_file", path="docs/win.txt", old_string="three", new_string="3")
                        .startswith("Edited"))


# ====================================================================== list_files

class TestListFiles(Base):
    def test_summaries_from_the_index(self):
        self.write("research/market.md", "# Phoenix roofing market\n\nDetails.\n")
        self.write("site/index.html", "<html><head><title>Roof Leads</title></head></html>")
        (self.root / "site" / "logo.png").write_bytes(b"\x89PNG")
        context.reindex(self.c)
        out = self.tool("list_files")
        self.assertIn("research/\n", out)
        self.assertIn("research/market.md (35 bytes): Phoenix roofing market", out)
        self.assertIn("site/index.html (51 bytes): Roof Leads", out)
        self.assertIn("site/logo.png (4 bytes)\n", out + "\n")
        self.assertIn("(empty)", self.tool("list_files", path="ops"))

    def test_library_listing(self):
        g = context.global_dir()
        g.mkdir(parents=True, exist_ok=True)
        (g / "about.md").write_text("# Who I am\n", encoding="utf-8")
        (g / "scan.pdf").write_bytes(b"%PDF")
        context.reindex_global()
        out = self.tool("list_files", path="global:")
        self.assertIn("global:about.md (11 bytes): Who I am", out)
        self.assertIn("global:scan.pdf (4 bytes): not a text file", out)
        self.assertIn("(empty: the owner has not put any context files in context:)",
                      self.tool("list_files", path="context:"))


# ====================================================================== prompt sections

CHARTER = """# Roofer Leads

## One-liner
Phoenix homeowners get three checked roofer quotes in a day.

## Problem
Homeowners do not know whom to trust.

## Customer
Homeowners in Phoenix with roofs older than 15 years, and roofing companies that pay for qualified leads. """ + \
    "They search on Google after storms and ask neighbours. " * 12 + """

## Offer & pricing
Free for homeowners; roofers pay $40 per qualified lead (an estimate to test).

## Go-to-market
Local SEO pages per neighbourhood, Nextdoor posts, and direct calls to 30 roofers.

## Competitors & how we're different
Angi, HomeAdvisor, Thumbtack.

## Riskiest assumption
At least 5 of 30 roofers will pay $40 for a lead in week two.

## North Star metric
Qualified leads delivered per week.

## 30-day plan
Week 1: talk to roofers.
"""


class TestPromptSections(Base):
    def test_company_core_uses_the_charter_key_points(self):
        db.update("companies", self.c["id"], charter=CHARTER, mission="Three checked roofer quotes in a day")
        core = context.company_core(db.get_company(self.c["id"]))
        self.assertLessEqual(len(core), context.CORE_CAP)
        self.assertTrue(core.startswith(f"Name: {self.c['name']}\nOne-liner: Three checked roofer quotes in a day\n"
                                        "Stage: building\nOriginal idea: Lead generation"))
        for needle in ("Customer: Homeowners in Phoenix", "Offer and price: Free for homeowners; roofers pay $40",
                       "How we reach customers: Local SEO pages", "North Star: Qualified leads delivered per week.",
                       "Riskiest assumption: At least 5 of 30 roofers", "Full charter: read_file docs/charter.md"):
            self.assertIn(needle, core)
        self.assertNotIn("Angi", core)
        self.assertNotIn("Not in the charter yet", core)

    def test_company_core_names_missing_sections_and_falls_back(self):
        db.update("companies", self.c["id"], charter="# X\n\n## One-liner\nMeal plans for dogs.\n\n## Problem for "
                                                      "customers\nThey guess.\n\n**Customer:** Dog owners.\n")
        core = context.company_core(db.get_company(self.c["id"]))
        self.assertIn("One-liner: Meal plans for dogs.", core)   # from the charter when the mission is empty
        self.assertIn("Customer: Dog owners.", core)
        self.assertNotIn("They guess", core)
        self.assertIn("Not in the charter yet: Offer and price, How we reach customers, North Star, Riskiest "
                      "assumption.", core)
        free_text = "We will sell things to people. " * 100
        db.update("companies", self.c["id"], charter=free_text)
        core = context.company_core(db.get_company(self.c["id"]))
        self.assertLessEqual(len(core), context.CORE_CAP)
        self.assertIn("Charter:\nWe will sell things", core)
        self.assertTrue(core.endswith("[charter cut here; the full text is in docs/charter.md]"))
        db.update("companies", self.c["id"], charter="")
        self.assertIn("Charter:\n(not written yet)", context.company_core(db.get_company(self.c["id"])))
        self.write("docs/charter.md", CHARTER)   # a charter that exists only as a file still counts
        self.assertIn("Customer: Homeowners", context.company_core(db.get_company(self.c["id"])))

    def test_memory_section_pinned_first_then_relevant_within_cap(self):
        cid, aid = self.c["id"], self.agent_id()
        add = lambda text, pinned=0, kind="note": db.run(
            "INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(?,?,?,?,?,?)",
            (cid, aid, kind, text, pinned, db.now()))
        self.assertEqual(context.memory_section(self.c, None, None), "")
        add("The owner wants plain language everywhere.", pinned=1)
        add("Gutter installers in Tempe answered our survey: they pay $25 per lead.", kind="fact")
        for i in range(30):
            add(f"Unrelated note number {i} about office chairs and coffee. " * 3)
        context.reindex(self.c)
        task = {"title": "Price the gutter leads", "description": "Decide what Tempe installers pay"}
        sec = context.memory_section(self.c, None, task)
        self.assertLessEqual(len(sec), context.MEMORY_CAP)
        self.assertTrue(sec.startswith("- (pinned) The owner wants plain language everywhere."))
        self.assertIn("they pay $25 per lead", sec)   # older than the 14 newest, found by search
        self.assertIn('search("words", scope="memory")', sec)
        chat = context.memory_section(self.c, None, None)
        self.assertNotIn("$25 per lead", chat)   # planning and chat get the newest notes
        self.assertIn("Unrelated note number 29", chat)
        self.assertLessEqual(len(chat), context.MEMORY_CAP)

    def test_pinned_section(self):
        self.assertEqual(context.pinned_section(self.c), "")
        g = context.global_dir()
        g.mkdir(parents=True, exist_ok=True)
        (g / "about-me.md").write_text("# About me\nI only have weekends.\n", encoding="utf-8")
        self.write("context/brand.md", "# Brand\nNever say cheap.\n")
        db.set_settings({"pinned_context": ["about-me.md", "missing.md"]})
        cs = db.company_settings(self.c)
        cs["pinned_context"] = ["context/brand.md"]
        db.update("companies", self.c["id"], settings=json.dumps(cs))
        c = db.get_company(self.c["id"])
        sec = context.pinned_section(c)
        self.assertTrue(sec.startswith("# Owner context"))
        self.assertIn("## global:about-me.md\n# About me\nI only have weekends.", sec)
        self.assertIn("## context:brand.md\n# Brand\nNever say cheap.", sec)
        (g / "big.md").write_text("\n".join(f"Rule {i}: be careful with item {i}." for i in range(300)), encoding="utf-8")
        (g / "bigger.md").write_text("x " * 3000, encoding="utf-8")
        db.set_settings({"pinned_context": ["about-me.md", "big.md", "bigger.md"]})
        sec = context.pinned_section(c)
        self.assertLessEqual(len(sec), context.PINNED_CAP)
        self.assertIn("[cut for room; read the rest with read_file global:big.md start_line=", sec)
        self.assertIn("Pinned files cut for room (read them with read_file): global:bigger.md, context:brand.md", sec)
        prompt = agents.system_prompt(c, db.get_agent(self.agent_id()), "task", 14)
        self.assertIn("# Owner context (always included", prompt)

    def test_index_section(self):
        self.assertEqual(context.index_section(self.c), "")
        g = context.global_dir()
        g.mkdir(parents=True, exist_ok=True)
        (g / "about-me.md").write_text("# Who the owner is\n", encoding="utf-8")
        (g / "photo.jpg").write_bytes(b"\xff\xd8")
        self.write("context/calls.md", "# Customer calls in May\n")
        self.write("docs/charter.md", CHARTER)
        self.write("research/market.md", "# Phoenix roofing market\n")
        db.set_settings({"pinned_context": ["about-me.md"]})
        sec = context.index_section(self.c)
        self.assertLessEqual(len(sec), context.INDEX_CAP)
        self.assertTrue(sec.startswith("# Context you can open\n"))
        for needle in ("- global:about-me.md: Who the owner is (always included above)",
                       "- context:calls.md: Customer calls in May", "- docs/charter.md: Roofer Leads",
                       "- research/market.md: Phoenix roofing market"):
            self.assertIn(needle, sec)
        self.assertNotIn("photo.jpg", sec)
        self.assertTrue(sec.endswith("past sessions, facts and web pages your team fetched."))
        self.assertIn('search("your words")', sec)
        for i in range(60):
            (g / f"note-{i:02d}.md").write_text(f"# Note number {i} with a fairly long title for testing\n",
                                                encoding="utf-8")
        sec = context.index_section(self.c)
        self.assertLessEqual(len(sec), context.INDEX_CAP)
        self.assertIn("more (list_files global: or list_files context:)", sec)
        prompt = agents.system_prompt(self.c, db.get_agent(self.agent_id()), "task", 14)
        self.assertIn("# Context you can open", prompt)


# ====================================================================== the owner's library API

class TestLibraryApi(Base):
    def test_global_library_flow(self):
        self.assertEqual(call("GET", "/api/context")["files"], [])
        r = call("PUT", "/api/context/file", {"path": "owner/about-me.md", "content": "# About me\nI like teal.\n"})
        self.assertEqual(r["path"], "owner/about-me.md")
        listing = call("GET", "/api/context")
        f = listing["files"][0]
        self.assertEqual((f["path"], f["ref"], f["indexed"], f["pinned"], f["text"]),
                         ("owner/about-me.md", "global:owner/about-me.md", True, False, True))
        self.assertEqual(f["summary"], "About me")
        self.assertEqual(listing["pinned_cap"], 2500)
        self.assertEqual(call("GET", "/api/context/file", query={"path": "owner/about-me.md"})["content"],
                         "# About me\nI like teal.\n")
        self.assertEqual(context.search_rows(self.c, "teal")[0]["ref"], "global:owner/about-me.md")
        pin = call("PUT", "/api/context/pinned", {"paths": ["owner/about-me.md", "nope.md", "../x.md"]})
        self.assertEqual(pin["pinned"], ["owner/about-me.md"])
        self.assertEqual(pin["ignored"], ["nope.md", "../x.md"])
        self.assertEqual(db.get_settings()["pinned_context"], ["owner/about-me.md"])
        self.assertIn("I like teal.", context.pinned_section(self.c))
        self.assertTrue(call("GET", "/api/context")["files"][0]["pinned"])
        d = call("DELETE", "/api/context/file", {"path": "owner/about-me.md"})
        self.assertTrue(Path(d["trash"]).is_file())
        self.assertIn(str(config.TRASH_DIR / "context"), d["trash"])
        self.assertEqual(call("GET", "/api/context")["files"], [])
        self.assertEqual(db.get_settings()["pinned_context"], [])
        self.assertFalse(context.search_rows(self.c, "teal"))

    def test_refusals(self):
        for body, needle in (({"path": "scan.pdf", "content": "x"}, "Only text files"),
                             ({"path": "a.md", "content": "x" * 500_001}, "too big"),
                             ({"path": "../escape.md", "content": "x"}, "outside"),
                             ({"path": ".hidden.md", "content": "x"}, "dot"),
                             ({"path": "", "content": "x"}, "file name"),
                             ({"path": "a.md", "content": 5}, "text")):
            with self.assertRaises(HTTPError) as cm:
                call("PUT", "/api/context/file", body)
            self.assertEqual(cm.exception.status, 400)
            self.assertIn(needle, cm.exception.message)
        with self.assertRaises(HTTPError) as cm:
            call("GET", "/api/context/file", query={"path": "missing.md"})
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(HTTPError):
            call("PUT", "/api/context/pinned", {"paths": "a.md"})

    def test_company_library_flow_and_search_route(self):
        cid = self.c["id"]
        call("PUT", f"/api/companies/{cid}/context/file", {"path": "calls/sam.md", "content": "# Call with Sam\nSam wants magenta tiles.\n"})
        (self.root / "context" / "scan.pdf").write_bytes(b"%PDF")
        listing = call("GET", f"/api/companies/{cid}/context")
        files = {f["path"]: f for f in listing["files"]}
        self.assertTrue(files["calls/sam.md"]["indexed"])
        self.assertIn("convert it to text", files["scan.pdf"]["note"])
        call("PUT", f"/api/companies/{cid}/context/pinned", {"paths": ["context:calls/sam.md"]})
        self.assertEqual(db.company_settings(db.get_company(cid))["pinned_context"], ["calls/sam.md"])
        self.assertIn("## context:calls/sam.md", context.pinned_section(db.get_company(cid)))
        ev = db.one("SELECT * FROM events WHERE company_id=? AND kind='file' ORDER BY id DESC", (cid,))
        self.assertEqual(json.loads(ev["data"]), {"path": "context/calls/sam.md", "by": "owner"})
        res = call("GET", f"/api/companies/{cid}/search", query={"q": "magenta", "scope": "context"})
        self.assertEqual(res["results"][0], {"source": "context", "ref": "context:calls/sam.md",
                                             "path": "context:calls/sam.md", "line": 2, "heading": "Call with Sam",
                                             "snippet": res["results"][0]["snippet"], "title": "context:calls/sam.md",
                                             "id": None})
        self.assertIn("[magenta]", res["results"][0]["snippet"])
        self.assertEqual(call("GET", f"/api/companies/{cid}/search", query={"q": ""})["results"], [])
        with self.assertRaises(HTTPError) as cm:
            call("GET", f"/api/companies/{cid}/search", query={"q": "x", "scope": "planets"})
        self.assertEqual(cm.exception.status, 400)
        call("DELETE", f"/api/companies/{cid}/context/file", {"path": "calls/sam.md"})
        self.assertFalse((self.root / "context" / "calls" / "sam.md").exists())
        self.assertEqual(db.company_settings(db.get_company(cid))["pinned_context"], [])
        self.assertFalse(call("GET", f"/api/companies/{cid}/search", query={"q": "magenta"})["results"])
        with self.assertRaises(HTTPError) as cm:
            call("GET", "/api/companies/99999999/context")
        self.assertEqual(cm.exception.status, 404)

    def test_index_status(self):
        self.write("docs/a.md", "# A\n")
        context.reindex(self.c)
        st = call("GET", "/api/index/status")
        self.assertTrue(st["fts"])
        self.assertGreater(st["rows"], 0)
        mine = next(x for x in st["companies"] if x["id"] == self.c["id"])
        self.assertEqual(mine["files_indexed"], 1)
        self.assertTrue(mine["last_indexed"])
        self.assertEqual(st["embedding_model"], "")


# ====================================================================== speed

class TestSpeed(Base):
    def test_no_change_reindex_of_300_files_is_fast(self):
        for i in range(300):
            folder = ("docs", "site", "code", "research", "marketing")[i % 5]
            ext = (".md", ".html", ".py", ".md", ".txt")[i % 5]
            self.write(f"{folder}/sub{i % 7}/file{i}{ext}", f"# File {i}\n\n" + f"Line about topic {i}.\n" * 40)
        t0 = time.perf_counter()
        context.reindex(self.c)
        full = time.perf_counter() - t0
        self.assertEqual(len(self.stamps()), 300)
        self.assertEqual(db.scalar("SELECT COUNT(DISTINCT ref) FROM search_index WHERE company_id=? AND source='file'",
                                   (self.c["id"],)), 300)
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            context.reindex(self.c)
            times.append(time.perf_counter() - t0)
        best = min(times)
        print(f"\n[context] 300 files: full index {full * 1000:.0f} ms, no-change reindex {best * 1000:.1f} ms")
        self.assertLess(best, 0.5)


# ====================================================================== meaning-based search

class TestHybridSearch(Base):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeModelServer(delay=0).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        super().setUp()
        self.saved = db.get_settings()
        db.set_settings({"llm_backend": "ollama", "llm_base_url": self.server.url, "embedding_model": "fake-embed"})
        context.EMBED_IN_BACKGROUND = False
        context._embed_down_until = 0.0
        self.write("docs/pricing.md", "# Plans\n\nOur monthly price is twelve dollars for each roofer.\n")
        self.write("docs/weather.md", "# Weather\n\nLeeds is rainy in winter.\n")

    def tearDown(self):
        db.set_settings({k: self.saved[k] for k in ("llm_backend", "llm_base_url", "embedding_model")})
        context.EMBED_IN_BACKGROUND = True
        context._embed_down_until = 0.0
        super().tearDown()

    def test_meaning_search_finds_what_keywords_miss(self):
        context.reindex(self.c)   # embeds the new chunks (synchronously in tests)
        self.assertTrue(any(c["path"] == "/api/embed" for c in self.server.calls))
        vectors = db.scalar("SELECT COUNT(*) FROM search_vectors v JOIN search_index s ON s.rowid=v.rowid "
                            "WHERE s.company_id=? AND v.model='fake-embed'", (self.c["id"],))
        self.assertGreaterEqual(vectors, 2)
        rows = context.search_rows(self.c, "fee")
        self.assertEqual(rows[0]["path"], "docs/pricing.md")
        self.assertIn("pricing.md", self.tool("search", query="fee"))
        db.set_settings({"embedding_model": ""})
        self.assertEqual(context.search_rows(self.c, "fee"), [])   # BM25 alone finds nothing
        self.assertEqual(context.index_status()["embedding_model"], "")

    def test_embedding_failure_falls_back_to_keywords(self):
        db.set_settings({"embedding_model": EMBED_FAIL_MODEL})
        with quiet():
            context.reindex(self.c)   # embedding fails quietly
        rows = context.search_rows(self.c, "Leeds")
        self.assertEqual(rows[0]["path"], "docs/weather.md")
        self.assertGreater(context._embed_down_until, time.time())
        db.set_settings({"llm_base_url": "http://127.0.0.1:9", "embedding_model": "fake-embed"})
        context._embed_down_until = 0.0
        self.assertEqual(context.search_rows(self.c, "rainy")[0]["path"], "docs/weather.md")


# ====================================================================== without FTS5

class TestLikeFallback(Base):
    def test_search_and_sections_work_without_fts(self):
        self.write("research/market.md", "# Market\n\nintro\nRoofers pay for jaguar leads.\n")
        g = context.global_dir()
        g.mkdir(parents=True, exist_ok=True)
        (g / "me.md").write_text("I like jaguar cars.\n", encoding="utf-8")
        db.run("INSERT INTO memory(company_id, agent_id, kind, content, pinned, created_at) VALUES(?,?,?,?,?,?)",
               (self.c["id"], self.agent_id(), "fact", "The jaguar segment pays most.", 0, db.now()))
        db.run("INSERT INTO tasks(company_id, title, role, status, created_by, created_at, updated_at) "
               "VALUES(?,?,?,?,?,?,?)", (self.c["id"], "Call jaguar owners", "sales", "todo", "t", db.now(), db.now()))
        saved = db.HAS_FTS
        db.HAS_FTS = False
        try:
            self.assertFalse(context._fts_ok())
            context.reindex(self.c)   # a no-op, never an error
            rows = context.search_rows(self.c, "jaguar", limit=10)
            got = {(r["source"], r["path"] or r["ref"]) for r in rows}
            self.assertIn(("file", "research/market.md"), got)
            self.assertIn(("context", "global:me.md"), got)
            self.assertTrue(any(r["source"] == "memory" for r in rows))
            self.assertTrue(any(r["source"] == "task" for r in rows))
            hit = next(r for r in rows if r["path"] == "research/market.md")
            self.assertEqual(hit["line"], 4)
            out = self.tool("search", query="jaguar leads")
            self.assertIn("research/market.md:4", out)
            self.assertIn("[jaguar]", out)
            self.assertTrue(self.tool("write_file", path="docs/x.md", content="jaguar").startswith("Saved"))
            self.assertIn("docs/x.md (6 bytes)", self.tool("list_files"))
            sec = context.memory_section(self.c, None, {"title": "jaguar pricing", "description": ""})
            self.assertIn("jaguar segment", sec)
        finally:
            db.HAS_FTS = saved


# ====================================================================== keeping sessions small

class TestTrim(unittest.TestCase):
    def convo(self):
        msgs = [{"role": "system", "content": "S" * 1000}, {"role": "user", "content": "U" * 500}]
        for i in range(4):
            cid = f"c{i}"
            msgs.append({"role": "assistant", "content": "", "thinking": "T" * 2000,
                         "tool_calls": [{"id": cid, "name": "read_file", "arguments": {"path": f"docs/{i}.md"}}]})
            msgs.append({"role": "tool", "name": "read_file", "tool_call_id": cid,
                         "content": f"docs/{i}.md: lines 1-200 of 900\n" + "x" * 3000})
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "s1", "name": "search", "arguments": {"query": "roof prices", "scope": "files"}}]})
        msgs.append({"role": "tool", "name": "search", "tool_call_id": "s1", "content": "r" * 3000})
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "w1", "name": "write_file", "arguments": {"path": "site/a.html", "content": "W" * 9000}}]})
        msgs.append({"role": "tool", "name": "write_file", "tool_call_id": "w1", "content": "Saved site/a.html"})
        msgs += [{"role": "assistant", "content": "last-a"}, {"role": "user", "content": "last-b"},
                 {"role": "assistant", "content": "last-c", "thinking": "keep me"},
                 {"role": "tool", "name": "read_file", "tool_call_id": "z", "content": "L" * 3000}]
        return msgs

    def test_thinking_goes_first(self):
        msgs = self.convo()
        total = sum(len(m.get("content") or "") + len(m.get("thinking") or "") for m in msgs)
        agents._trim(msgs, total + 9100 - 8000)   # dropping the old thinking (8,000) is just not enough...
        self.assertTrue(all(not m.get("thinking") for m in msgs[2:-3]))
        self.assertEqual(msgs[-2]["thinking"], "keep me")

    def test_old_results_keep_their_reference(self):
        msgs = self.convo()
        original = [dict(m) for m in msgs]
        agents._trim(msgs, 15000)
        self.assertEqual(msgs[0], original[0])
        self.assertEqual(msgs[1], original[1])
        self.assertEqual(msgs[-4:], original[-4:])   # the newest messages stay whole
        self.assertEqual(msgs[3]["content"], "[read_file docs/0.md lines 1-200, shortened to save room; run it "
                                             "again if you need it: read_file docs/0.md start_line=1 end_line=200]")
        joined = "\n".join(m["content"] or "" for m in msgs)
        self.assertIn('[search "roof prices" scope=files, shortened to save room; run it again if you need it]', joined)
        self.assertNotIn("older result shortened", joined)
        total = sum(len(m.get("content") or "") + len(m.get("thinking") or "")
                    + sum(len(json.dumps(tc["arguments"])) for tc in m.get("tool_calls") or []) for m in msgs)
        self.assertLessEqual(total, 15000)

    def test_big_write_arguments_are_shortened_last(self):
        msgs = self.convo()
        call = msgs[12]["tool_calls"][0]
        before = call["arguments"]
        agents._trim(msgs, 9000)
        self.assertEqual(msgs[12]["tool_calls"][0]["arguments"]["content"], "[9000 characters, shortened to save room]")
        self.assertEqual(msgs[12]["tool_calls"][0]["arguments"]["path"], "site/a.html")
        self.assertEqual(len(before["content"]), 9000)   # the original arguments object is not changed

    def test_repeated_call_ids_pair_with_their_own_call(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        for path in ("docs/a.md", "docs/b.md"):   # some servers number calls per reply, so ids repeat
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_0", "name": "read_file", "arguments": {"path": path}}]})
            msgs.append({"role": "tool", "name": "read_file", "tool_call_id": "call_0",
                         "content": f"{path}: lines 1-200 of 300\n" + "x" * 4000})
        msgs += [{"role": "user", "content": str(i)} for i in range(4)]
        agents._trim(msgs, 1000)
        self.assertIn("read_file docs/a.md lines 1-200", msgs[3]["content"])
        self.assertIn("read_file docs/b.md lines 1-200", msgs[5]["content"])

    def test_reference_lines(self):
        ref = context.trim_reference
        self.assertEqual(ref("grep", {"pattern": "TODO", "path": "site"}, "x"),
                         '[grep "TODO" in site, shortened to save room; run it again if you need it]')
        self.assertEqual(ref("fetch_url", {"url": "https://a.b/c"}, "x"),
                         "[fetch_url https://a.b/c, shortened to save room; run it again if you need it]")
        self.assertIn("read_file docs/a.md outline", ref("read_file", {"path": "docs/a.md", "outline": True}, "x"))
        self.assertNotIn("start_line=",
                         ref("read_file", {"path": "a", "start_line": 1, "end_line": 9}, "a: lines 1-9 of 20\n"))


# ====================================================================== smaller pieces

class TestToolsWiring(Base):
    def test_registry_aliases_and_write_reminder(self):
        for name in ("search", "grep", "edit_file"):
            self.assertIn(name, tools.REGISTRY)
            self.assertIn(name, self.ctx.allowed)
        self.write("docs/a.md", "hello there\n")
        self.assertIn("1: hello there", self.tool("cat", path="docs/a.md"))
        self.assertIn("1: hello there", self.tool("view_file", file_path="docs/a.md"))
        big = self.tool("write_file", path="docs/big.md", content="x" * 8001)
        self.assertTrue(big.startswith("Saved docs/big.md (8001 characters). For big files, write a first section, "
                                       "then add the rest with append=true or change parts with edit_file."))
        self.assertEqual(self.tool("write_file", path="docs/small.md", content="x" * 8000),
                         "Saved docs/small.md (8000 characters).")

    def test_search_keeps_its_old_meaning_for_agents_without_the_tool(self):
        # Every belt carries search since Phase 0.5, so simulate an agent that has web_search but not search.
        older = ctx_for(self.c, "ceo", kind="founding")
        older.allowed = (older.allowed - {"search"}) | {"web_search"}
        self.assertEqual(tools.resolve(older, "search", {"q": "roofers", "topn": 2}),
                         ("web_search", {"query": "roofers", "max_results": 2}))
        founding = ctx_for(self.c, "ceo", kind="founding")   # staged founding stages carry the real search tool
        self.assertIn("search", founding.allowed)
        self.assertEqual(tools.resolve(founding, "search", {"q": "roofers"}), ("search", {"query": "roofers"}))
        self.assertEqual(tools.resolve(self.ctx, "search", {"q": "roofers"}), ("search", {"query": "roofers"}))
        self.assertEqual(tools.resolve(self.ctx, "find", {"text": "x"}), ("search", {"query": "x"}))


if __name__ == "__main__":
    unittest.main()
