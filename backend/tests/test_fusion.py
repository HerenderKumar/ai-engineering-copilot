"""RRF: verify the math and the deterministic tie-break."""

from app.services.fusion import rrf_fuse, rrf_fuse_with_scores


def test_doc_in_both_lists_wins():
    # b appears in both lists → highest fused score.
    assert rrf_fuse([["a", "b"], ["b", "c"]], k=60)[0] == "b"


def test_symmetric_tie_breaks_deterministically():
    # a and b get identical scores (rank 0 + rank 1 each) → tie broken by id.
    assert rrf_fuse([["a", "b", "c"], ["b", "a"]], k=60) == ["a", "b", "c"]


def test_scores_exposed():
    scored = rrf_fuse_with_scores([["a"], ["a", "b"]], k=60)
    assert scored[0]["id"] == "a"
    # scores are rounded to 6 decimals for stable logging
    assert abs(scored[0]["rrf_score"] - (2 / 60)) < 1e-5
    assert abs(scored[1]["rrf_score"] - (1 / 61)) < 1e-5


def test_empty_input():
    assert rrf_fuse([]) == []
