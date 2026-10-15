"""Context system (Phase 0.5): the index, unified search, line-based reading, small edits, the owner's context
library, and the prompt sections that give every agent the company's context without dumping it all.

Design: docs/phases/phase-0.5-ground-truth-and-context.md, section 1.

How the pieces fit:
- One SQLite FTS5 table (search_index, created by db.py) holds chunks of company files, the owner's context
  library, memory, tasks, emails, chat, approvals, past sessions and facts. index_state remembers what was indexed:
  a stamp per file or row plus the FTS rowids of its chunks, so a change replaces exactly those rows.
- reindex() brings a company up to date at the start of every run and after_write() re-indexes a file an agent
  just wrote. Both are incremental and never raise: the index helps agents, a run must never depend on it.
- search() ranks with BM25 and, when the owner set an embedding model, merges meaning-based ranks too. Without
  FTS5 it falls back to plain LIKE scans so the tool still answers.
- The owner's context library lives in data/context/ (every company sees it, refs "global:<path>") and in each
  company's context/ folder (refs "context:<path>"). Agents can read and search it but never write it.
"""
from __future__ import annotations

import html as _html
import inspect
import json
import math
import operator
import os
import re
import shutil
import sqlite3
import threading
import time
import traceback
import zlib
from html.parser import HTMLParser
from pathlib import Path

from . import bus, config, db, llm, workspace
from . import tools as _tools
from .routing import HTTPError, company_or_404, route, to_int
from .tools import Ctx, ToolError, S, I, tool

# ---------------------------------------------------------------- limits and names

MAX_FILE_BYTES = 2_000_000      # the limit read_file always had; bigger files are not indexed either
LIB_MAX_BYTES = 500_000         # one owner context file saved from the dashboard
LIB_FORMATS = (".md", ".txt", ".csv", ".json", ".html", ".xml", ".yaml", ".yml")
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff", ".psd", ".avif", ".heic",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".mp3", ".mp4", ".wav", ".ogg", ".m4a",
    ".mov", ".avi", ".webm", ".mkv", ".flac", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".exe", ".dll", ".so",
    ".dylib", ".bin", ".pyc", ".pyo", ".class", ".jar", ".o", ".a", ".db", ".sqlite", ".sqlite3", ".docx",
    ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".key", ".numbers", ".pages", ".wasm",
}
# Holdco's own folders, tool caches and installed packages are not company knowledge.
INDEX_SKIP = set(workspace.SKIP) | {".cache", "venv", "site-packages", "bower_components"}

WINDOW = 60                     # lines per chunk for code and plain text
CSV_ROWS = 50
MAX_CHUNKS = 200                # per file, so one giant file cannot crowd out everything else
MAX_CHUNK_CHARS = 8000
ROW_BODY_CHARS = 12000          # tasks and emails can grow long; keep the start and the newest part

READ_LINES = 200                # read_file's default range
READ_MAX_LINES = 500
# tools.execute cuts every tool result at 7,000 characters. read_file stays under that, so the pointer to the
# next range is never the part that gets cut off.
READ_CHARS = 6000
LONG_LINE = 1500

CORE_CAP, MEMORY_CAP, PINNED_CAP, INDEX_CAP = 1500, 1800, 2500, 1500
KEY_DOCS = ("docs/charter.md", "research/market.md", "docs/critique.md", "records/STATUS.md", "docs/decisions.md")

ALL_SOURCES = ("file", "context", "memory", "task", "email", "chat", "approval", "run", "fact")
SCOPES = {"all": ALL_SOURCES, "files": ("file",), "context": ("context",), "memory": ("memory",),
          "tasks": ("task",), "emails": ("email",), "chat": ("chat",), "decisions": ("approval",),
          "sessions": ("run",), "facts": ("fact",)}
_SCOPE_ALIASES = {"file": "files", "everything": "all", "any": "all", "": "all", "owner": "context",
                  "global": "context", "library": "context", "memories": "memory", "notes": "memory",
                  "task": "tasks", "email": "emails", "inbox": "emails", "mail": "emails", "messages": "chat",
                  "message": "chat", "chats": "chat", "decision": "decisions", "approvals": "decisions",
                  "approval": "decisions", "session": "sessions", "runs": "sessions", "run": "sessions",
                  "fact": "facts"}

OWNER_ONLY = "context/ is the owner's. Save your notes elsewhere, for example docs/."

_STOP = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "how", "i", "in", "is",
         "it", "its", "of", "on", "or", "our", "that", "the", "this", "to", "was", "we", "what", "when", "where",
         "which", "who", "will", "with", "you", "your", "do", "does", "did", "can", "about", "into", "any", "all",
         "there", "their", "they", "them", "not", "if", "then", "than", "so", "but", "me", "my", "us"}

# Arguments models often send under other names (tools.resolve renames them before the call).
_tools.ARG_RENAMES["edit_file"] = {
    "file": "path", "filename": "path", "file_path": "path", "filepath": "path",
    "old": "find", "old_str": "find", "old_string": "find", "old_text": "find", "search": "find",
    "target": "find", "original": "find", "pattern": "find",
    "new": "replace", "new_str": "replace", "new_string": "replace", "new_text": "replace",
    "replacement": "replace", "with": "replace", "content": "replace"}
_tools.ARG_RENAMES["grep"] = {"query": "pattern", "text": "pattern", "search": "pattern", "regexp": "pattern",
                              "string": "pattern", "term": "pattern", "dir": "path", "directory": "path",
                              "folder": "path", "file": "path", "is_regex": "regex", "use_regex": "regex"}
_tools.ARG_RENAMES["search"] = {"q": "query", "text": "query", "term": "query", "terms": "query",
                                "keywords": "query", "question": "query", "max_results": "limit", "topn": "limit",
                                "top_k": "limit", "k": "limit", "n": "limit", "source": "scope", "type": "scope",
                                "where": "scope"}
_tools.ARG_RENAMES.setdefault("read_file", {}).update({
    "start": "start_line", "line": "start_line", "from_line": "start_line", "line_start": "start_line",
    "end": "end_line", "to_line": "end_line", "line_end": "end_line", "filepath": "path"})
_tools.ARG_RENAMES.setdefault("list_files", {}).update({"folder": "path", "dir": "path", "directory": "path"})

_resolve_names = _tools.resolve


def _resolve_with_old_meaning(ctx: Ctx, name: str, args: dict) -> tuple[str, dict]:
    """'search' was an alias for web_search before it became a tool of its own (gpt-oss calls its built-in
    browser search that way). Founding runs do not carry the new search tool, so for an agent without it the
    old meaning still applies instead of an error. Any other call resolves exactly as before."""
    if name in _tools.REGISTRY and ctx.allowed and name not in ctx.allowed:
        old = _tools.ALIASES.get(name)
        if old and old in ctx.allowed:
            name = old
    return _resolve_names(ctx, name, args)


_tools.resolve = _resolve_with_old_meaning


# ---------------------------------------------------------------- paths

def global_dir() -> Path:
    """The owner's context library that every company sees."""
    return config.DATA_DIR / "context"


def company_dir(company: dict) -> Path:
    """One company's context library."""
    return workspace.root(company) / "context"


def _inside(base: Path, rel: str, what: str = "the context folder") -> Path:
    """A path inside `base`; anything that would land outside it is refused (the rules of workspace.safe_path)."""
    rel = (rel or "").strip().replace("\\", "/")
    rel = re.sub(r"^(\./)+", "", rel).lstrip("/")
    if re.match(r"^[a-zA-Z]:", rel):
        raise workspace.PathError(f"Use a path inside {what}, for example notes.md")
    b = base.resolve()
    p = (b / rel).resolve()
    if p != b and b not in p.parents:
        raise workspace.PathError(f"That path is outside {what}.")
    return p


def _rel_to(base: Path, p: Path) -> str:
    try:
        r = p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return p.name
    return "" if r == "." else r


def resolve(company: dict, path: str) -> tuple[Path, str, str]:
    """(absolute path, the name to show the agent, kind) for a company path or a library ref. kind is "file",
    "context" (the company's context/ folder) or "global" (data/context/)."""
    raw = (path or "").strip()
    low = raw.lower()
    if low.startswith("global:"):
        p = _inside(global_dir(), raw[7:], "the owner's context folder")
        return p, "global:" + _rel_to(global_dir(), p), "global"
    if low.startswith("context:"):
        p = _inside(company_dir(company), raw[8:], "the company's context folder")
        return p, "context:" + _rel_to(company_dir(company), p), "context"
    p = workspace.safe_path(company, raw or ".")
    return p, workspace.rel(company, p), "file"


# ---------------------------------------------------------------- text helpers

def _lines(text: str) -> list[str]:
    """Lines as editors count them: a trailing newline does not start another line and \\r\\n is one break. The
    index, read_file, grep and edit_file all count this way, so a path:line from one works in the others."""
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [s[:-1] if s.endswith("\r") else s for s in parts]


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _binary_name(name: str) -> bool:
    return _ext(name) in BINARY_EXT


def _load_text(p: Path, size: int | None = None, keep_bom: bool = False) -> str | None:
    """A file's text, or None when it is binary, too large or unreadable."""
    try:
        if size is None:
            size = p.stat().st_size
        if size > MAX_FILE_BYTES:
            return None
        raw = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:2000]:
        return None
    text = raw.decode("utf-8", "replace")
    return text if keep_bom else text.lstrip("\ufeff")


def _head_text(p: Path, n: int = 16384) -> str | None:
    """The start of a text file (enough for a summary) without reading all of it."""
    try:
        with p.open("rb") as f:
            raw = f.read(n)
    except OSError:
        return None
    if b"\x00" in raw[:2000]:
        return None
    return raw.decode("utf-8", "replace").lstrip("\ufeff")


def _short(s: str, n: int = 80) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:max(1, n - 3)].rstrip() + "..."


def _clip(text: str, n: int) -> str:
    """Keep the start and the newest part of a long text (results and notes are appended at the end)."""
    if len(text) <= n:
        return text
    head = n // 3
    return text[:head] + "\n...\n" + text[-(n - head - 5):]


_MD_HEAD = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_PY_DEF = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+\w+")
_JS_DEF = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s*\*?\s*\w+|class\s+\w+|"
                     r"(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|\w+\s*=>))")
_HTML_HEAD = re.compile(r"<h([1-3])\b[^>]*>(.*?)</h\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_JS_EXT = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")


def outline_entries(text: str, ext: str, lines: list[str] | None = None) -> list[tuple[int, str]]:
    """(line number, heading) for Markdown headings, HTML h1-h3, Python def/class and JavaScript functions."""
    lines = lines if lines is not None else _lines(text)
    out: list[tuple[int, str]] = []
    if ext in (".md", ".markdown"):
        fence = False
        for n, line in enumerate(lines, 1):
            if line.lstrip().startswith(("```", "~~~")):
                fence = not fence
                continue
            if not fence and _MD_HEAD.match(line):
                out.append((n, line.strip()[:160]))
    elif ext in (".html", ".htm"):
        for m in _HTML_HEAD.finditer(text):
            title = _short(_html.unescape(_TAG.sub(" ", m.group(2))), 120)
            if title:
                out.append((text.count("\n", 0, m.start()) + 1, f"h{m.group(1)}: {title}"))
    elif ext == ".py":
        out = [(n, line.rstrip()[:160]) for n, line in enumerate(lines, 1) if _PY_DEF.match(line)]
    elif ext in _JS_EXT:
        out = [(n, line.rstrip()[:160]) for n, line in enumerate(lines, 1) if _JS_DEF.match(line)]
    return out


def summarize(name: str, text: str) -> str:
    """One line of at most 80 characters saying what a file is: its title or first heading, else its first
    sentence. list_files and the context index show it so agents can pick a file without opening it."""
    ext = _ext(name)
    head = (text or "")[:16384]
    if ext in (".html", ".htm"):
        for rx in (r"<title[^>]*>(.*?)</title>", r"<h1[^>]*>(.*?)</h1>"):
            m = re.search(rx, head, re.I | re.S)
            if m and _TAG.sub(" ", m.group(1)).strip():
                return _short(_html.unescape(_TAG.sub(" ", m.group(1))))
        head = _html.unescape(_TAG.sub("\n", re.sub(r"(?is)<(script|style|head)\b.*?</\1\s*>", " ", head)))
    lines = _lines(head)
    if ext in (".csv", ".tsv"):
        return _short("Table with columns: " + lines[0]) if lines else ""
    if ext == ".json" and len(text) < 200_000:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return _short("JSON with keys: " + ", ".join(str(k) for k in list(data)[:12]))
            if isinstance(data, list):
                return _short(f"JSON list of {len(data)} items")
        except ValueError:
            pass
    if ext in (".md", ".markdown", ".txt", ""):
        fence = False
        for line in lines[:300]:
            if line.lstrip().startswith(("```", "~~~")):
                fence = not fence
            elif not fence and (m := _MD_HEAD.match(line)):
                return _short(m.group(2).strip("*_ "))
    for line in lines[:300]:
        s = re.sub(r"^(?:#+|//+|/\*+|\*+|--+|>+|[-*+]\s|\d+[.)]\s)\s*", "", line.strip()).strip()
        s = s.strip("\"'`*_ ").strip()
        if (not s or s.startswith(("<", "{", "}", "[", "]", "(", ")", "import ", "from ", "#!", "@", "---", "```",
                                   "use ", "package ", "require(")) or re.fullmatch(r"[\W_]+", s)):
            continue
        return _short(re.split(r"(?<=[.!?])\s", s, maxsplit=1)[0])
    return ""


# ---------------------------------------------------------------- chunking

def chunk_file(name: str, text: str) -> list[tuple[int, str, str]]:
    """Split a file into (first line, heading, body) chunks: Markdown at its headings, HTML as text with its
    headings kept, CSV as the header and the first 50 rows, everything else in 60-line windows."""
    ext = _ext(name)
    if ext in (".html", ".htm"):
        chunks = _chunk_html(text)
    else:
        lines = _lines(text)
        if ext in (".md", ".markdown"):
            chunks = _chunk_markdown(lines)
        elif ext in (".csv", ".tsv"):
            chunks = _chunk_csv(lines)
        else:
            chunks = _chunk_windows(lines, outline_entries(text, ext, lines))
    return [(line, _short(heading, 160), body[:MAX_CHUNK_CHARS])
            for line, heading, body in chunks[:MAX_CHUNKS] if body.strip()]


def _chunk_markdown(lines: list[str]) -> list[tuple[int, str, str]]:
    sections: list[tuple[int, int, str]] = []
    start, heading, fence = 0, "", False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        m = None if fence else _MD_HEAD.match(line)
        if m:
            if i > start:
                sections.append((start, i, heading))
            start, heading = i, m.group(2).strip()
    sections.append((start, len(lines), heading))
    out = []
    for s, e, h in sections:
        for w in range(s, e, WINDOW):   # long sections become several windows that keep the heading
            out.append((w + 1, h, "\n".join(lines[w:min(e, w + WINDOW)])))
    return out


def _chunk_windows(lines: list[str], entries: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    out = []
    for s in range(0, len(lines), WINDOW):
        e = min(len(lines), s + WINDOW)
        inside = [t for n, t in entries if s < n <= e]
        before = [t for n, t in entries if n <= s]
        heading = (inside[0] if inside else before[-1] if before else "").strip()
        out.append((s + 1, heading, "\n".join(lines[s:e])))
    return out


def _chunk_csv(lines: list[str]) -> list[tuple[int, str, str]]:
    if not lines:
        return []
    body = "\n".join(lines[:CSV_ROWS + 1])
    more = len(lines) - 1 - CSV_ROWS
    if more > 0:
        body += f"\n({more} more rows)"
    return [(1, "columns: " + lines[0], body)]


class _HtmlChunker(HTMLParser):
    """Page text split at h1-h3, each part remembering the source line of its heading so search results point
    at the right place in the raw file."""
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    HEADS = {"h1", "h2", "h3"}
    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "header", "footer", "ul", "ol", "table", "td",
             "th", "blockquote", "pre", "dd", "dt", "h4", "h5", "h6", "form", "label", "button", "nav", "main"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.sections: list[list] = [[1, "", []]]
        self._skip = 0
        self._in_title = False
        self._head: list | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag == "body":
            self._skip = 0   # a missing </head> must not hide the whole page
        if tag in self.SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in self.HEADS:
            self._head = [self.getpos()[0], []]
        elif tag in self.BLOCK:
            self.sections[-1][2].append("\n")
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.sections[-1][2].append(f" {alt} ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag in self.HEADS and self._head is not None:
            line, parts = self._head
            self._head = None
            text = " ".join("".join(parts).split())
            if text:
                self.sections.append([line, text, [text, "\n"]])
            return
        if tag in self.BLOCK:
            self.sections[-1][2].append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        if self._head is not None:
            self._head[1].append(data)
        else:
            self.sections[-1][2].append(data)


def _chunk_html(text: str) -> list[tuple[int, str, str]]:
    p = _HtmlChunker()
    try:
        p.feed(text)
        p.close()
    except Exception:   # malformed HTML: keep what was parsed
        pass
    title = " ".join(p.title.split())
    if title:
        p.sections[0][2].insert(0, title + "\n")
        if not p.sections[0][1]:
            p.sections[0][1] = title
    out = []
    for line, heading, parts in p.sections:
        rows = [r for r in (" ".join(x.split()) for x in "".join(parts).splitlines()) if r]
        for s in range(0, len(rows), WINDOW):
            out.append((line, heading, "\n".join(rows[s:s + WINDOW])))
    return out


# ---------------------------------------------------------------- index plumbing

_index_lock = threading.RLock()
_last_checked: dict[int, float] = {}


def _fts_ok() -> bool:
    """Is the FTS5 index usable? False when SQLite was built without FTS5 or the table is missing."""
    if not db.HAS_FTS:
        return False
    try:
        return bool(db.scalar("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_index'"))
    except Exception:
        return False


def _warn(what: str) -> None:
    """The index is a convenience: a failure is printed for the log and the caller carries on without it."""
    print(f"[context] {what} failed:")
    traceback.print_exc()


def _safe(fn, *args) -> int:
    try:
        return fn(*args) or 0
    except Exception:
        _warn(f"indexing ({fn.__name__})")
        return 0


class _Tx:
    """One transaction on db's shared connection. db.run commits after every statement, which costs a disk sync
    each time; indexing writes hundreds of rows, so they go in one transaction under db's lock instead."""

    def __enter__(self):
        db._lock.acquire()
        return self

    def x(self, sql: str, params=()):
        return db._conn.execute(sql, params)

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                db._conn.commit()
            else:
                db._conn.rollback()
        finally:
            db._lock.release()
        return False


def _stamp(sig: str, rowids: list[int], summary: str = "") -> str:
    """index_state.stamp: "<signature>\\t<rowid,rowid,...>\\t<one-line summary>"."""
    clean = " ".join((summary or "").split())
    return f"{sig}\t{','.join(str(r) for r in rowids)}\t{clean}"


def _parse(stamp: str | None) -> tuple[str, list[int], str]:
    parts = (stamp or "").split("\t", 2) + ["", ""]
    return parts[0], [int(x) for x in parts[1].split(",") if x.strip().isdigit()], parts[2]


def _sig_of(stamp: str | None) -> str | None:
    return None if stamp is None else stamp.split("\t", 1)[0]


def _states(cid: int, source: str) -> dict[str, str]:
    return {r["ref"]: r["stamp"] or "" for r in
            db.q("SELECT ref, stamp FROM index_state WHERE company_id=? AND source=?", (cid, source))}


def _state(cid: int, source: str, ref: str) -> str | None:
    return db.scalar("SELECT stamp FROM index_state WHERE company_id=? AND source=? AND ref=?", (cid, source, ref))


_INSERT = ("INSERT INTO search_index(title, heading, body, company_id, source, ref, line, updated_at) "
           "VALUES(?,?,?,?,?,?,?,?)")
_UPSERT = ("INSERT INTO index_state(company_id, source, ref, stamp) VALUES(?,?,?,?) "
           "ON CONFLICT(company_id, source, ref) DO UPDATE SET stamp=excluded.stamp")


def _delete_rows(tx: _Tx, rowids: list[int]) -> None:
    for i in range(0, len(rowids), 400):
        part = rowids[i:i + 400]
        qm = ",".join("?" * len(part))
        tx.x(f"DELETE FROM search_index WHERE rowid IN ({qm})", part)
        tx.x(f"DELETE FROM search_vectors WHERE rowid IN ({qm})", part)


def _put(tx: _Tx, cid: int, source: str, ref: str, sig: str, chunks: list, summary: str,
         old_rowids: list[int]) -> None:
    """Replace one file's or row's chunks. chunks: (title, heading, body, line)."""
    _delete_rows(tx, old_rowids)
    ts = db.now()
    new = [tx.x(_INSERT, (t, h, b, cid, source, ref, line, ts)).lastrowid for t, h, b, line in chunks]
    tx.x(_UPSERT, (cid, source, ref, _stamp(sig, new, summary)))


def _drop(tx: _Tx, cid: int, source: str, ref: str, old_rowids: list[int]) -> None:
    _delete_rows(tx, old_rowids)
    tx.x("DELETE FROM index_state WHERE company_id=? AND source=? AND ref=?", (cid, source, ref))


def _set_meta(cid: int, ref: str, value: str) -> None:
    with _Tx() as tx:
        tx.x(_UPSERT, (cid, "meta", ref, value))


def _walk(base: Path, skip_top: tuple = ()):
    """(relative posix path, absolute path, size, mtime_ns) for each regular file under base. Skips Holdco's own
    folders (.runs, .trash, .cache), node_modules, .git, other dot-files, and symlinks (they could point outside
    the company folder)."""
    if not base.is_dir():
        return
    stack = [(str(base), "")]
    while stack:
        d, prefix = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                name = e.name
                if name in INDEX_SKIP or name.startswith("."):
                    continue
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir():
                        if not prefix and name in skip_top:
                            continue
                        stack.append((e.path, prefix + name + "/"))
                    elif e.is_file():
                        st = e.stat()
                        yield prefix + name, e.path, st.st_size, st.st_mtime_ns
                except OSError:
                    continue


# ---------------------------------------------------------------- indexing files

def _file_chunks(ref: str, rel: str, ap: str, size: int, library: bool) -> tuple[list, str]:
    """(chunks, summary) for one file; nothing for binaries, big files and, in the library, non-text formats."""
    if _binary_name(rel) or (library and _ext(rel) not in LIB_FORMATS):
        return [], ""
    text = _load_text(Path(ap), size)
    if text is None:
        return [], ""
    return [(ref, h, b, line) for line, h, b in chunk_file(rel, text)], summarize(rel, text)


def _sync_files(cid: int, source: str, base: Path, prefix: str, skip_top: tuple = (), library: bool = False) -> int:
    """Re-index the files under base whose size or modification time changed, and forget deleted ones."""
    states = _states(cid, source)
    todo, seen = [], set()
    for rel, ap, size, mtime in _walk(base, skip_top):
        ref = prefix + rel
        seen.add(ref)
        sig = f"{size}:{mtime}"
        old = states.get(ref)
        if _sig_of(old) == sig:
            continue
        todo.append((ref, rel, ap, size, sig, _parse(old)[1]))
    gone = [(ref, _parse(st)[1]) for ref, st in states.items() if ref not in seen]
    if gone:
        with _Tx() as tx:
            for ref, ids in gone:
                _drop(tx, cid, source, ref, ids)
    for i in range(0, len(todo), 40):   # read and chunk outside db's lock, write in short transactions
        batch = [(ref, sig, *_file_chunks(ref, rel, ap, size, library), old)
                 for ref, rel, ap, size, sig, old in todo[i:i + 40]]
        with _Tx() as tx:
            for ref, sig, chunks, summary, old in batch:
                _put(tx, cid, source, ref, sig, chunks, summary, old)
    return len(todo) + len(gone)


def _index_one(cid: int, source: str, ref: str, p: Path, library: bool) -> None:
    old = _state(cid, source, ref)
    try:
        st = p.stat() if p.is_file() else None
    except OSError:
        st = None
    if st is None:
        if old is not None:
            with _Tx() as tx:
                _drop(tx, cid, source, ref, _parse(old)[1])
        return
    sig = f"{st.st_size}:{st.st_mtime_ns}"
    if _sig_of(old) == sig:
        return
    rel = ref.split(":", 1)[1] if source == "context" else ref
    chunks, summary = _file_chunks(ref, rel, str(p), st.st_size, library)
    with _Tx() as tx:
        _put(tx, cid, source, ref, sig, chunks, summary, _parse(old)[1])


# ---------------------------------------------------------------- indexing database rows

def _names(cid: int) -> dict[int, str]:
    return {a["id"]: a["name"] for a in db.q("SELECT id, name FROM agents WHERE company_id=?", (cid,))}


def _flatten(v, key: str = "") -> list[str]:
    if isinstance(v, dict):
        return [x for k, val in v.items() for x in _flatten(val, str(k))]
    if isinstance(v, list):
        return [x for val in v for x in _flatten(val, key)]
    if v is None or v == "":
        return []
    return [f"{key}: {v}" if key else str(v)]


def _memory_doc(m: dict, names=None) -> list:
    kind = (m.get("kind") or "note") + (" (pinned)" if m.get("pinned") else "")
    return [(kind, (m.get("created_at") or "")[:10], m.get("content") or "", None)]


def _task_doc(t: dict, names=None) -> list:
    parts = [t.get("description") or ""]
    if t.get("deliverable"):
        parts.append("Deliverable: " + t["deliverable"])
    if t.get("acceptance"):
        parts.append("Acceptance criteria:\n" + t["acceptance"])
    if t.get("result"):
        parts.append("Handoff and results:\n" + t["result"])
    if t.get("notes"):
        parts.append("Working notes:\n" + t["notes"])
    body = _clip("\n\n".join(p for p in parts if p and p.strip()), ROW_BODY_CHARS) or (t.get("title") or "")
    return [(t.get("title") or "", f"[{t.get('status') or ''}] ({t.get('role') or ''})", body, None)]


def _approval_doc(a: dict, names=None) -> list:
    payload = db.loads(a.get("payload"), {}) or {}
    parts = ["\n".join(_flatten(payload))]
    if a.get("result"):
        parts.append("Result: " + str(a["result"]))
    if a.get("feedback"):
        parts.append("Owner's note: " + str(a["feedback"]))
    body = _clip("\n".join(p for p in parts if p), ROW_BODY_CHARS) or (a.get("summary") or "")
    return [(a.get("summary") or a.get("action") or "", f"{a.get('action') or ''} [{a.get('status') or ''}]",
             body, None)]


def _email_doc(e: dict, names=None) -> list:
    who = f"from {e.get('from_addr') or '?'}" if e.get("direction") == "in" else f"to {e.get('to_addr') or '?'}"
    body = _clip(f"From: {e.get('from_addr') or ''}\nTo: {e.get('to_addr') or ''}\n\n{e.get('body') or ''}",
                 ROW_BODY_CHARS)
    return [(e.get("subject") or "(no subject)", who, body, None)]


def _chat_doc(m: dict, names=None) -> list:
    who = "Owner" if m.get("sender") == "owner" else ((names or {}).get(m.get("agent_id")) or "Team")
    return [(who, (m.get("created_at") or "")[:16].replace("T", " "), m.get("content") or "", None)]


def _run_doc(r: dict, names=None) -> list:
    who = (names or {}).get(r.get("agent_id")) or "agent"
    head = f"[{r.get('status') or ''}]" + (f" task #{r['task_id']}" if r.get("task_id") else "")
    return [(f"{r.get('kind') or ''} session by {who}", head, r.get("summary") or "", None)]


def _fact_doc(f: dict, names=None) -> list:
    body = f"{f.get('claim') or ''}\nQuote: {f.get('quote') or ''}\nSource: {f.get('url') or ''}"
    return [(f.get("claim") or "", f.get("url") or "", body, None)]


def _write_rows(cid: int, source: str, changed: list, gone: list) -> int:
    for i in range(0, max(len(changed), len(gone)), 200):
        with _Tx() as tx:
            for ref, ids in gone[i:i + 200]:
                _drop(tx, cid, source, ref, ids)
            for ref, sig, chunks, old in changed[i:i + 200]:
                _put(tx, cid, source, ref, sig, chunks, "", old)
    return len(changed) + len(gone)


def _sync_memory(cid: int) -> int:
    """Memory can be edited, pinned and deleted from the dashboard, so each note keeps its own stamp."""
    rows = db.q("SELECT id, kind, pinned, content, created_at FROM memory WHERE company_id=?", (cid,))
    states = _states(cid, "memory")
    changed, seen = [], set()
    for m in rows:
        ref = str(m["id"])
        seen.add(ref)
        sig = f"{m['pinned']}|{m['kind']}|{zlib.crc32((m['content'] or '').encode('utf-8', 'replace'))}"
        old = states.get(ref)
        if _sig_of(old) != sig:
            changed.append((ref, sig, _memory_doc(m), _parse(old)[1]))
    gone = [(ref, _parse(st)[1]) for ref, st in states.items() if ref not in seen]
    return _write_rows(cid, "memory", changed, gone) if changed or gone else 0


# Rows that change after they are written: a cheap signature per row tells which ones to re-index. Lengths catch
# notes appended by the watchdog, which does not touch updated_at.
_TASK_SIG = ("SELECT id, COALESCE(updated_at,'') || '|' || COALESCE(status,'') || '|' || COALESCE(role,'') || '|' || "
             "length(COALESCE(title,'')) || '|' || length(COALESCE(description,'')) || '|' || "
             "length(COALESCE(acceptance,'')) || '|' || length(COALESCE(result,'')) || '|' || "
             "length(COALESCE(notes,'')) || '|' || length(COALESCE(deliverable,'')) AS sig "
             "FROM tasks WHERE company_id=?")
_APPROVAL_SIG = ("SELECT id, COALESCE(status,'') || '|' || COALESCE(decided_at,'') || '|' || "
                 "length(COALESCE(summary,'')) || '|' || length(COALESCE(payload,'')) || '|' || "
                 "length(COALESCE(result,'')) || '|' || length(COALESCE(feedback,'')) AS sig "
                 "FROM approvals WHERE company_id=?")


def _sync_rows(cid: int, source: str, sig_sql: str, table: str, builder) -> int:
    sigs = {str(r["id"]): r["sig"] or "" for r in db.q(sig_sql, (cid,))}
    states = _states(cid, source)
    ids = [int(ref) for ref, sig in sigs.items() if _sig_of(states.get(ref)) != sig]
    gone = [(ref, _parse(st)[1]) for ref, st in states.items() if ref not in sigs]
    if not ids and not gone:
        return 0
    changed = []
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        for r in db.q(f"SELECT * FROM {table} WHERE id IN ({','.join('?' * len(part))})", part):
            ref = str(r["id"])
            changed.append((ref, sigs[ref], builder(r), _parse(states.get(ref))[1]))
    return _write_rows(cid, source, changed, gone)


def _sync_append(cid: int, source: str, table: str, builder) -> int:
    """Rows that never change once written (chat, emails, facts): index everything above the last indexed id."""
    mark = to_int(_state(cid, source, "__max__"), 0)
    names = _names(cid) if source == "chat" else None
    total = 0
    while True:
        rows = db.q(f"SELECT * FROM {table} WHERE id > ? AND company_id=? ORDER BY id LIMIT 1000", (mark, cid))
        if not rows:
            break
        with _Tx() as tx:
            ts = db.now()
            for r in rows:
                for t, h, b, line in builder(r, names):
                    tx.x(_INSERT, (t, h, b, cid, source, str(r["id"]), line, ts))
            mark = rows[-1]["id"]
            tx.x(_UPSERT, (cid, source, "__max__", str(mark)))
        total += len(rows)
        if len(rows) < 1000:
            break
    return total


def _sync_runs(cid: int) -> int:
    """A session's summary arrives when it finishes, and sessions finish out of order. The stamp keeps a mark
    below which every session is done, plus the finished ids above it."""
    raw = _state(cid, "run", "__mark__") or "0|"
    mark_s, _, done_s = raw.partition("|")
    mark = to_int(mark_s, 0)
    done = {int(x) for x in done_s.split(",") if x.strip().isdigit()}
    rows = db.q("SELECT id, agent_id, task_id, kind, status, summary, started_at, finished_at FROM runs "
                "WHERE id > ? AND company_id=? ORDER BY id", (mark, cid))
    if not rows:
        return 0
    names, new_mark, contiguous, todo = None, mark, True, []
    for r in rows:
        finished = bool(r["finished_at"]) or (r["status"] or "running") != "running"
        stale = not finished and db.minutes_since(r["started_at"]) > 24 * 60   # a crash left it "running"
        if finished and r["id"] not in done:
            todo.append(r)
            done.add(r["id"])
        if contiguous and (finished or stale):
            new_mark = r["id"]
        else:
            contiguous = False
    done = {i for i in done if i > new_mark}
    stamp = f"{new_mark}|{','.join(str(i) for i in sorted(done))}"
    if not todo and stamp == raw:
        return 0
    with _Tx() as tx:
        for r in todo:
            if (r["summary"] or "").strip():
                if names is None:
                    names = _names(cid)
                _put(tx, cid, "run", str(r["id"]), "done", _run_doc(r, names), "", [])
        tx.x(_UPSERT, (cid, "run", "__mark__", stamp))
    return len(todo)


def _check_identity(company: dict) -> None:
    """A new company can get the id of a deleted one (SQLite reuses the highest id). Its index must then start
    empty instead of showing the old company's files and notes."""
    cid = company["id"]
    want = company.get("slug") or str(cid)
    if _state(cid, "meta", "company") == want:
        return
    with _Tx() as tx:
        tx.x("DELETE FROM search_vectors WHERE rowid IN (SELECT rowid FROM search_index WHERE company_id=?)", (cid,))
        tx.x("DELETE FROM search_index WHERE company_id=?", (cid,))
        tx.x("DELETE FROM index_state WHERE company_id=?", (cid,))
        tx.x(_UPSERT, (cid, "meta", "company", want))


# ---------------------------------------------------------------- public indexing entry points

def reindex(company: dict) -> None:
    """Bring the search index up to date for this company (incremental; called at the start of every run).
    Unchanged files cost one stat each and unchanged rows one cheap query per source. An empty index is built in
    full. Never raises."""
    if not company or not _fts_ok():
        return
    try:
        with _index_lock:
            cid = company["id"]
            _check_identity(company)
            root = workspace.root(company)
            changed = _safe(_sync_files, 0, "context", global_dir(), "global:", (), True)
            if changed:
                _safe(_set_meta, 0, "last_indexed", db.now())
            changed += _safe(_sync_files, cid, "file", root, "", ("context",), False)
            changed += _safe(_sync_files, cid, "context", company_dir(company), "context:", (), True)
            changed += _safe(_sync_memory, cid)
            changed += _safe(_sync_rows, cid, "task", _TASK_SIG, "tasks", _task_doc)
            changed += _safe(_sync_rows, cid, "approval", _APPROVAL_SIG, "approvals", _approval_doc)
            changed += _safe(_sync_append, cid, "chat", "messages", _chat_doc)
            changed += _safe(_sync_append, cid, "email", "emails", _email_doc)
            changed += _safe(_sync_append, cid, "fact", "facts", _fact_doc)
            changed += _safe(_sync_runs, cid)
            if changed:
                _safe(_set_meta, cid, "last_indexed", db.now())
                _embed_dirty.add(cid)
            _last_checked[cid] = time.time()
        _maybe_embed(company)
    except Exception:
        _warn(f"reindex of company {company.get('id')}")


def reindex_global() -> None:
    """Re-index the owner's library in data/context/ (shared by every company, stored once as company 0)."""
    if not _fts_ok():
        return
    try:
        with _index_lock:
            if _sync_files(0, "context", global_dir(), "global:", (), True):
                _set_meta(0, "last_indexed", db.now())
    except Exception:
        _warn("indexing the owner's context library")


def reindex_all() -> None:
    """Every company and the global library; for startup or a 'rebuild index' button."""
    for c in db.q("SELECT * FROM companies"):
        reindex(c)


def after_write(ctx: Ctx, rel_path: str) -> None:
    """A file was written or edited by an agent: re-index it so the next search sees it. Never raises."""
    if not _fts_ok():
        return
    try:
        rel = (rel_path or "").replace("\\", "/").lstrip("/")
        parts = rel.split("/")
        if not rel or any(x in INDEX_SKIP or x.startswith(".") for x in parts):
            return
        company = ctx.company
        with _index_lock:
            if _state(company["id"], "meta", "company") != (company.get("slug") or str(company["id"])):
                reindex(company)   # first use: build everything (this file included)
                return
            if parts[0] == "context" and len(parts) > 1:
                _index_one(company["id"], "context", "context:" + "/".join(parts[1:]),
                           workspace.root(company) / rel, True)
            else:
                _index_one(company["id"], "file", rel, workspace.root(company) / rel, False)
            _embed_dirty.add(company["id"])
    except Exception:
        _warn(f"indexing {rel_path}")


def index_status() -> dict:
    """For the dashboard and the Doctor: is the index working and how much it holds."""
    s = db.get_settings()
    model = (s.get("embedding_model") or "").strip()
    out = {"fts": _fts_ok(), "rows": 0, "global_files": 0, "companies": [], "embedding_model": model,
           "vectors": 0}
    if not out["fts"]:
        return out
    out["rows"] = db.scalar("SELECT COUNT(*) FROM search_index") or 0
    rows_by = {r["company_id"]: r["n"] for r in
               db.q("SELECT company_id, COUNT(*) AS n FROM search_index GROUP BY company_id")}
    files_by = {r["company_id"]: r["n"] for r in
                db.q("SELECT company_id, COUNT(*) AS n FROM index_state WHERE source='file' "
                     "AND instr(stamp, char(9) || char(9)) = 0 GROUP BY company_id")}
    last_by = {r["company_id"]: r["stamp"] for r in
               db.q("SELECT company_id, stamp FROM index_state WHERE source='meta' AND ref='last_indexed'")}
    out["global_files"] = db.scalar("SELECT COUNT(*) FROM index_state WHERE company_id=0 AND source='context' "
                                    "AND instr(stamp, char(9) || char(9)) = 0") or 0
    out["global_last_indexed"] = last_by.get(0)
    for c in db.q("SELECT id, name FROM companies ORDER BY id"):
        checked = _last_checked.get(c["id"])   # this process only: when reindex last looked for changes
        out["companies"].append({"id": c["id"], "name": c["name"], "files_indexed": files_by.get(c["id"], 0),
                                 "rows": rows_by.get(c["id"], 0), "last_indexed": last_by.get(c["id"]),
                                 "last_checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(checked))
                                 if checked else None})
    if model:
        out["vectors"] = db.scalar("SELECT COUNT(*) FROM search_vectors WHERE model=?", (model,)) or 0
    return out


# ---------------------------------------------------------------- optional meaning-based search (embeddings)

EMBED_IN_BACKGROUND = True   # tests switch this off to embed synchronously
EMBED_BATCH = 16
EMBED_PER_PASS = 64
EMBED_PAUSE = 300            # seconds to stop trying after a failure, so runs are never slowed by a dead model
_embed_lock = threading.Lock()
_embed_dirty: set[int] = set()
_embed_seen: set[int] = set()
_embed_down_until = 0.0


def _embedding_model(s: dict | None = None) -> tuple[dict, str]:
    s = s or db.get_settings()
    return s, (s.get("embedding_model") or "").strip()


def embed_texts(texts: list[str], settings: dict | None = None, timeout: float = 30.0) -> list[list[float]]:
    """Vectors from the model server (Ollama POST /api/embed, or /v1/embeddings on OpenAI-compatible servers),
    scaled to length 1 so a dot product is the cosine similarity. Raises on any failure."""
    s, model = _embedding_model(settings)
    if not model:
        raise llm.LLMError("No embedding model is set.")
    base = (s.get("llm_base_url") or "").rstrip("/")
    if (s.get("llm_backend") or "ollama") == "ollama":
        data = llm._request(base + "/api/embed", {"model": model, "input": texts}, llm._headers(s), timeout)
        vecs = data.get("embeddings")
    else:
        data = llm._request(llm._openai_base(base) + "/embeddings", {"model": model, "input": texts},
                            llm._headers(s), timeout)
        vecs = [d.get("embedding") for d in sorted(data.get("data") or [], key=lambda d: d.get("index", 0))]
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        raise llm.LLMError("The model server sent an unexpected embedding reply.")
    out = []
    for v in vecs:
        v = [float(x) for x in v]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def embed_pending(company: dict, max_chunks: int = EMBED_PER_PASS) -> int:
    """Give vectors to up to max_chunks chunks (company and library) that have none yet. Returns how many were
    embedded. A failure stops quietly and pauses embedding for a few minutes."""
    global _embed_down_until
    s, model = _embedding_model()
    if not model or not _fts_ok() or time.time() < _embed_down_until:
        return 0
    cid = company["id"]
    rows = db.q("SELECT s.rowid AS rid, s.title, s.heading, s.body FROM search_index s LEFT JOIN search_vectors v "
                "ON v.rowid = s.rowid AND v.model = ? WHERE v.rowid IS NULL AND s.company_id IN (?, 0) "
                "ORDER BY s.rowid DESC LIMIT ?", (model, cid, max_chunks))
    done = 0
    try:
        for i in range(0, len(rows), EMBED_BATCH):
            part = rows[i:i + EMBED_BATCH]
            vecs = embed_texts([f"{r['title']}\n{r['heading']}\n{r['body']}"[:2000] for r in part], s)
            with _index_lock, _Tx() as tx:   # skip chunks replaced while the model was working
                for r, v in zip(part, vecs):
                    tx.x("INSERT OR REPLACE INTO search_vectors(rowid, model, vector) SELECT ?, ?, ? WHERE EXISTS "
                         "(SELECT 1 FROM search_index WHERE rowid = ? AND title = ?)",
                         (r["rid"], model, json.dumps([round(x, 6) for x in v]), r["rid"], r["title"]))
            done += len(part)
    except Exception as e:
        _embed_down_until = time.time() + EMBED_PAUSE
        print(f"[context] embedding paused for {EMBED_PAUSE // 60} minutes: {e}")
        return done
    if len(rows) < max_chunks:
        _embed_dirty.discard(cid)
    return done


def _maybe_embed(company: dict) -> None:
    s, model = _embedding_model()
    if not model:
        return
    cid = company["id"]
    if cid not in _embed_seen:
        _embed_seen.add(cid)
        _embed_dirty.add(cid)
    if cid not in _embed_dirty or time.time() < _embed_down_until:
        return
    if not EMBED_IN_BACKGROUND:
        embed_pending(company)
        return
    if not _embed_lock.acquire(blocking=False):
        return

    def work():
        try:
            embed_pending(company)
        except Exception:
            _warn("embedding")
        finally:
            _embed_lock.release()

    threading.Thread(target=work, daemon=True, name=f"embed-{cid}").start()


def _vector_rank(company: dict, query: str, sources: tuple, n: int = 30) -> list[int]:
    """Rowids of the chunks closest in meaning to the query; [] when embeddings are off or fail."""
    global _embed_down_until
    s, model = _embedding_model()
    if not model or time.time() < _embed_down_until:
        return []
    try:
        qv = embed_texts([query], s, timeout=10)[0]
    except Exception:
        _embed_down_until = time.time() + EMBED_PAUSE
        return []
    src = f" AND s.source IN ({','.join('?' * len(sources))})" if sources else ""
    rows = db.q("SELECT v.rowid AS rid, v.vector FROM search_vectors v JOIN search_index s ON s.rowid = v.rowid "
                f"WHERE v.model = ? AND s.company_id IN (?, 0){src} ORDER BY v.rowid DESC LIMIT 3000",
                (model, company["id"], *sources))
    scored = []
    for r in rows:
        try:
            v = json.loads(r["vector"])
        except (TypeError, ValueError):
            continue
        if len(v) == len(qv):
            scored.append((sum(map(operator.mul, qv, v)), r["rid"]))
    scored.sort(reverse=True)
    return [rid for sim, rid in scored[:n] if sim > 0.2]


def _rrf(ranked: list[dict], vec_ids: list[int], k: int = 60) -> list[dict]:
    """Reciprocal rank fusion: each list adds 1/(k + rank) per hit, so a chunk both rankings like rises."""
    by_id = {r["rid"]: r for r in ranked}
    score: dict[int, float] = {}
    for rank, r in enumerate(ranked):
        score[r["rid"]] = score.get(r["rid"], 0.0) + 1.0 / (k + rank + 1)
    for rank, rid in enumerate(vec_ids):
        score[rid] = score.get(rid, 0.0) + 1.0 / (k + rank + 1)
    missing = [rid for rid in vec_ids if rid not in by_id]
    if missing:
        for r in db.q(f"SELECT rowid AS rid, source, ref, line, title, heading, body FROM search_index "
                      f"WHERE rowid IN ({','.join('?' * len(missing))})", missing):
            r["snip"], r["score"] = None, None
            by_id[r["rid"]] = r
    return [by_id[rid] for rid in sorted(score, key=lambda x: -score[x]) if rid in by_id]


# ---------------------------------------------------------------- search

def parse_scope(scope: str) -> tuple[str, ...]:
    """Sources for a scope name (several may be given, separated by commas). Raises ValueError for unknown ones."""
    out: list[str] = []
    for word in re.split(r"[,\s|+]+", (scope or "all").strip().lower()) or ["all"]:
        key = _SCOPE_ALIASES.get(word, word)
        if key not in SCOPES:
            raise ValueError(f"unknown scope '{word}'. Use one of: {', '.join(SCOPES)}.")
        out += [s for s in SCOPES[key] if s not in out]
    return tuple(out) or ALL_SOURCES


def _terms(query: str) -> list[str]:
    words = [w for w in re.findall(r"\w+", (query or "").lower()) if w.strip("_")]
    keep = [w for w in words if w not in _STOP] or words
    out: list[str] = []
    for w in keep:
        if w not in out:
            out.append(w)
    return out[:12]


def _match(terms: list[str], mode: str) -> str:
    """An FTS5 query from plain words. Each word is quoted, so nothing a model types can break the syntax."""
    quoted = [f'"{t}"' for t in terms]
    return " ".join(quoted) if mode == "and" else " OR ".join(quoted)


def _stems(terms: list[str]) -> list[str]:
    """Word starts used to find the matching line inside a chunk (the index matches 'pricing' for 'price')."""
    return [t if len(t) <= 4 else t[:max(4, len(t) - 3)] for t in terms]


def _line_offset(body: str, terms: list[str]) -> int:
    stems = _stems(terms)
    best, best_n = 0, 0
    for i, line in enumerate(body.split("\n")):
        low = line.lower()
        n = sum(1 for s in stems if s in low)
        if n > best_n:
            best, best_n = i, n
            if n == len(stems):
                break
    return best


def _make_snippet(body: str, terms: list[str], width: int = 180) -> str:
    """A snippet with the query words in [brackets], for hits that FTS5 did not give a snippet for."""
    text = " ".join((body or "").split())
    stems = _stems(terms)
    low = text.lower()
    pos = min([p for p in (low.find(s) for s in stems) if p >= 0] or [0])
    start = max(0, pos - width // 3)
    piece = text[start:start + width]
    if stems:
        rx = re.compile(r"\b(\w*(?:" + "|".join(re.escape(s) for s in stems) + r")\w*)", re.I)
        piece = rx.sub(r"[\1]", piece)
    return ("..." if start else "") + piece + ("..." if start + width < len(text) else "")


_LABEL = {"memory": "memory", "task": "task", "email": "email", "chat": "chat", "approval": "approval",
          "run": "session", "fact": "fact"}


def _row_heading(src: str, title: str, heading: str) -> str:
    title, heading = (title or "").strip(), (heading or "").strip()
    if src in ("task", "run"):
        return f"{title} {heading}".strip()
    if src in ("email", "approval"):
        return f"{title} ({heading})" if heading else title
    if src == "chat":
        return f"{title}, {heading}" if heading else title
    if src == "fact":
        return heading
    return title


def _result(r: dict, terms: list[str]) -> dict:
    src, ref = r["source"], str(r.get("ref") or "")
    snip = r.get("snip")
    if not snip:
        snip = _make_snippet(r.get("body") or "", terms)
    out = {"source": src, "ref": ref, "path": "", "line": None, "heading": "", "snippet": _short(snip, 240),
           "title": r.get("title") or "", "id": None}
    if src in ("file", "context"):
        line = to_int(r.get("line"), 1) or 1
        if _ext(ref) not in (".html", ".htm"):   # HTML chunks hold page text, not raw lines
            line += _line_offset(r.get("body") or "", terms)
        out.update(path=ref, line=line, heading=_short(r.get("heading") or "", 80))
    else:
        rid = to_int(ref)
        out.update(id=rid, ref=f"{_LABEL.get(src, src)} #{rid}",
                   heading=_short(_row_heading(src, r.get("title") or "", r.get("heading") or ""), 90))
    return out


def _fts_rows(cid: int, match: str, sources: tuple, limit: int) -> list[dict]:
    src = f" AND source IN ({','.join('?' * len(sources))})" if sources else ""
    return db.q("SELECT rowid AS rid, source, ref, line, title, heading, body, "
                "snippet(search_index, 2, '[', ']', '...', 18) AS snip, bm25(search_index, 3.0, 2.0, 1.0) AS score "
                f"FROM search_index WHERE search_index MATCH ? AND company_id IN (?, 0){src} "
                "ORDER BY score LIMIT ?", (match, cid, *sources, limit))


def _fts_search(company: dict, query: str, terms: list[str], sources: tuple, limit: int) -> list[dict]:
    cid = company["id"]
    want = limit * 3   # room for the per-file cap below
    ranked = _fts_rows(cid, _match(terms, "and"), sources, want)
    if len(terms) > 1 and len(ranked) < want:   # every word first, then any word
        seen = {r["rid"] for r in ranked}
        ranked += [r for r in _fts_rows(cid, _match(terms, "or"), sources, want) if r["rid"] not in seen]
    vec = _vector_rank(company, query, sources)
    if vec:
        ranked = _rrf(ranked, vec)
    out, per = [], {}
    for r in ranked:
        key = (r["source"], r["ref"])
        if per.get(key, 0) >= 3:   # several hits in one long file should not crowd out everything else
            continue
        per[key] = per.get(key, 0) + 1
        out.append(_result(r, terms))
        if len(out) >= limit:
            break
    return out


_LIKE_TABLES = {
    "memory": ("memory", ("content",), "id, kind, pinned, content, created_at", _memory_doc),
    "task": ("tasks", ("title", "description", "acceptance", "result", "notes"), "*", _task_doc),
    "email": ("emails", ("subject", "body", "from_addr", "to_addr"), "*", _email_doc),
    "chat": ("messages", ("content",), "*", _chat_doc),
    "approval": ("approvals", ("summary", "payload", "result", "feedback"), "*", _approval_doc),
    "run": ("runs", ("summary",), "id, agent_id, task_id, kind, status, summary", _run_doc),
    "fact": ("facts", ("claim", "quote", "url"), "*", _fact_doc),
}


def _like_search(company: dict, terms: list[str], sources: tuple, limit: int) -> list[dict]:
    """Plain scans for when FTS5 is missing: ranked only by how many of the words match, but the tool answers."""
    cid = company["id"]
    stems = _stems(terms)
    hits: list[tuple[int, int, dict]] = []
    targets = []
    if "file" in sources:
        targets.append((workspace.root(company), "", ("context",), False, "file"))
    if "context" in sources:
        targets += [(company_dir(company), "context:", (), True, "context"),
                    (global_dir(), "global:", (), True, "context")]
    for base, prefix, skip, library, src in targets:
        for rel, ap, size, _m in sorted(_walk(base, skip)):
            if _binary_name(rel) or (library and _ext(rel) not in LIB_FORMATS):
                continue
            text = _load_text(Path(ap), size)
            if text is None:
                continue
            in_name = sum(1 for s in stems if s in rel.lower())
            found = []
            for n, line in enumerate(_lines(text), 1):
                k = sum(1 for s in stems if s in line.lower())
                if k:
                    found.append((k, n, line))
            if not found and not in_name:
                continue
            found.sort(key=lambda f: (-f[0], f[1]))
            for k, n, line in found[:2] or [(0, 1, text[:200])]:
                row = {"source": src, "ref": prefix + rel, "line": n, "heading": "", "title": prefix + rel,
                       "body": line, "snip": _make_snippet(line, terms)}
                hits.append((k + in_name, len(hits), _result(row, terms)))
    names = _names(cid)
    for src, (table, cols, select, builder) in _LIKE_TABLES.items():
        if src not in sources:
            continue
        cond = " OR ".join(f"COALESCE({c},'') LIKE ?" for _t in terms for c in cols)
        params = [f"%{t}%" for t in terms for _c in cols]
        try:
            rows = db.q(f"SELECT {select} FROM {table} WHERE company_id=? AND ({cond}) ORDER BY id DESC LIMIT 200",
                        (cid, *params))
        except sqlite3.Error:
            continue
        for r in rows:
            for title, heading, body, _l in builder(r, names):
                low = f"{title}\n{heading}\n{body}".lower()
                k = sum(1 for s in stems if s in low)
                row = {"source": src, "ref": str(r["id"]), "title": title, "heading": heading, "body": body,
                       "snip": None, "line": None}
                hits.append((k, len(hits), _result(row, terms)))
    hits.sort(key=lambda h: (-h[0], h[1]))
    return [h[2] for h in hits[:limit]]


def search_rows(company: dict, query: str, scope: str = "all", limit: int = 8) -> list[dict]:
    """Ranked hits as dicts: source, ref, path, line, heading, snippet, title, id. Raises ValueError for an
    unknown scope. Used by the search tool and the dashboard's search box."""
    sources = parse_scope(scope)
    limit = max(1, min(int(limit or 8), 20))
    terms = _terms(query)
    if not terms:
        return []
    if _fts_ok():
        try:
            return _fts_search(company, query, terms, sources, limit)
        except sqlite3.Error:
            _warn("full-text search")
    return _like_search(company, terms, sources, limit)


_GROUPS = {
    "file": ("Files", "open one with read_file <path> start_line=<line>"),
    "context": ("Owner's context (read-only)", "open one with read_file <path> start_line=<line>"),
    "task": ("Tasks", "see them with list_tasks status=all; add a note with update_task task_id=<number>"),
    "memory": ("Memory", "saved notes; add one with remember"),
    "approval": ("Decisions", "requests to the owner and what happened to them"),
    "email": ("Emails", "read inbound mail with check_inbox include_handled=true"),
    "chat": ("Chat with the owner", ""),
    "run": ("Past sessions", "summaries of earlier work sessions"),
    "fact": ("Facts", "verified facts; cite one in a file as [fact <number>]"),
}


def format_results(query: str, scope: str, rows: list[dict]) -> str:
    where = "" if (scope or "all").strip().lower() in ("", "all") else f" in {scope}"
    if not rows:
        return (f'No results for "{query}"{where}. Try fewer or other words, another scope, or grep for exact '
                "text in files.")
    order: list[str] = []
    for r in rows:
        if r["source"] not in order:
            order.append(r["source"])
    out = [f'Search results for "{_short(query, 80)}"{where} ({len(rows)}, best first):']
    for src in order:
        label, how = _GROUPS.get(src, (src, ""))
        out.append("")
        out.append(f"{label}: {how}" if how else f"{label}:")
        for r in rows:
            if r["source"] != src:
                continue
            loc = f"{r['path']}:{r['line']}" if r["path"] else r["ref"]
            bits = [loc] + ([r["heading"]] if r["heading"] else []) + [r["snippet"]]
            out.append(_short(" | ".join(bits), 300))
    return "\n".join(out)


@tool("search", "Search everything the company knows: its files, the owner's context, memory, tasks, emails, chat, "
      "decisions, past sessions and verified facts. Returns ranked hits with path:line and the matching words in "
      "[brackets]. Call this first when you need to find something.",
      {"query": S("Words to look for"),
       "scope": S("all (default), files, context, memory, tasks, emails, chat, decisions, sessions or facts"),
       "limit": I("How many results, 1-20 (default 8)")}, ("query",))
def search(ctx: Ctx, query: str, scope: str = "all", limit: int = 8) -> str:
    if not (query or "").strip():
        raise ToolError("Give some words to search for.")
    try:
        parse_scope(scope)
    except ValueError as e:
        raise ToolError(str(e)) from None
    reindex(ctx.company)   # cheap when nothing changed; finds notes and tasks saved earlier in this session
    rows = search_rows(ctx.company, query, scope, limit)
    _tools._log(ctx, "tool", f'searched for "{_short(query, 100)}"')
    return format_results(query, scope, rows)


# ---------------------------------------------------------------- read_file, list_files, grep, edit_file

def read_file(ctx: Ctx, path: str, offset: int = 0, start_line: int = 0, end_line: int = 0,
              outline: bool = False) -> str:
    """The read_file tool body: numbered lines (200 by default, with a pointer to the next range), an outline of
    the headings, or the old character offset when one is given. context: and global: read the owner's library."""
    p, shown, _kind = resolve(ctx.company, path)
    if p.is_dir():
        raise ToolError(f"{path} is a folder. Use list_files {shown or '.'} to see what is inside.")
    if not p.exists() or not p.is_file():
        raise ToolError(f"{path} doesn't exist. Use list_files to see what's there.")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ToolError("File is too large to read.")
    raw = p.read_bytes()
    if b"\x00" in raw[:2000]:
        raise ToolError("That's a binary file.")
    text = raw.decode("utf-8", "replace")
    if offset and offset > 0 and not start_line and not end_line and not outline:
        # The old character-offset reader, unchanged, for old transcripts and models that still use it.
        chunk = text[offset:offset + 12000]
        more = f"\n[{len(text) - offset - 12000} more characters: read again with offset={offset + 12000}]" \
            if len(text) > offset + 12000 else ""
        return chunk + more
    text = text.lstrip("\ufeff")
    lines = _lines(text)
    total = len(lines)
    if outline:
        return _outline_text(shown, text, lines)
    if total == 0:
        return f"{shown} is empty."
    start = max(1, int(start_line or 1))
    if start > total:
        raise ToolError(f"{shown} has only {total} lines. Read from start_line=1, or use outline=true.")
    if end_line and end_line >= start:
        end = min(total, int(end_line), start + READ_MAX_LINES - 1)
    else:
        end = min(total, start + READ_LINES - 1)
    out, used, last = [], 0, start - 1
    for n in range(start, end + 1):
        line = lines[n - 1]
        if len(line) > LONG_LINE:
            line = (line[:LONG_LINE] + f" [line cut: {len(line) - LONG_LINE} more characters; read them with "
                    f"read_file {shown} offset={_line_start(text, n) + LONG_LINE}]")
        entry = f"{n}: {line}"
        if out and used + len(entry) > READ_CHARS:
            break
        out.append(entry)
        used += len(entry) + 1
        last = n
    head = f"{shown}: lines {start}-{last} of {total}"
    if last < total:
        nxt = min(total, last + READ_LINES)
        tail = f"[lines {last + 1}-{nxt} next: read_file {shown} start_line={last + 1}]"
    else:
        tail = "[end of file]"
    return head + "\n" + "\n".join(out) + "\n" + tail


def _line_start(text: str, n: int) -> int:
    """Character offset where line n (1-based) starts, for the old offset reader."""
    pos = 0
    for _ in range(n - 1):
        nl = text.find("\n", pos)
        if nl < 0:
            return len(text)
        pos = nl + 1
    return pos


def _outline_text(shown: str, text: str, lines: list[str]) -> str:
    ext = _ext(shown.split(":", 1)[-1])
    entries = outline_entries(text, ext, lines)
    total = len(lines)
    if ext in (".csv", ".tsv") and lines:
        return (f"{shown}: {total} lines. Columns: {_short(lines[0], 300)}\n"
                f"[Read rows with read_file {shown} start_line=2]")
    if not entries:
        return (f"{shown}: {total} lines. No headings found; read it with read_file {shown} start_line=1 "
                "(200 lines at a time).")
    out, used = [f"{shown}: {total} lines, {len(entries)} headings"], 0
    for n, title in entries:
        entry = f"{n}: {title}"
        if used + len(entry) > READ_CHARS - 200:
            out.append(f"[{len(entries) - len(out) + 1} more headings not shown]")
            break
        out.append(entry)
        used += len(entry) + 1
    out.append(f"[Read a part with read_file {shown} start_line=N]")
    return "\n".join(out)


def _summaries(cid: int) -> dict[str, str]:
    """File summaries saved by the index, keyed by the path list_files shows."""
    out: dict[str, str] = {}
    if not _fts_ok():
        return out
    for src in ("file", "context"):
        for ref, st in _states(cid, src).items():
            summary = _parse(st)[2]
            if summary:
                out["context/" + ref[8:] if ref.startswith("context:") else ref] = summary
    return out


def _fit_listing(entries: list[tuple[str, str]], more_hint: str) -> str:
    """Lines for list_files within the tool result limit: summaries go first when room runs short."""
    out, used = [], 0
    for i, (plain, summary) in enumerate(entries):
        line = f"{plain}: {summary}" if summary and used + len(plain) + len(summary) < READ_CHARS - 600 else plain
        if used + len(line) > READ_CHARS:
            out.append(f"[{len(entries) - i} more entries not shown; {more_hint}]")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def list_files(ctx: Ctx, path: str = ".") -> str:
    """The list_files tool body: today's listing plus each file's one-line summary from the index."""
    raw = (path or ".").strip()
    if raw.lower().startswith(("global:", "context:")):
        return _list_library(ctx.company, raw)
    items = workspace.tree(ctx.company, raw or ".", depth=4, limit=300)
    if not items:
        return "(empty)"
    sums = _summaries(ctx.company["id"])
    entries = [(f"{i['path']}/", "") if i["dir"] else (f"{i['path']} ({i['size']} bytes)", sums.get(i["path"], ""))
               for i in items]
    return _fit_listing(entries, "list a sub-folder, for example list_files path=site")


def _list_library(company: dict, raw: str) -> str:
    p, shown, kind = resolve(company, raw)
    if p.is_file():
        return f"{shown} is a file. Read it with read_file {shown}."
    cid = 0 if kind == "global" else company["id"]
    base = global_dir() if kind == "global" else company_dir(company)
    prefix = "global:" if kind == "global" else "context:"
    states = _states(cid, "context") if _fts_ok() else {}
    entries = []
    for rel, ap, size, _m in sorted(_walk(p), key=lambda x: x[0].lower()):
        ref = prefix + _rel_to(base, Path(ap))
        if _ext(rel) not in LIB_FORMATS:
            entries.append((f"{ref} ({size} bytes)", "not a text file, so it cannot be read or searched"))
        else:
            entries.append((f"{ref} ({size} bytes)", _parse(states.get(ref))[2]))
    if not entries:
        return f"(empty: the owner has not put any context files in {shown})"
    return _fit_listing(entries, f"list a sub-folder, for example list_files path={prefix}notes")


def _excerpt(line: str, pos: int, width: int = 160) -> str:
    s = line.strip()
    if len(s) <= width:
        return s
    pos = max(0, pos - (len(line) - len(line.lstrip())))
    start = max(0, pos - width // 3)
    return ("..." if start else "") + s[start:start + width] + ("..." if start + width < len(s) else "")


@tool("grep", "Find exact text in the company's files, or a regular expression with regex=true. Returns up to 50 "
      "matching lines as path:line: text. Upper and lower case count as the same.",
      {"pattern": S("The text to find (or a regular expression when regex is true)"),
       "path": S("Folder or file to search (default: the whole company folder; global: for the owner's library)"),
       "regex": {"type": "boolean", "description": "Treat pattern as a regular expression"}}, ("pattern",))
def grep(ctx: Ctx, pattern: str, path: str = ".", regex: bool = False) -> str:
    if not pattern:
        raise ToolError("Give the text to look for.")
    if regex:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ToolError(f"that regular expression does not work ({e}). Fix it, or search plain text with "
                            "regex=false.") from None

        def find(line: str) -> int:
            m = rx.search(line[:4000])
            return m.start() if m else -1
    else:
        needle = pattern.lower()

        def find(line: str) -> int:
            return line.lower().find(needle)
    target, shown, kind = resolve(ctx.company, path or ".")
    if target.is_file():
        files = [(shown, target)]
    elif target.is_dir():
        if kind == "file":
            prefix = "" if shown in ("", ".") else shown + "/"
        else:
            prefix = shown if shown.endswith(":") else shown + "/"
        files = sorted(((prefix + rel, Path(ap)) for rel, ap, size, _m in _walk(target)
                        if size <= MAX_FILE_BYTES and not _binary_name(rel)), key=lambda f: f[0])
    else:
        raise ToolError(f"{path} doesn't exist. Use list_files to see what's there.")
    out, used, searched, truncated = [], 0, 0, False
    for disp, p in files:
        text = _load_text(p)
        if text is None:
            continue
        searched += 1
        for n, line in enumerate(_lines(text), 1):
            pos = find(line)
            if pos < 0:
                continue
            entry = f"{disp}:{n}: {_excerpt(line, pos)}"
            if len(out) >= 50 or used + len(entry) > READ_CHARS:
                truncated = True
                break
            out.append(entry)
            used += len(entry) + 1
        if truncated:
            break
    where = "the company folder" if shown in ("", ".") else shown
    if not out:
        return f"No matches for {pattern!r} in {where} ({searched} files searched)."
    note = f"\n[stopped at {len(out)} matches; use a narrower pattern or path]" if truncated else ""
    return "\n".join(out) + note


_NUM_PREFIX = re.compile(r"^\s*\d+: ?")


def _strip_line_numbers(s: str) -> str | None:
    """The text without read_file's "12: " prefixes, when every non-empty line has one."""
    lines = s.split("\n")
    if not any(x.strip() for x in lines) or not all(_NUM_PREFIX.match(x) for x in lines if x.strip()):
        return None
    return "\n".join(_NUM_PREFIX.sub("", x, count=1) for x in lines)


def _call_write_guard(ctx: Ctx, rel: str, text: str):
    """truth.write_guard, told that this is an edit when its signature accepts that."""
    g = _tools.truth.write_guard
    try:
        params = inspect.signature(g).parameters
        if "is_edit" in params or any(x.kind == x.VAR_KEYWORD for x in params.values()):
            return g(ctx, rel, text, is_edit=True)
    except (TypeError, ValueError):
        pass
    return g(ctx, rel, text)


@tool("edit_file", "Change part of a file: replaces the exact text `find` with `replace`. Copy `find` from read_file "
      "output without the line numbers, with enough of the surrounding text to match one place. Prefer this to "
      "rewriting a whole file.",
      {"path": S("Path relative to the company folder, e.g. site/index.html"),
       "find": S("The exact text to replace, copied from the file"),
       "replace": S("The new text (empty to delete)"),
       "count": I("How many places to change (default 1); refused when the text appears more often")},
      ("path", "find", "replace"))
def edit_file(ctx: Ctx, path: str, find: str, replace: str, count: int = 1) -> str:
    if (path or "").strip().lower().startswith(("context:", "global:")):
        raise ToolError(OWNER_ONLY)
    p = workspace.safe_path(ctx.company, path)
    r = workspace.rel(ctx.company, p)
    if r == "context" or r.startswith("context/"):
        raise ToolError(OWNER_ONLY)
    if not find:
        raise ToolError("find is empty. Copy the exact text you want to change from read_file (without the line "
                        "numbers).")
    if p.is_dir():
        raise ToolError(f"{r} is a folder.")
    if not p.is_file():
        raise ToolError(f"{r} doesn't exist. Create it with write_file, or use list_files to find the right path.")
    text = _load_text(p, keep_bom=True)
    if text is None:
        raise ToolError("That file is binary or too large to edit.")
    count = max(1, int(count or 1))
    old, new = find, replace
    if old not in text:
        stripped = _strip_line_numbers(find)
        if stripped and stripped in text:   # copied together with read_file's line numbers
            plain = _strip_line_numbers(replace)
            old, new = stripped, plain if plain is not None else replace
        elif "\r\n" in text and "\r\n" not in find and find.replace("\n", "\r\n") in text:   # Windows line ends
            old, new = find.replace("\n", "\r\n"), replace.replace("\r\n", "\n").replace("\n", "\r\n")
    n = text.count(old)
    if n == 0:
        hint = ""
        first = next((x.strip() for x in find.split("\n") if x.strip()), "")
        if len(first) >= 4:
            for i, line in enumerate(_lines(text), 1):
                if first in line:
                    hint = f" Its first line looks like line {i}: read_file {r} start_line={max(1, i - 3)}."
                    break
        raise ToolError("the text to change was not found; read the file around the place you mean with read_file "
                        "and copy the text exactly." + hint)
    if n > count:
        at = _line_numbers(text, old, 6)
        listed = ", ".join(str(x) for x in at[:5]) + (", ..." if n > 5 else "")
        raise ToolError(f"that text appears {n} times in {r} (lines {listed}). Include more of the surrounding text "
                        f"so it matches one place, or set count={n} to change all of them.")
    new_text = text.replace(old, new, n)
    if new_text == text:
        return f"No change: the new text is the same as the old in {r}."
    # The same guards as write_file: records/ and context/, log-like names, unsupported claims, the charter path.
    refusal = _call_write_guard(ctx, r, new_text) or _tools.planning.charter_write_hook(ctx, r, new_text)
    if refusal:
        return refusal
    p.write_text(new_text, encoding="utf-8", newline="")
    ctx.written.add(r)
    _tools._log(ctx, "file", f"updated {r}", path=r, task_id=_tools._task_id(ctx))
    after_write(ctx, r)
    return (f"Edited {r}: {n} change{'s' if n != 1 else ''}. The changed lines now read:\n"
            + _changed_lines(text, new_text, old, new, n))


def _line_numbers(text: str, needle: str, limit: int) -> list[int]:
    out, i = [], 0
    while len(out) < limit:
        j = text.find(needle, i)
        if j < 0:
            break
        out.append(text.count("\n", 0, j) + 1)
        i = j + len(needle)
    return out


def _changed_lines(text: str, new_text: str, old: str, new: str, n: int) -> str:
    """Each changed place with two lines of context around it, numbered as read_file numbers them."""
    starts, i = [], 0
    while len(starts) < n:
        j = text.find(old, i)
        if j < 0:
            break
        starts.append(j)
        i = j + len(old)
    delta = len(new) - len(old)
    lines = _lines(new_text.lstrip("\ufeff"))
    blocks: list[list[int]] = []
    for k, s in enumerate(starts):
        pos = s + k * delta
        first = new_text.count("\n", 0, pos) + 1
        last = first + new.count("\n")
        a, b = max(1, first - 2), min(len(lines), last + 2)
        if blocks and a <= blocks[-1][1] + 1:
            blocks[-1][1] = max(blocks[-1][1], b)
        else:
            blocks.append([a, b])
    out, used = [], 0
    for a, b in blocks:
        if out:
            out.append("...")
        for ln in range(a, b + 1):
            text_line = lines[ln - 1]
            entry = f"{ln}: {_short(text_line, 300) if len(text_line) > 300 else text_line}"
            if used + len(entry) > 3000:
                out.append("[more changes not shown; read the file to check them]")
                return "\n".join(out)
            out.append(entry)
            used += len(entry) + 1
    return "\n".join(out) or "(the file is empty now)"


# ---------------------------------------------------------------- prompt sections

_CORE_FIELDS = (("customer", "Customer"), ("offer", "Offer and price"), ("reach", "How we reach customers"),
                ("north_star", "North Star"), ("riskiest", "Riskiest assumption"))


def _charter_key(title: str) -> str | None:
    t = re.sub(r"^[\d.\s)]+", "", (title or "").lower()).strip(" *_:#")
    if t.startswith(("problem", "competitor", "business model", "risks", "kpi", "30-day", "solution")):
        return None   # these often mention customers or prices but are not the sections the core shows
    if "north star" in t:
        return "north_star"
    if "riskiest" in t or "risky assumption" in t or "key assumption" in t:
        return "riskiest"
    if "one-liner" in t or "one liner" in t or "oneliner" in t:
        return "one_liner"
    if any(w in t for w in ("reach", "go-to-market", "go to market", "channel", "distribution", "acquisition")):
        return "reach"
    if "offer" in t or "pric" in t:
        return "offer"
    if "customer" in t or "audience" in t or "target market" in t or "who it is for" in t:
        return "customer"
    return None


_BOLD_LABEL = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*|__)([^*_\n]{2,60}?):?(?:\*\*|__)\s*:?\s*(.*)$")
_PLAIN_LABEL = re.compile(r"^([A-Z][A-Za-z &/'()-]{2,40}):\s+(\S.*)$")


def _plain(line: str) -> str:
    s = re.sub(r"^\s*(?:[-*+]|\d+[.)]|>)\s+", "", line or "")
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[*_`]+", "", s)
    return " ".join(s.split())


def charter_fields(text: str) -> dict[str, str]:
    """The charter's key sections as plain text: one_liner, customer, offer, reach, north_star, riskiest. Works
    with Markdown headings, bold labels ("**Customer:** ...") and plain labels ("Customer: ...")."""
    sections: list[list] = []
    cur: list | None = None
    fence = False
    for line in _lines(text or ""):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        m = _MD_HEAD.match(line)
        if m:
            cur = [m.group(2), []]
            sections.append(cur)
            continue
        m = _BOLD_LABEL.match(line) or _PLAIN_LABEL.match(line)
        if m and _charter_key(m.group(1)):
            cur = [m.group(1), [m.group(2)]]
            sections.append(cur)
            continue
        if cur is not None:
            cur[1].append(line)
    out: dict[str, str] = {}
    for title, body in sections:
        key = _charter_key(title)
        if key and key not in out:
            txt = " ".join(x for x in (_plain(b) for b in body) if x)
            if txt:
                out[key] = txt
    return out


def _fit(values: list[str], budget: int) -> list[str]:
    """Share budget characters: short values keep their full text, long ones split what is left."""
    out: list[str] = [""] * len(values)
    left = budget
    for done, idx in enumerate(sorted(range(len(values)), key=lambda i: len(values[i]))):
        share = max(60, left // max(1, len(values) - done))
        v = values[idx] if len(values[idx]) <= share else _short(values[idx], share)
        out[idx] = v
        left -= len(v)
    return out


def company_core(company: dict) -> str:
    """The '# The company' section: name, one-liner, stage, idea, then the charter's key points (customer, offer
    and price, how we reach customers, North Star, riskiest assumption) and where the full charter is."""
    charter = (company.get("charter") or "").strip()
    if not charter:
        try:   # the charter of record is the database; a charter that exists only as a file still counts
            p = workspace.root(company) / "docs" / "charter.md"
            charter = (_load_text(p) or "").strip() if p.is_file() else ""
        except OSError:
            charter = ""
    fields = charter_fields(charter) if charter else {}
    mission = (company.get("mission") or "").strip() or fields.get("one_liner", "")
    head = [f"Name: {company['name']}", f"One-liner: {_short(mission, 300) if mission else '(not set yet)'}",
            f"Stage: {company.get('stage') or 'idea'}"]
    if not fields:
        text = ("\n".join(head) + f"\nOriginal idea: {(company.get('idea') or '')[:600]}\n\n"
                f"Charter:\n{charter or '(not written yet)'}")
        if len(text) > CORE_CAP:
            tail = "\n[charter cut here; the full text is in docs/charter.md]"
            text = text[:CORE_CAP - len(tail)].rstrip() + tail
        return text
    head.append(f"Original idea: {_short(company.get('idea') or '', 300)}")
    present = [(label, fields[k]) for k, label in _CORE_FIELDS if fields.get(k)]
    missing = [label for k, label in _CORE_FIELDS if not fields.get(k)]
    tail = ["Full charter: read_file docs/charter.md"]
    if missing:
        tail.insert(0, "Not in the charter yet: " + ", ".join(missing) + ".")
    fixed = len("\n".join(head)) + len("\n\nCharter, key points:") + len("\n".join(tail)) + 2
    budget = CORE_CAP - fixed - sum(len(label) + 3 for label, _v in present)
    values = _fit([v for _l, v in present], budget) if present else []
    lines = head + ["", "Charter, key points:"] + [f"{label}: {v}" for (label, _v), v in zip(present, values)] + tail
    text = "\n".join(lines)
    return text if len(text) <= CORE_CAP else text[:CORE_CAP - 3].rstrip() + "..."


def _relevant_memory(company: dict, text: str, exclude: set, n: int = 10) -> list[dict]:
    """Notes that match the task's words, best first."""
    terms = _terms(text)[:14]
    if not terms:
        return []
    ids: list[int] = []
    if _fts_ok():
        try:
            ids = [to_int(r["ref"]) for r in _fts_rows(company["id"], _match(terms, "or"), ("memory",),
                                                       n + len(exclude))]
        except sqlite3.Error:
            ids = []
    else:
        ids = [r["id"] for r in _like_search(company, terms, ("memory",), n + len(exclude))]
    ids = [i for i in ids if i and i not in exclude][:n]
    if not ids:
        return []
    rows = {m["id"]: m for m in db.q(f"SELECT * FROM memory WHERE company_id=? AND id IN "
                                     f"({','.join('?' * len(ids))})", (company["id"], *ids))}
    return [rows[i] for i in ids if i in rows]


def memory_section(company: dict, agent: dict, task: dict | None) -> str:
    """Shared memory for the prompt (at most 1,800 characters): pinned notes first, then the notes most relevant
    to the task (found by search), or the newest ones for planning and chat."""
    cid = company["id"]
    pinned = db.q("SELECT * FROM memory WHERE company_id=? AND pinned=1 ORDER BY id DESC LIMIT 15", (cid,))
    if task:
        others = _relevant_memory(company, f"{task.get('title') or ''} {task.get('description') or ''}",
                                  {m["id"] for m in pinned})
        if len(others) < 6:   # few matches: top up with the newest notes
            have = {m["id"] for m in others} | {m["id"] for m in pinned}
            others += [m for m in db.q("SELECT * FROM memory WHERE company_id=? AND pinned=0 ORDER BY id DESC "
                                       "LIMIT 14", (cid,)) if m["id"] not in have][:8 - len(others)]
    else:
        others = list(reversed(db.q("SELECT * FROM memory WHERE company_id=? AND pinned=0 ORDER BY id DESC "
                                    "LIMIT 14", (cid,))))
    if not pinned and not others:
        return ""
    more = '(More notes: search("words", scope="memory"))'
    budget = MEMORY_CAP - len(more) - 1
    used, cut = 0, False

    def take(candidates: list[str]) -> list[str]:
        nonlocal used, cut
        kept = []
        for line in candidates:
            line = " ".join(line.split())
            if used + len(line) + 1 > budget:
                cut = True
                continue
            kept.append(line)
            used += len(line) + 1
        return kept

    lines = take([f"- (pinned) {m['content'][:400]}" for m in pinned])
    rest = [f"- [{m['kind']}] {m['content'][:300]}" for m in others]
    if task:
        lines += take(rest)   # best match first: what does not fit is the least relevant
    else:
        lines += list(reversed(take(list(reversed(rest)))))   # oldest first, but keep the newest when room runs out
    total = db.scalar("SELECT COUNT(*) FROM memory WHERE company_id=?", (cid,)) or 0
    if cut or total > len(lines):
        lines.append(more)
    return "\n".join(lines)


def _pinned(company: dict | None = None) -> list[str]:
    """Pinned library paths: the global setting (company None) or the company setting."""
    v = db.get_settings().get("pinned_context") if company is None else \
        db.company_settings(company).get("pinned_context")
    out: list[str] = []
    for item in v if isinstance(v, list) else []:
        p = str(item).strip().replace("\\", "/")
        for pre in (("global:",) if company is None else ("context:", "context/")):
            if p.lower().startswith(pre):
                p = p[len(pre):]
        p = p.lstrip("/")
        if p and p not in out:
            out.append(p)
    return out


def pinned_section(company: dict) -> str:
    """Owner context files marked 'always include' (global first, then the company's), at most 2,500 characters.
    When they do not fit, the prompt says which were cut and how to read them."""
    items = [("global:" + p, global_dir(), p) for p in _pinned(None)] + \
            [("context:" + p, company_dir(company), p) for p in _pinned(company)]
    if not items:
        return ""
    head = "# Owner context (always included; the owner wrote these, follow them)"
    budget = PINNED_CAP - len(head) - 260   # room for the note about cut files
    parts, cut, used = [head], [], 0
    for ref, base, rel in items:
        try:
            p = _inside(base, rel)
        except workspace.PathError:
            continue
        if not p.is_file() or _ext(p.name) not in LIB_FORMATS:
            continue
        text = (_load_text(p) or "").strip()
        if not text:
            continue
        block = f"## {ref}\n{text}"
        if used + len(block) + 1 <= budget:
            parts.append(block)
            used += len(block) + 1
            continue
        room = budget - used - len(ref) - 120
        if room >= 300:   # show the start of the file and say where to continue
            keep, size = [], 0
            for ln in _lines(text):
                if size + len(ln) + 1 > room:
                    break
                keep.append(ln)
                size += len(ln) + 1
            if keep:
                parts.append(f"## {ref}\n" + "\n".join(keep) +
                             f"\n[cut for room; read the rest with read_file {ref} start_line={len(keep) + 1}]")
                used = budget
                continue
        cut.append(ref)
    if len(parts) == 1 and not cut:
        return ""
    if cut:
        parts.append(_short("Pinned files cut for room (read them with read_file): " + ", ".join(cut), 250))
    text = "\n".join(parts)
    return text if len(text) <= PINNED_CAP else text[:PINNED_CAP - 3] + "..."


def _library(company: dict) -> list[tuple[str, Path]]:
    """(ref, path) of readable files in the global and company libraries, at most 60 of each."""
    out = []
    for base, prefix in ((global_dir(), "global:"), (company_dir(company), "context:")):
        files = sorted((rel for rel, _ap, size, _m in _walk(base)
                        if _ext(rel) in LIB_FORMATS and size <= MAX_FILE_BYTES), key=str.lower)
        out += [(prefix + rel, base / rel) for rel in files[:60]]
    return out


def index_section(company: dict) -> str:
    """'# Context you can open': the owner's context files and the key documents, one line each with a summary,
    at most 1,500 characters. Empty when there is nothing to list."""
    pinned = {"global:" + p for p in _pinned(None)} | {"context:" + p for p in _pinned(company)}
    lib = []
    for ref, p in _library(company):
        summary = summarize(p.name, _head_text(p) or "")
        lib.append(f"- {ref}" + (f": {summary}" if summary else "")
                   + (" (always included above)" if ref in pinned else ""))
    docs = []
    root = workspace.root(company)
    for path in KEY_DOCS:
        p = root / path
        if p.is_file():
            summary = summarize(path, _head_text(p) or "")
            docs.append(f"- {path}" + (f": {summary}" if summary else ""))
    if not lib and not docs:
        return ""
    head = "# Context you can open\nRead any of these with read_file <path> (outline=true lists its headings first)."
    tail = ('Find anything else with search("your words"): it covers files, the owner\'s context, memory, tasks, '
            "emails, chat, decisions, past sessions and facts.")
    budget = INDEX_CAP - len(head) - len(tail) - 2
    out, used = [head], 0
    for label, entries in (("The owner's context files (read-only):", lib), ("Key documents:", docs)):
        if not entries or used + len(label) + 1 > budget - 40:
            continue
        out.append(label)
        used += len(label) + 1
        for i, line in enumerate(entries):
            line = _short(line, 160)
            if used + len(line) + 1 > budget - 70:
                rest = len(entries) - i
                note = (f"- ... and {rest} more (list_files global: or list_files context:)" if entries is lib
                        else f"- ... and {rest} more")
                out.append(note)
                used += len(note) + 1
                break
            out.append(line)
            used += len(line) + 1
    out.append(tail)
    text = "\n".join(out)
    return text if len(text) <= INDEX_CAP else text[:INDEX_CAP - 3] + "..."


# ---------------------------------------------------------------- keeping sessions small (used by agents._trim)

def _arg(args: dict, *names: str) -> str:
    for n in names:
        v = (args or {}).get(n)
        if v not in (None, ""):
            return str(v)
    return ""


def trim_reference(name: str, args: dict, content: str) -> str:
    """One line that replaces an old, long tool result and keeps what it referred to, so the agent can fetch it
    again. For read_file it also gives a call with different arguments, so the loop guard (which stops exact
    repeats) does not refuse the re-read."""
    a = args or {}
    name = name or "tool"
    again = ""
    if name == "read_file":
        path = _arg(a, "path", "file", "filename", "file_path")
        m = re.match(r"^\S+: lines (\d+)-(\d+) of \d+", content or "")
        if m:
            what = f"read_file {path} lines {m.group(1)}-{m.group(2)}"
            if not (a.get("start_line") and a.get("end_line")):
                again = f": read_file {path} start_line={m.group(1)} end_line={m.group(2)}"
        elif a.get("outline"):
            what = f"read_file {path} outline"
        else:
            off = _arg(a, "offset")
            what = f"read_file {path}" + (f" offset={off}" if off else "")
    elif name in ("search", "recall", "web_search"):
        what = f'{name} "{_short(_arg(a, "query", "q", "text", "term"), 80)}"'
        if _arg(a, "scope"):
            what += f" scope={_arg(a, 'scope')}"
    elif name == "grep":
        what = f'grep "{_short(_arg(a, "pattern", "query", "text"), 60)}" in {_arg(a, "path") or "."}'
    elif name in ("fetch_url", "http_request"):
        what = f"{name} {_short(_arg(a, 'url', 'link', 'id'), 120)}"
    elif name == "list_files":
        what = f"list_files {_arg(a, 'path') or '.'}"
    elif _arg(a, "path", "file", "file_path"):
        what = f"{name} {_arg(a, 'path', 'file', 'file_path')}"
    else:
        compact = json.dumps({k: (_short(v, 40) if isinstance(v, str) else v) for k, v in a.items()},
                             ensure_ascii=False, default=str)
        what = f"{name}({_short(compact, 100)})"
    return f"[{what}, shortened to save room; run it again if you need it{again}]"


# ---------------------------------------------------------------- the owner's context library (dashboard API)

def _lib_base(company: dict | None) -> Path:
    return global_dir() if company is None else company_dir(company)


def _lib_path(company: dict | None, path: str) -> Path:
    rel = (path or "").strip()
    for pre in ("global:", "context:"):
        if rel.lower().startswith(pre):
            rel = rel[len(pre):]
    if any(part.startswith(".") and part not in (".", "..") for part in rel.replace("\\", "/").split("/")):
        raise HTTPError(400, "File and folder names cannot start with a dot.")
    try:
        return _inside(_lib_base(company), rel)
    except workspace.PathError as e:
        raise HTTPError(400, str(e)) from None


def _pinned_chars(company: dict | None, pinned: list[str]) -> int:
    base, total = _lib_base(company), 0
    for rel in pinned:
        p = base / rel
        try:
            if p.is_file() and _ext(p.name) in LIB_FORMATS:
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _lib_list(company: dict | None) -> dict:
    base = _lib_base(company)
    cid = 0 if company is None else company["id"]
    prefix = "global:" if company is None else "context:"
    pinned = _pinned(company)
    states = _states(cid, "context") if _fts_ok() else {}
    files = []
    for rel, _ap, size, mtime in sorted(_walk(base), key=lambda x: x[0].lower()):
        text_ok = _ext(rel) in LIB_FORMATS
        st = states.get(prefix + rel)
        item = {"path": rel, "ref": prefix + rel, "size": size, "modified": mtime / 1e9, "pinned": rel in pinned,
                "text": text_ok, "indexed": bool(text_ok and _sig_of(st) == f"{size}:{mtime}"),
                "summary": _parse(st)[2] if st else ""}
        if not text_ok:
            item["note"] = "Not searchable: convert it to text (" + ", ".join(LIB_FORMATS) + ")."
        files.append(item)
    return {"files": files, "pinned": pinned, "pinned_chars": _pinned_chars(company, pinned),
            "pinned_cap": PINNED_CAP, "formats": list(LIB_FORMATS), "max_bytes": LIB_MAX_BYTES,
            "folder": str(base), "fts": _fts_ok()}


def _lib_get(company: dict | None, h) -> dict:
    path = (getattr(h, "query", {}) or {}).get("path", [""])[0]
    p = _lib_path(company, path)
    if not p.is_file():
        raise HTTPError(404, "File not found")
    rel = _rel_to(_lib_base(company), p)
    raw = p.read_bytes()[:MAX_FILE_BYTES]
    out = {"path": rel, "size": p.stat().st_size, "pinned": rel in _pinned(company),
           "text": _ext(rel) in LIB_FORMATS}
    if b"\x00" in raw[:4000]:
        return {**out, "binary": True}
    return {**out, "binary": False, "content": raw.decode("utf-8", "replace")}


def _library_changed(company: dict | None, rel: str, verb: str) -> None:
    if company is None:
        reindex_global()
        bus.notify("context")
    else:
        c = db.get_company(company["id"]) or company
        reindex(c)
        bus.emit(c["id"], "file", f"{verb} context/{rel}", None, {"path": f"context/{rel}", "by": "owner"})


def _lib_put(company: dict | None, body) -> dict:
    b = body or {}
    content = b.get("content", "")
    if not isinstance(content, str):
        raise HTTPError(400, "content must be text.")
    p = _lib_path(company, str(b.get("path") or ""))
    base = _lib_base(company)
    if p == base.resolve() or p.is_dir() or not _rel_to(base, p):
        raise HTTPError(400, "Pick a file name, for example notes.md")
    if _ext(p.name) not in LIB_FORMATS:
        raise HTTPError(400, "Only text files go in the context library (" + ", ".join(LIB_FORMATS) +
                        "). Convert other files to text first.")
    if len(content.encode("utf-8")) > LIB_MAX_BYTES:
        raise HTTPError(400, "That file is too big: 500 KB at most. Split it, or keep only what agents need.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="")
    rel = _rel_to(base, p)
    _library_changed(company, rel, "saved")
    return {"ok": True, "path": rel, "size": p.stat().st_size}


def _lib_delete(company: dict | None, body) -> dict:
    p = _lib_path(company, str((body or {}).get("path") or ""))
    if not p.is_file():
        raise HTTPError(404, "File not found")
    rel = _rel_to(_lib_base(company), p)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if company is None:
        dest = config.TRASH_DIR / "context" / stamp / rel
    else:
        dest = workspace.root(company) / ".trash" / stamp / "context" / rel
    n = 2
    while dest.exists():   # two deletes in the same second must not overwrite each other
        dest = dest.with_name(f"{Path(rel).stem}-{n}{Path(rel).suffix}")
        n += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), str(dest))
    pinned = _pinned(company)
    if rel in pinned:
        _save_pinned(company, [x for x in pinned if x != rel])
    _library_changed(company, rel, "deleted")
    return {"ok": True, "path": rel, "trash": str(dest)}


def _save_pinned(company: dict | None, paths: list[str]) -> None:
    if company is None:
        db.set_settings({"pinned_context": paths})
        return
    c = db.get_company(company["id"])
    cs = db.company_settings(c)
    cs["pinned_context"] = paths
    db.update("companies", c["id"], settings=json.dumps(cs))
    bus.notify("company", company_id=c["id"])


def _lib_pin(company: dict | None, body) -> dict:
    paths = (body or {}).get("paths")
    if not isinstance(paths, list):
        raise HTTPError(400, 'Send {"paths": ["notes.md", ...]}.')
    base = _lib_base(company)
    keep, ignored = [], []
    for item in paths[:40]:
        try:
            p = _lib_path(company, str(item))
        except HTTPError:
            ignored.append(item)
            continue
        rel = _rel_to(base, p)
        if rel and p.is_file() and _ext(p.name) in LIB_FORMATS:
            if rel not in keep:
                keep.append(rel)
        else:
            ignored.append(item)
    _save_pinned(company, keep)
    return {"pinned": keep, "ignored": ignored, "pinned_chars": _pinned_chars(company, keep),
            "pinned_cap": PINNED_CAP}


@route("GET", "/api/context")
def api_context(h, m, body):
    reindex_global()
    return _lib_list(None)


@route("GET", "/api/context/file")
def api_context_file(h, m, body):
    return _lib_get(None, h)


@route("PUT", "/api/context/file")
def api_context_file_put(h, m, body):
    return _lib_put(None, body)


@route("DELETE", "/api/context/file")
def api_context_file_delete(h, m, body):
    return _lib_delete(None, body)


@route("PUT", "/api/context/pinned")
def api_context_pinned(h, m, body):
    return _lib_pin(None, body)


@route("GET", r"/api/companies/(\d+)/context")
def api_company_context(h, m, body):
    c = company_or_404(m.group(1))
    reindex(c)
    return _lib_list(c)


@route("GET", r"/api/companies/(\d+)/context/file")
def api_company_context_file(h, m, body):
    return _lib_get(company_or_404(m.group(1)), h)


@route("PUT", r"/api/companies/(\d+)/context/file")
def api_company_context_file_put(h, m, body):
    return _lib_put(company_or_404(m.group(1)), body)


@route("DELETE", r"/api/companies/(\d+)/context/file")
def api_company_context_file_delete(h, m, body):
    return _lib_delete(company_or_404(m.group(1)), body)


@route("PUT", r"/api/companies/(\d+)/context/pinned")
def api_company_context_pinned(h, m, body):
    return _lib_pin(company_or_404(m.group(1)), body)


@route("GET", r"/api/companies/(\d+)/search")
def api_company_search(h, m, body):
    c = company_or_404(m.group(1))
    qs = getattr(h, "query", {}) or {}
    q = (qs.get("q", [""])[0] or "").strip()
    scope = (qs.get("scope", ["all"])[0] or "all").strip()
    limit = max(1, min(to_int(qs.get("limit", [10])[0], 10), 20))
    try:
        parse_scope(scope)
    except ValueError as e:
        raise HTTPError(400, str(e)) from None
    if not q:
        return {"query": "", "scope": scope, "fts": _fts_ok(), "results": []}
    reindex(c)
    rows = search_rows(c, q, scope, limit)
    keys = ("source", "ref", "path", "line", "heading", "snippet", "title", "id")
    return {"query": q, "scope": scope, "fts": _fts_ok(), "results": [{k: r.get(k) for k in keys} for r in rows]}


@route("GET", "/api/index/status")
def api_index_status(h, m, body):
    return index_status()
