"""
Retrieval pipeline (Phases 1 + 3) — hybrid, multi-source GraphRAG.

The full pipeline (handoff §6.6), each stage optional-by-config and each
stage able to fail WITHOUT killing the query:

  query
    → rewrite            (2-3 sub-query variants; heuristic, Phase 1)
    → dense search       (per variant × per space: 'code' + 'okf')   ┐
    → sparse search      (per variant, BM25/FTS5)                    ├ ranked lists
    → RRF fusion         (rank-only, replaces the old union-dedup)   ┘
    → graph expansion    (top hits → graph nodes via chunk_id → 1-2 hop
                          callers/callees/definitions → their chunks join the
                          candidate pool; Phase 3 — this is what finds the
                          wired-together neighbor that shares NO words with
                          the query)
    → cross-encoder rerank (precision pass over ≤ RERANK_CANDIDATE_CAP)
    → assemble           (top_k chunks, each carrying graph_context strings
                          for the prompt builder's structural preamble)

Graceful degradation (logged, never silent):
  embedder down → sparse-only        graph down   → plain hybrid RAG
  rerank down   → RRF order          cache down   → recompute

Every stage logs duration + candidate counts via stage_timer — the exact
numbers you chart as p50/p95 per stage in ops dashboards (handoff §9).
"""

import logging
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logging import log_event, stage_timer
from app.services.cache import cache
from app.services.fusion import rrf_fuse
from app.services.query_rewrite import rewrite_query
from app.services.vector_store import vector_metadata_store

logger = logging.getLogger(__name__)


class RetrievalService:

    @staticmethod
    def retrieve_context(repo_id: str, query: str,
                         top_k: int = settings.DEFAULT_TOP_K) -> List[Dict[str, Any]]:
        if not query.strip():
            return []

        # 0. Query cache (normalized-query → results).
        cache_key = f"qry:{repo_id}:{top_k}:{' '.join(query.lower().split())}"
        cached = cache.get_json(cache_key)
        if cached is not None:
            log_event(logger, "retrieval.cache_hit", repo_id=repo_id)
            return cached

        fallbacks: List[str] = []

        # 1. Rewrite into sub-queries.
        variants = rewrite_query(query) if settings.QUERY_REWRITE_ENABLED else [query]

        candidate_pool = max(top_k * 2, 20)
        ranked_lists: List[List[int]] = []
        docs_by_id: Dict[int, Dict[str, Any]] = {}

        # 2. Dense retrieval — per variant, per embedding space.
        with stage_timer(logger, "retrieval.dense", repo_id=repo_id) as ctx:
            try:
                from app.services.embeddings import generate_embeddings
                embeddings = generate_embeddings(variants)
                spaces = ["code", "okf"]
                for i, _variant in enumerate(variants):
                    for space in spaces:
                        results = vector_metadata_store.search_dense(
                            repo_id, embeddings[i], top_k=candidate_pool, space=space)
                        if results:
                            ranked_lists.append([d["faiss_id"] for d in results])
                            for d in results:
                                docs_by_id.setdefault(d["faiss_id"], d)
                ctx["lists"] = len(ranked_lists)
            except Exception as e:  # embedder down → sparse-only
                fallbacks.append("dense_unavailable")
                log_event(logger, "retrieval.fallback", level=logging.WARNING,
                          repo_id=repo_id, stage="dense", reason=str(e))

        # 3. Sparse retrieval (BM25) — per variant.
        with stage_timer(logger, "retrieval.sparse", repo_id=repo_id) as ctx:
            sparse_lists = 0
            for variant in variants:
                try:
                    results = vector_metadata_store.search_sparse(
                        repo_id, variant, top_k=candidate_pool)
                except Exception as e:
                    fallbacks.append("sparse_error")
                    log_event(logger, "retrieval.fallback", level=logging.WARNING,
                              repo_id=repo_id, stage="sparse", reason=str(e))
                    results = []
                if results:
                    sparse_lists += 1
                    ranked_lists.append([d["faiss_id"] for d in results])
                    for d in results:
                        docs_by_id.setdefault(d["faiss_id"], d)
            ctx["lists"] = sparse_lists

        if not ranked_lists:
            log_event(logger, "retrieval.empty", level=logging.WARNING,
                      repo_id=repo_id, fallbacks=fallbacks)
            return []

        # 4. RRF fusion (replaces the old union-dedup).
        fused_ids = rrf_fuse(ranked_lists, k=settings.RRF_K)
        fused_docs = [docs_by_id[i] for i in fused_ids if i in docs_by_id]

        # 5. Graph expansion (Phase 3).
        if settings.GRAPH_ENABLED and settings.GRAPH_EXPANSION_ENABLED:
            try:
                with stage_timer(logger, "retrieval.graph_expand", repo_id=repo_id) as ctx:
                    added = RetrievalService._graph_expand(
                        repo_id, fused_docs[:top_k], docs_by_id)
                    fused_docs.extend(added)
                    ctx["added"] = len(added)
            except Exception as e:  # graph down → plain hybrid RAG
                fallbacks.append("graph_unavailable")
                log_event(logger, "retrieval.fallback", level=logging.WARNING,
                          repo_id=repo_id, stage="graph_expand", reason=str(e))

        # 6. Cross-encoder rerank (precision pass).
        pool = fused_docs[: settings.RERANK_CANDIDATE_CAP]
        final: List[Dict[str, Any]]
        if len(pool) > top_k:
            try:
                with stage_timer(logger, "retrieval.rerank", repo_id=repo_id) as ctx:
                    from app.services.reranker import reranker_service
                    final = reranker_service.rerank(query, pool, top_k=top_k)
                    ctx["pool"] = len(pool)
            except Exception as e:  # rerank down → RRF order
                fallbacks.append("rerank_unavailable")
                log_event(logger, "retrieval.fallback", level=logging.WARNING,
                          repo_id=repo_id, stage="rerank", reason=str(e))
                final = pool[:top_k]
        else:
            final = pool[:top_k]

        log_event(logger, "retrieval.done", repo_id=repo_id,
                  candidates=len(fused_docs), returned=len(final),
                  fallbacks=fallbacks or None)

        cache.set_json(cache_key, final, settings.QUERY_CACHE_TTL)
        return final

    # ------------------------------------------------------------------ graph --

    @staticmethod
    def _graph_expand(repo_id: str, seed_docs: List[Dict[str, Any]],
                      docs_by_id: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Map the top fused chunks to graph nodes (chunk_id join), pull their
        1-2 hop neighbors over CALLS/IMPORTS/INHERITS/CONTAINS, fetch the
        neighbors' chunks as NEW candidates, and attach human-readable
        `graph_context` lines to the seed chunks for the prompt's preamble.
        """
        from app.services.graph_store import graph_store

        seed_ids = [d["faiss_id"] for d in seed_docs]
        seed_nodes = graph_store.get_nodes_by_chunk_ids(repo_id, seed_ids)
        if not seed_nodes:
            return []

        nodes_by_id = {n["id"]: n for n in seed_nodes}
        neighbor_nodes, edges = graph_store.neighbors(
            repo_id,
            [n["id"] for n in seed_nodes],
            kinds=["CALLS", "IMPORTS", "INHERITS", "CONTAINS"],
            depth=settings.GRAPH_EXPANSION_HOPS,
            direction="both",
            max_nodes=settings.GRAPH_MAX_NEIGHBORS * max(1, len(seed_docs)),
        )
        for n in neighbor_nodes:
            nodes_by_id[n["id"]] = n

        # Human-readable relationship lines, attached to seed chunks.
        chunk_context: Dict[int, List[str]] = {}
        for e in edges:
            src = nodes_by_id.get(e["src_id"])
            dst = nodes_by_id.get(e["dst_id"])
            if not src or not dst or e["kind"] == "CONTAINS":
                continue
            verb = {"CALLS": "calls", "IMPORTS": "imports", "INHERITS": "inherits from"}[e["kind"]]
            qualifier = " (low confidence)" if e["confidence"] < 0.5 else ""
            line = f"{src['qualified_name']} {verb} {dst['qualified_name']}{qualifier}"
            for node in (src, dst):
                cid = node.get("chunk_id")
                if cid in docs_by_id:
                    chunk_context.setdefault(cid, [])
                    if line not in chunk_context[cid]:
                        chunk_context[cid].append(line)
        for cid, lines in chunk_context.items():
            docs_by_id[cid]["graph_context"] = lines[:8]  # compact, not a dump

        # Neighbors whose chunks aren't in the pool yet → new candidates.
        new_chunk_ids = []
        for n in neighbor_nodes:
            cid = n.get("chunk_id")
            if cid is not None and cid not in docs_by_id:
                new_chunk_ids.append(cid)
        added = vector_metadata_store.fetch_chunks_by_ids(
            repo_id, list(dict.fromkeys(new_chunk_ids)))
        for d in added:
            d["via_graph"] = True
            docs_by_id[d["faiss_id"]] = d
        return added


retrieve_context = RetrievalService.retrieve_context
