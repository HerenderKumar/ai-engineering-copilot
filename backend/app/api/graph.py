"""
Graph API (Phase 3) — feeds the 3D graph UI and any external consumer.

GET /graph/{repo_id}/subgraph
    Bounded subgraph (never a full dump at scale — handoff §6.9), filterable:
      ?center=<node_id>   neighborhood view around one node
      ?file=<path>        all nodes of a file + their neighbors
      ?kinds=CALLS,IMPORTS  edge-kind filter
      ?depth=1..4         hop count
      ?limit=1..2000      node cap
    Response shape is exactly what the frontend renderer consumes:
      { nodes: [{id, label, kind, file, x, y, z, cluster, ...}],
        edges: [{src, dst, kind, confidence}] }
    Positions were precomputed at index time (graph_layout.py) so the browser
    does zero physics.

GET /graph/{repo_id}/stats   node/edge counts + low-confidence count (QA).
GET /graph/{repo_id}/node/{node_id}  one node + immediate neighborhood.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.services.graph_store import EDGE_KINDS, graph_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/graph", tags=["Knowledge Graph"])


def _serialize(subgraph: dict) -> dict:
    return {
        "nodes": [{
            "id": str(n["id"]),  # JS can't hold 63-bit ints exactly → strings
            "label": n["name"],
            "qualified_name": n["qualified_name"],
            "kind": n["kind"],
            "file": n["file_path"],
            "start_line": n["start_line"],
            "end_line": n["end_line"],
            "signature": n["signature"],
            "x": n["x"], "y": n["y"], "z": n["z"],
            "cluster": n["cluster"] if n["cluster"] is not None else 0,
            "chunk_id": n["chunk_id"],
        } for n in subgraph["nodes"]],
        "edges": [{
            "src": str(e["src_id"]),
            "dst": str(e["dst_id"]),
            "kind": e["kind"],
            "confidence": e["confidence"],
        } for e in subgraph["edges"]],
    }


@router.get("/{repo_id}/subgraph")
async def get_subgraph(
    repo_id: str,
    center: Optional[str] = Query(None, description="Node id to center on"),
    file: Optional[str] = Query(None, description="Repo-relative file path"),
    kinds: Optional[str] = Query(None, description="Comma-separated edge kinds"),
    depth: int = Query(2, ge=1, le=4),
    limit: int = Query(500, ge=1, le=2000),
):
    kind_list = None
    if kinds:
        kind_list = [k.strip().upper() for k in kinds.split(",") if k.strip()]
        invalid = set(kind_list) - set(EDGE_KINDS)
        if invalid:
            raise HTTPException(400, f"Unknown edge kinds: {sorted(invalid)}")
    try:
        center_id = int(center) if center else None
    except ValueError:
        raise HTTPException(400, "center must be a numeric node id")
    subgraph = graph_store.subgraph(repo_id, center=center_id, file_path=file,
                                    kinds=kind_list, depth=depth, limit=limit)
    if not subgraph["nodes"]:
        raise HTTPException(404, f"No graph found for repo '{repo_id}' (ingest it first)")
    return _serialize(subgraph)


@router.get("/{repo_id}/stats")
async def get_stats(repo_id: str):
    return graph_store.stats(repo_id)


@router.get("/{repo_id}/node/{node_id}")
async def get_node(repo_id: str, node_id: int, depth: int = Query(1, ge=1, le=3)):
    nodes = graph_store.get_nodes(repo_id, [node_id])
    if not nodes:
        raise HTTPException(404, "Node not found")
    neighbor_nodes, edges = graph_store.neighbors(repo_id, [node_id], depth=depth)
    return _serialize({"nodes": nodes + neighbor_nodes, "edges": edges})
