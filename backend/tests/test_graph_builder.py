"""
THE VALIDATION HARNESS (Phase 2 exit criterion, handoff §7.6).

The fixture repo's relationships are hand-labeled below as ground truth.
The builder must achieve precision == recall == 1.0 on the labeled CALLS
subset, produce correct INHERITS/IMPORTS/CONTAINS, be deterministic, avoid
known false positives, and heal in-bound edges after an incremental change.
Gate CI on this file: a resolver regression fails loudly, not silently.
"""

import os

import pytest

pytest.importorskip("tree_sitter_languages",
                    reason="graph building requires the tree-sitter grammars")

from app.services.graph_builder import GraphBuilder
from app.services.graph_store import GraphStore

REPO = "fixture-repo"


def _qual_edges(store, repo_id, kind):
    """Edges as (src_qualified_name, dst_qualified_name) for readable asserts."""
    names = {n["id"]: n["qualified_name"] for n in store.all_nodes(repo_id)}
    return {(names[e["src_id"]], names[e["dst_id"]])
            for e in store.all_edges(repo_id) if e["kind"] == kind}


def _build(store, repo_root):
    files = sorted(f for f in os.listdir(repo_root) if f.endswith(".py"))
    return GraphBuilder(REPO, repo_root, store).build_or_update(files, [])


# ---- Ground truth for the fixture repo (hand-labeled) -----------------------

EXPECTED_CALLS = {
    ("billing.Billing.charge", "users.fetch_user_tier"),  # imported symbol
    ("billing.Billing.charge", "users.Base.save"),        # unique global (self.save)
    ("orders.create", "billing.Billing"),                 # constructor call
    ("orders.create", "billing.Billing.charge"),          # instance method
}
EXPECTED_INHERITS = {("billing.Billing", "users.Base")}
EXPECTED_IMPORTS = {("billing", "users"), ("orders", "billing")}
EXPECTED_CONTAINS = {
    ("users", "users.Base"), ("users.Base", "users.Base.save"),
    ("users", "users.fetch_user_tier"),
    ("billing", "billing.Billing"), ("billing.Billing", "billing.Billing.charge"),
    ("orders", "orders.create"),
}


def test_edges_match_ground_truth(graph_env, fixture_repo):
    stats = _build(graph_env, fixture_repo)
    assert stats["parse_failures"] == 0

    calls = _qual_edges(graph_env, REPO, "CALLS")
    # Precision AND recall == 1.0 on the labeled subset (the CI gate).
    assert calls == EXPECTED_CALLS, f"CALLS mismatch: {calls ^ EXPECTED_CALLS}"
    assert _qual_edges(graph_env, REPO, "INHERITS") == EXPECTED_INHERITS
    assert _qual_edges(graph_env, REPO, "IMPORTS") == EXPECTED_IMPORTS
    assert _qual_edges(graph_env, REPO, "CONTAINS") == EXPECTED_CONTAINS


def test_no_false_positive_for_external_namesake(graph_env, fixture_repo):
    """stripe.PaymentIntent.create() must NOT resolve to orders.create —
    the resolver knows 'stripe' is external and stops."""
    _build(graph_env, fixture_repo)
    calls = _qual_edges(graph_env, REPO, "CALLS")
    assert ("billing.Billing.charge", "orders.create") not in calls


def test_confidence_tiers(graph_env, fixture_repo):
    _build(graph_env, fixture_repo)
    names = {n["qualified_name"]: n["id"] for n in graph_env.all_nodes(REPO)}
    conf = {(e["src_id"], e["dst_id"]): e["confidence"]
            for e in graph_env.all_edges(REPO) if e["kind"] == "CALLS"}
    # imported symbol → 0.90; unique-global (self.save) → 0.60
    assert conf[(names["billing.Billing.charge"], names["users.fetch_user_tier"])] == 0.90
    assert conf[(names["billing.Billing.charge"], names["users.Base.save"])] == 0.60


def test_determinism_same_input_same_graph(graph_env, fixture_repo, tmp_path):
    _build(graph_env, fixture_repo)
    first = (sorted(n["id"] for n in graph_env.all_nodes(REPO)),
             sorted((e["src_id"], e["dst_id"], e["kind"]) for e in graph_env.all_edges(REPO)))
    store2 = GraphStore(base_dir=str(tmp_path / "again"))
    _build(store2, fixture_repo)
    second = (sorted(n["id"] for n in store2.all_nodes(REPO)),
              sorted((e["src_id"], e["dst_id"], e["kind"]) for e in store2.all_edges(REPO)))
    assert first == second  # correctness requirement #5


def test_incremental_reconciliation_heals_inbound_edges(graph_env, fixture_repo):
    """Rename users.fetch_user_tier → the old CALLS edge from billing must
    disappear (not dangle), and billing must be re-resolved automatically."""
    _build(graph_env, fixture_repo)

    users_path = os.path.join(fixture_repo, "users.py")
    with open(users_path, encoding="utf-8") as f:
        source = f.read()
    with open(users_path, "w", encoding="utf-8") as f:
        f.write(source.replace("fetch_user_tier", "lookup_user_tier"))

    stats = GraphBuilder(REPO, fixture_repo, graph_env).build_or_update(["users.py"], [])
    assert stats["files_reresolved"] >= 1  # billing.py re-resolved

    calls = _qual_edges(graph_env, REPO, "CALLS")
    assert ("billing.Billing.charge", "users.fetch_user_tier") not in calls
    # billing.py still imports the OLD name → correctly unresolved, no edge.
    assert ("billing.Billing.charge", "users.lookup_user_tier") not in calls
    # But the self.save() edge was healed against the rebuilt users.py nodes.
    assert ("billing.Billing.charge", "users.Base.save") in calls

    # Global invariant: every edge endpoint exists (no dangling edges, req #3).
    ids = {n["id"] for n in graph_env.all_nodes(REPO)}
    for e in graph_env.all_edges(REPO):
        assert e["src_id"] in ids and e["dst_id"] in ids
