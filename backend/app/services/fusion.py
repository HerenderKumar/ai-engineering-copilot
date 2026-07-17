"""
Reciprocal Rank Fusion (Phase 1, locked decision #6).

The problem: dense (vector) search and sparse (BM25) search return ranked
lists whose SCORES live on incomparable scales — cosine similarity vs BM25
weights. The old code "fused" them by union + dedup, throwing every rank away.

RRF fixes this using only rank positions:

    score(doc) = Σ over lists containing doc of  1 / (k + rank)

k=60 (from the original RRF paper) damps the head so one list can't dominate;
a document ranked #1 and #3 in two lists beats one ranked #1 in a single
list. Rank-only fusion is robust: no score calibration, works for any number
of lists — we fuse dense×(sub-queries × spaces) + sparse×(sub-queries) in one
call.
"""

from collections import defaultdict
from typing import Any, Dict, Hashable, List, Sequence


def rrf_fuse(ranked_lists: Sequence[Sequence[Hashable]], k: int = 60) -> List[Hashable]:
    """
    Fuse N ranked lists of ids into one list, best first.
    Deterministic: ties break by id to keep runs reproducible (a graph/eval
    correctness requirement — same inputs, same output, every time).
    """
    scores: Dict[Hashable, float] = defaultdict(float)
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] += 1.0 / (k + rank)
    return sorted(scores, key=lambda d: (-scores[d], str(d)))


def rrf_fuse_with_scores(ranked_lists: Sequence[Sequence[Hashable]],
                         k: int = 60) -> List[Dict[str, Any]]:
    """Same fusion, but returns [{'id': ..., 'rrf_score': ...}] for logging/eval."""
    scores: Dict[Hashable, float] = defaultdict(float)
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] += 1.0 / (k + rank)
    ordered = sorted(scores, key=lambda d: (-scores[d], str(d)))
    return [{"id": d, "rrf_score": round(scores[d], 6)} for d in ordered]
