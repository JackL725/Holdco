"""Embeddings for memory (Phase 3, docs/phases/phase-3-verify.md section 7). Phase 0.5 already embeds every indexed
chunk, memory included, with the owner's embedding model (context.embed_texts, table search_vectors) and recall
already ranks by meaning through context.search; this module adds what Phase 3 needs on top: cosine similarity and
the nearest existing memory for the small model's duplicate check in tools.remember.
"""
from __future__ import annotations

import math


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def nearest_memory(company: dict, text: str) -> tuple[dict | None, float]:
    """The company's memory row closest in meaning to text and its similarity, or (None, 0.0) without an embedding
    model. Scaffold: (None, 0.0)."""
    return None, 0.0
