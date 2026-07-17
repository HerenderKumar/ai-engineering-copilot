"""Graph store: stable identity, bounded traversal, reconciliation reporting."""

from app.services.graph_store import GraphStore


def _node(store_cls, repo, qname, file_path, kind="function", **kw):
    return {
        "id": store_cls.stable_node_id(repo, qname, file_path, kw.get("signature", "")),
        "kind": kind, "name": qname.split(".")[-1], "qualified_name": qname,
        "file_path": file_path, "start_line": kw.get("start_line", 1),
        "end_line": kw.get("end_line", 10), "signature": kw.get("signature"),
        "language": "python", "chunk_id": kw.get("chunk_id"),
    }


def test_stable_ids_are_deterministic_and_distinct(graph_env):
    a1 = GraphStore.stable_node_id("r", "m.f", "m.py", "def f()")
    a2 = GraphStore.stable_node_id("r", "m.f", "m.py", "def f()")
    b = GraphStore.stable_node_id("r", "m.g", "m.py", "def g()")
    assert a1 == a2 and a1 != b and a1 > 0  # 63-bit positive


def test_upsert_idempotent(graph_env):
    n = _node(GraphStore, "r", "m.f", "m.py")
    graph_env.upsert_nodes("r", [n])
    graph_env.upsert_nodes("r", [n])  # re-index → REPLACE, not duplicate
    assert len(graph_env.all_nodes("r")) == 1


def test_traversal_is_cycle_safe_and_bounded(graph_env):
    a = _node(GraphStore, "r", "m.a", "m.py")
    b = _node(GraphStore, "r", "m.b", "m.py")
    graph_env.upsert_nodes("r", [a, b])
    graph_env.upsert_edges("r", [
        {"src_id": a["id"], "dst_id": b["id"], "kind": "CALLS", "confidence": 1.0},
        {"src_id": b["id"], "dst_id": a["id"], "kind": "CALLS", "confidence": 1.0},  # cycle!
    ])
    nodes, edges = graph_env.neighbors("r", [a["id"]], depth=4)  # must terminate
    assert {n["id"] for n in nodes} == {b["id"]}
    assert len(edges) == 2


def test_trace_path(graph_env):
    ids = []
    for name in ("a", "b", "c"):
        n = _node(GraphStore, "r", f"m.{name}", "m.py")
        ids.append(n["id"])
        graph_env.upsert_nodes("r", [n])
    graph_env.upsert_edges("r", [
        {"src_id": ids[0], "dst_id": ids[1], "kind": "CALLS", "confidence": 1.0},
        {"src_id": ids[1], "dst_id": ids[2], "kind": "CALLS", "confidence": 1.0},
    ])
    assert graph_env.trace_path("r", ids[0], ids[2]) == ids
    assert graph_env.trace_path("r", ids[2], ids[0]) is None  # directed


def test_delete_files_reports_affected(graph_env):
    callee = _node(GraphStore, "r", "lib.f", "lib.py")
    caller = _node(GraphStore, "r", "app.g", "app.py")
    graph_env.upsert_nodes("r", [callee, caller])
    graph_env.upsert_edges("r", [
        {"src_id": caller["id"], "dst_id": callee["id"], "kind": "CALLS", "confidence": 0.9}])
    deleted_ids, affected = graph_env.delete_files("r", ["lib.py"])
    assert deleted_ids == [callee["id"]]
    assert affected == {"app.py"}          # app.py pointed in → must re-resolve
    assert graph_env.all_edges("r") == []  # no dangling edges survive
