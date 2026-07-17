"""Metrics are the ruler everything else is measured with — verify them
against hand-computed values before trusting any eval number."""

import math

from app.eval.metrics import aggregate, mrr, ndcg_at_k, recall_at_k


def test_recall_at_k():
    assert recall_at_k(["a", "b", "c", "d"], ["b", "e"], k=2) == 0.5
    assert recall_at_k(["a", "b"], ["a", "b"], k=2) == 1.0
    assert recall_at_k(["x"], ["a"], k=5) == 0.0
    assert recall_at_k([], ["a"], k=5) == 0.0
    assert recall_at_k(["a"], [], k=5) == 0.0  # no labels → 0, not crash


def test_mrr():
    assert mrr(["a", "b", "c"], ["b"]) == 0.5
    assert mrr(["b", "a"], ["b"]) == 1.0
    assert mrr(["x", "y"], ["z"]) == 0.0


def test_ndcg_hand_computed():
    # ranked [a,b,c,d], relevant {b,e}, k=2:
    # DCG = 1/log2(3) (b at rank 2); IDCG = 1/log2(2) + 1/log2(3)
    expected = (1 / math.log2(3)) / (1 / math.log2(2) + 1 / math.log2(3))
    assert abs(ndcg_at_k(["a", "b", "c", "d"], ["b", "e"], k=2) - expected) < 1e-9
    assert ndcg_at_k(["a", "b"], ["a", "b"], k=2) == 1.0  # perfect ordering


def test_aggregate_means():
    rows = [{"mrr": 1.0, "recall@5": 0.5}, {"mrr": 0.0, "recall@5": 1.0}]
    agg = aggregate(rows)
    assert agg == {"mrr": 0.5, "recall@5": 0.75}
