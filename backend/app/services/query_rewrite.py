"""
Query rewrite (Phase 1) — expand one question into 2-3 search-friendly variants.

Why: a developer asks "how do we avoid re-embedding an unchanged repo?" but
the code says `_compute_sha256` and `file_hashes`. One phrasing rarely hits
every index well: dense search likes natural sentences, BM25 likes exact
identifiers. Firing all variants and RRF-fusing the results lifts recall.

This implementation is deliberately HEURISTIC (deterministic, zero-latency,
free, unit-testable):
  variant 1: the original query (always kept — never make things worse)
  variant 2: identifier-style — camelCase/snake_case tokens split and joined,
             so "getUserTier" also searches "get user tier" (and vice versa)
  variant 3: keyword skeleton — stopwords stripped, code nouns kept

An LLM rewriter (better paraphrases, adds domain synonyms) can slot behind
the same function later; it costs a model call per query, so measure with
the eval harness before adopting it (the phase-gating rule).
"""

import re
from typing import List

_STOPWORDS = {
    "a", "an", "and", "are", "can", "could", "do", "does", "for", "from",
    "how", "i", "in", "is", "it", "of", "on", "or", "our", "should", "that",
    "the", "this", "to", "we", "what", "when", "where", "which", "who",
    "why", "with", "you", "your",
}

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def _split_identifier(token: str) -> List[str]:
    """getUserTier -> [get, user, tier]; file_hashes -> [file, hashes]"""
    parts: List[str] = []
    for piece in token.replace(".", " ").replace("_", " ").split():
        parts.extend(_CAMEL_RE.sub(" ", piece).split())
    return [p.lower() for p in parts if p]


def rewrite_query(query: str, max_variants: int = 3) -> List[str]:
    """Return 1-3 deduplicated variants, original first."""
    query = query.strip()
    if not query:
        return []
    variants = [query]

    tokens = _TOKEN_RE.findall(query)

    # Variant 2: identifier expansion — split code-ish tokens into words.
    codeish = [t for t in tokens if "_" in t or "." in t or _CAMEL_RE.search(t)]
    if codeish:
        expanded = query
        for t in codeish:
            expanded = expanded.replace(t, " ".join(_split_identifier(t)))
        if expanded.lower() != query.lower():
            variants.append(expanded)

    # Variant 3: keyword skeleton — drop stopwords/punctuation noise.
    keywords = [t for t in tokens if t.lower() not in _STOPWORDS]
    skeleton = " ".join(keywords)
    if skeleton and skeleton.lower() != query.lower():
        variants.append(skeleton)

    # Dedup case-insensitively, preserve order, cap.
    seen, out = set(), []
    for v in variants:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out[:max_variants]
