"""Embeddings for memory (Phase 3, docs/phases/phase-3-verify.md section 7). Phase 0.5 already embeds every indexed
chunk, memory included, with the owner's embedding model (context.embed_texts, table search_vectors) and recall
already ranks by meaning through context.search; this module adds what Phase 3 needs on top: cosine similarity and
the nearest existing memory for the small model's duplicate check in tools.remember.
"""
from __future__ import annotations

import json
import math
import time

from . import db, llm

DUPLICATE_SIMILARITY = 0.92   # above this, the small model is asked whether two notes state the same fact
RECENT_UNINDEXED = 20         # newest notes not embedded yet (saved in this session) that are embedded on the spot
EMBED_TIMEOUT = 10            # seconds, like a search query's embedding (context._vector_rank)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _chunk_text(kind: str, pinned, created_at: str, content: str) -> str:
    """A note as Phase 0.5 embeds it (context._memory_doc, then embed_pending): kind, date, content."""
    title = (kind or "note") + (" (pinned)" if pinned else "")
    return f"{title}\n{(created_at or '')[:10]}\n{content or ''}"[:2000]


def nearest_memory(company: dict, text: str, kind: str = "note") -> tuple[dict | None, float]:
    """The company's memory row closest in meaning to text and its cosine similarity, or (None, 0.0) without an
    embedding model or on any failure. The vectors are Phase 0.5's (search_vectors, memory scope); the newest notes
    that have none yet, such as one saved earlier in this session, are embedded with the query."""
    from . import context   # imported here: context imports tools, which imports this module
    s = db.get_settings()
    model = str(s.get("embedding_model") or "").strip()
    if not model or not (text or "").strip() or time.time() < context._embed_down_until:
        return None, 0.0   # no model, or embedding failed a moment ago and is paused (Phase 0.5)
    cid = company["id"]
    try:
        stored: dict[int, list[float]] = {}
        try:
            for r in db.q("SELECT s.ref, v.vector FROM search_vectors v JOIN search_index s ON s.rowid = v.rowid "
                          "WHERE v.model=? AND s.company_id=? AND s.source='memory'", (model, cid)):
                if str(r["ref"]).isdigit():
                    stored[int(r["ref"])] = json.loads(r["vector"])
        except Exception:   # no FTS5 index on this Python: only the newest notes are compared
            stored = {}
        fresh = [m for m in db.q("SELECT id, kind, pinned, content, created_at FROM memory WHERE company_id=? "
                                 "ORDER BY id DESC LIMIT ?", (cid, RECENT_UNINDEXED)) if m["id"] not in stored]
        texts = [_chunk_text(kind, False, db.now(), text)]
        texts += [_chunk_text(m["kind"], m["pinned"], m["created_at"], m["content"]) for m in fresh]
        try:
            vecs = context.embed_texts(texts, s, timeout=EMBED_TIMEOUT)
        except Exception:   # pause embedding like a failed search does, so the next notes are not slowed down
            context._embed_down_until = time.time() + context.EMBED_PAUSE
            raise
        query = vecs[0]
        stored.update({m["id"]: v for m, v in zip(fresh, vecs[1:])})
        best_id, best = None, 0.0
        for mid, v in stored.items():
            sim = cosine(query, v)
            if sim > best:
                best_id, best = mid, sim
        row = db.one("SELECT * FROM memory WHERE id=? AND company_id=?", (best_id, cid)) if best_id else None
        return (row, round(best, 4)) if row else (None, 0.0)
    except Exception:   # a duplicate check must never stop a note from being saved
        return None, 0.0


SAME_FACT_SYSTEM = ("You compare two short notes from a company's memory. They state the same fact when they say the "
                    "same thing, even in other words. Different numbers, names, dates or places make them different "
                    'facts. Reply with JSON only: {"same": true} or {"same": false}.')


def same_fact(a: str, b: str, settings: dict | None = None) -> bool:
    """The small model's answer to "do these two notes state the same fact?". Raises LLMError when it cannot be
    asked or its answer is unclear."""
    out = llm.run_job("triage", f"Note A: {a.strip()[:1500]}\n\nNote B: {b.strip()[:1500]}", SAME_FACT_SYSTEM,
                      settings, json_output=True)
    data = llm.extract_json(out)
    if isinstance(data, dict) and isinstance(data.get("same"), bool):
        return data["same"]
    word = (out or "").strip().strip('."\'`').lower()
    if word in ("yes", "true", "same"):
        return True
    if word in ("no", "false", "different"):
        return False
    raise llm.LLMError(f"unclear answer to the same-fact question: {out[:80]!r}")


def duplicate_memory(company: dict, text: str, kind: str = "note") -> int | None:
    """The id of a note that already states what text states, or None. Needs both the embedding model (the nearest
    note, above DUPLICATE_SIMILARITY) and the small model (asked "same fact?"); without either, or on any failure,
    None, so remember saves the note as before Phase 3."""
    s = db.get_settings()
    if not llm.small_model(s):
        return None
    row, sim = nearest_memory(company, text, kind)
    if not row or sim <= DUPLICATE_SIMILARITY:
        return None
    try:
        return int(row["id"]) if same_fact(row["content"] or "", text, s) else None
    except Exception:   # LLMError (timeout, server down, unclear answer) or anything else: save it
        return None
