"""
Graph layout service (Phase 3) — precompute 3D positions + communities at
INDEX time, so the browser never runs physics.

This is performance lever #1 for the 3D UI (handoff §6.9): a force simulation
over thousands of nodes at 60 fps melts laptops; instead we run the layout
once per ingestion on the server, store x/y/z on each node, and the frontend
renders fixed positions (cooldownTicks=0).

Two engines, best-first with graceful degradation:
  * python-igraph  (C core) — Fruchterman-Reingold in 3D, fast at 10^4+ nodes
  * networkx      (pure python fallback) — spring_layout(dim=3), fine for
    small/medium graphs; seeded for determinism

Communities via Louvain (networkx built-in) — these become the "galaxies" the
UI colors by cluster and can aggregate at overview zoom (LOD lever #2).
"""

import logging
from typing import Dict, Tuple

from app.core.logging import log_event, stage_timer
from app.services.graph_store import GraphStore, graph_store

logger = logging.getLogger(__name__)

_SCALE = 600.0  # spread positions over a comfortable Three.js scene size


def compute_layout(repo_id: str, store: GraphStore = graph_store) -> int:
    """Compute + persist (x, y, z, cluster) for every node. Returns node count.
    Any failure logs and returns 0 — layout is cosmetic, never fatal."""
    try:
        with stage_timer(logger, "graph.layout", repo_id=repo_id) as ctx:
            nodes = store.all_nodes(repo_id)
            edges = store.all_edges(repo_id)
            if not nodes:
                return 0
            ids = [n["id"] for n in nodes]
            id_set = set(ids)
            edge_pairs = [(e["src_id"], e["dst_id"]) for e in edges
                          if e["src_id"] in id_set and e["dst_id"] in id_set]

            positions = _layout_igraph(ids, edge_pairs) or _layout_networkx(ids, edge_pairs)
            clusters = _communities(ids, edge_pairs)

            coords: Dict[int, Tuple[float, float, float, int]] = {}
            for node_id in ids:
                x, y, z = positions.get(node_id, (0.0, 0.0, 0.0))
                coords[node_id] = (round(x, 2), round(y, 2), round(z, 2),
                                   clusters.get(node_id, 0))
            store.update_layout(repo_id, coords)
            ctx.update(nodes=len(ids), edges=len(edge_pairs),
                       clusters=len(set(clusters.values())))
            return len(ids)
    except Exception as e:
        log_event(logger, "graph.layout_failed", level=logging.ERROR,
                  repo_id=repo_id, error=str(e))
        return 0


def _layout_igraph(ids, edge_pairs):
    """Preferred engine: igraph's C-speed 3D Fruchterman-Reingold."""
    try:
        import igraph as ig
    except ImportError:
        return None
    try:
        index_of = {node_id: i for i, node_id in enumerate(ids)}
        g = ig.Graph(n=len(ids),
                     edges=[(index_of[s], index_of[d]) for s, d in edge_pairs])
        layout = g.layout_fruchterman_reingold_3d(niter=150)
        coords = layout.coords
        # Normalize to scene scale.
        max_abs = max((abs(c) for xyz in coords for c in xyz), default=1.0) or 1.0
        factor = _SCALE / max_abs
        return {node_id: (coords[i][0] * factor, coords[i][1] * factor, coords[i][2] * factor)
                for node_id, i in index_of.items()}
    except Exception as e:
        logger.warning(f"igraph layout failed, falling back to networkx: {e}")
        return None


def _layout_networkx(ids, edge_pairs):
    """Fallback engine: seeded 3D spring layout (deterministic)."""
    import networkx as nx
    g = nx.Graph()
    g.add_nodes_from(ids)
    g.add_edges_from(edge_pairs)
    # Pure-python spring_layout is O(n²) per iteration: 50 iterations on a
    # ~10k-node graph meant 2+ minutes of silent "processing" during ingest.
    # Large graphs trade a little layout polish for a responsive pipeline
    # (python-igraph, when installed, does full quality at C speed instead).
    n = len(ids)
    iterations = 50 if n <= 1000 else 20 if n <= 5000 else 10
    pos = nx.spring_layout(g, dim=3, seed=42, iterations=iterations, scale=_SCALE)
    return {node_id: tuple(float(c) for c in xyz) for node_id, xyz in pos.items()}


def _communities(ids, edge_pairs) -> Dict[int, int]:
    """Louvain communities (seeded). Isolated nodes fall in cluster 0."""
    import networkx as nx
    g = nx.Graph()
    g.add_nodes_from(ids)
    g.add_edges_from(edge_pairs)
    clusters: Dict[int, int] = {}
    try:
        communities = nx.community.louvain_communities(g, seed=42)
    except Exception:
        communities = nx.community.greedy_modularity_communities(g)
    for i, community in enumerate(sorted(communities, key=len, reverse=True)):
        for node_id in community:
            clusters[node_id] = i
    return clusters
