"""
Retrieval quality metrics (Phase 0) — pure functions, no dependencies.

Why these three? They answer different questions about a ranked result list:

  recall@k  — "did the right file appear in the top k at all?"
              (the LLM can only cite what retrieval surfaced)
  MRR       — "how HIGH did the first right answer rank?"
              (1.0 = always first, 0.5 = usually second, ...)
  nDCG@k    — "how good is the whole ordering?" — rewards putting every
              relevant item early, with logarithmic discounting.

Relevance here is binary at file level: a retrieved chunk counts as relevant
if its file_path is in the question's labeled `relevant_files`. File-level
labels are much cheaper to hand-write than chunk-level ones and are the
standard first rung for code-RAG evals.

Every phase of the build is gated on these numbers (handoff §9): Phase 0
establishes the baseline, Phase 1 must beat it, Phase 3's hybrid must beat
pure-RAG. No metric, no merge.
"""

import math
from typing import Dict, List, Sequence


def recall_at_k(ranked: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Fraction of relevant items that appear in the top-k of `ranked`."""
    if not relevant:
        return 0.0
    top = set(ranked[:k])
    hit = sum(1 for r in set(relevant) if r in top)
    return hit / len(set(relevant))


def mrr(ranked: Sequence[str], relevant: Sequence[str]) -> float:
    """Reciprocal rank of the FIRST relevant item (0 if none retrieved)."""
    rel = set(relevant)
    for i, item in enumerate(ranked):
        if item in rel:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Normalized Discounted Cumulative Gain with binary relevance."""
    rel = set(relevant)
    dcg = sum(1.0 / math.log2(i + 2) for i, item in enumerate(ranked[:k]) if item in rel)
    ideal_hits = min(len(rel), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    return dcg / idcg if idcg > 0 else 0.0


def evaluate_ranking(ranked: Sequence[str], relevant: Sequence[str],
                     ks: Sequence[int] = (5, 10, 20)) -> Dict[str, float]:
    """All metrics for one question."""
    out: Dict[str, float] = {"mrr": mrr(ranked, relevant)}
    for k in ks:
        out[f"recall@{k}"] = recall_at_k(ranked, relevant, k)
        out[f"ndcg@{k}"] = ndcg_at_k(ranked, relevant, k)
    return out


def aggregate(per_question: List[Dict[str, float]]) -> Dict[str, float]:
    """Mean of each metric across questions (the number you gate a phase on)."""
    if not per_question:
        return {}
    keys = per_question[0].keys()
    return {k: round(sum(q[k] for q in per_question) / len(per_question), 4) for k in keys}
