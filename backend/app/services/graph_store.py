"""
Graph store (Phase 2) — SQLite persistence for the code knowledge graph.

This is the platform's backbone (locked decision #3): a property graph of
code entities (File / Class / Function / Method nodes) and relationships
(CONTAINS / CALLS / IMPORTS / INHERITS edges), one namespace per repo, in a
single SQLite file next to the vector metadata.

Schema per handoff §6.2, plus layout columns (x, y, z, cluster) that the
layout service precomputes at index time so the 3D UI never runs physics in
the browser.

Correctness requirements implemented here (handoff §7):
  #1 STABLE NODE IDENTITY — `stable_node_id()` derives the primary key from
     sha256(repo | qualified_name | file | signature). Deterministic across
     re-indexes; NEVER line numbers or array positions (those shift on every
     edit and would corrupt incremental updates).
  #3 INCREMENTAL RECONCILIATION — `delete_files()` removes a changed file's
     nodes AND both edge directions, and reports which OTHER files had edges
     pointing in, so the builder can re-resolve them (no dangling edges).
  #4 CYCLE-SAFE BOUNDED TRAVERSAL — `neighbors()` / `trace_path()` BFS with a
     visited set, depth cap and node cap (recursion and cyclic imports are
     normal in real code).
  #5 DETERMINISM — every query orders results; same graph in, same rows out.

Every edge carries a `confidence` (0..1). Low-confidence edges are MARKED,
never hidden — the 3D UI renders them dashed/red as a QA surface.
"""

import hashlib
import logging
import os
import sqlite3
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger(__name__)

EDGE_KINDS = ("CONTAINS", "CALLS", "IMPORTS", "INHERITS")


class GraphStore:
    def __init__(self, base_dir: str = settings.DATA_DIR):
        self.metadata_dir = os.path.join(base_dir, "metadata")
        os.makedirs(self.metadata_dir, exist_ok=True)
        self.db_path = os.path.join(self.metadata_dir, "graph.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id INTEGER NOT NULL,
                    repo_id TEXT NOT NULL,
                    kind TEXT NOT NULL,               -- file | class | function | method
                    name TEXT NOT NULL,               -- unqualified, for symbol lookup
                    qualified_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    start_line INTEGER,
                    end_line INTEGER,
                    signature TEXT,
                    language TEXT,
                    chunk_id INTEGER,                 -- join to vector store (faiss_id)
                    x REAL, y REAL, z REAL,           -- precomputed 3D layout
                    cluster INTEGER,                  -- Louvain community
                    PRIMARY KEY (repo_id, id)
                );
                CREATE INDEX IF NOT EXISTS idx_node_file ON nodes(repo_id, file_path);
                CREATE INDEX IF NOT EXISTS idx_node_name ON nodes(repo_id, name);
                CREATE INDEX IF NOT EXISTS idx_node_chunk ON nodes(repo_id, chunk_id);

                CREATE TABLE IF NOT EXISTS edges (
                    repo_id TEXT NOT NULL,
                    src_id INTEGER NOT NULL,
                    dst_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    PRIMARY KEY (repo_id, src_id, dst_id, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_edge_src ON edges(repo_id, src_id, kind);
                CREATE INDEX IF NOT EXISTS idx_edge_dst ON edges(repo_id, dst_id, kind);
            """)
            conn.commit()

    # -- identity (correctness requirement #1) -----------------------------------

    @staticmethod
    def stable_node_id(repo_id: str, qualified_name: str,
                       file_path: str, signature: str = "") -> int:
        """Deterministic 63-bit positive integer id. Same entity → same id on
        every re-index; bad ids would corrupt every incremental update."""
        key = f"{repo_id}|{qualified_name}|{file_path}|{signature}"
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF

    # -- writes --------------------------------------------------------------------

    def upsert_nodes(self, repo_id: str, nodes: List[Dict[str, Any]]) -> None:
        if not nodes:
            return
        with self._connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO nodes
                   (id, repo_id, kind, name, qualified_name, file_path,
                    start_line, end_line, signature, language, chunk_id,
                    x, y, z, cluster)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(n["id"], repo_id, n["kind"], n["name"], n["qualified_name"],
                  n["file_path"], n.get("start_line"), n.get("end_line"),
                  n.get("signature"), n.get("language"), n.get("chunk_id"),
                  n.get("x"), n.get("y"), n.get("z"), n.get("cluster"))
                 for n in nodes])
            conn.commit()

    def upsert_edges(self, repo_id: str, edges: List[Dict[str, Any]]) -> None:
        if not edges:
            return
        with self._connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO edges (repo_id, src_id, dst_id, kind, confidence)
                   VALUES (?, ?, ?, ?, ?)""",
                [(repo_id, e["src_id"], e["dst_id"], e["kind"],
                  float(e.get("confidence", 1.0))) for e in edges])
            conn.commit()

    def delete_files(self, repo_id: str, file_paths: Iterable[str]) -> Tuple[List[int], Set[str]]:
        """
        Remove all nodes of the given files plus every edge touching them.
        Returns (deleted_node_ids, affected_files): the files whose nodes had
        edges INTO the deleted nodes — the builder must re-resolve those files
        or the graph would keep phantom knowledge (correctness req #3).
        """
        file_paths = sorted(set(file_paths))
        if not file_paths:
            return [], set()
        with self._connect() as conn:
            ph = ",".join("?" * len(file_paths))
            ids = [row["id"] for row in conn.execute(
                f"SELECT id FROM nodes WHERE repo_id = ? AND file_path IN ({ph}) ORDER BY id",
                [repo_id] + file_paths).fetchall()]
            affected: Set[str] = set()
            if ids:
                for i in range(0, len(ids), 500):
                    batch = ids[i:i + 500]
                    idph = ",".join("?" * len(batch))
                    # Who pointed INTO these nodes? (their files need re-resolution)
                    for row in conn.execute(
                            f"""SELECT DISTINCT n.file_path FROM edges e
                                JOIN nodes n ON n.repo_id = e.repo_id AND n.id = e.src_id
                                WHERE e.repo_id = ? AND e.dst_id IN ({idph})""",
                            [repo_id] + batch).fetchall():
                        affected.add(row["file_path"])
                    conn.execute(f"DELETE FROM edges WHERE repo_id = ? AND src_id IN ({idph})",
                                 [repo_id] + batch)
                    conn.execute(f"DELETE FROM edges WHERE repo_id = ? AND dst_id IN ({idph})",
                                 [repo_id] + batch)
                conn.execute(f"DELETE FROM nodes WHERE repo_id = ? AND file_path IN ({ph})",
                             [repo_id] + file_paths)
            conn.commit()
        affected -= set(file_paths)
        log_event(logger, "graph.files_deleted", repo_id=repo_id,
                  files=len(file_paths), nodes=len(ids), affected_files=len(affected))
        return ids, affected

    def delete_out_edges(self, repo_id: str, file_paths: Iterable[str],
                         kinds: Iterable[str] = ("CALLS", "IMPORTS", "INHERITS")) -> None:
        """Drop resolution edges ORIGINATING from these files (nodes stay);
        used before re-resolving a file against an updated symbol table."""
        file_paths = sorted(set(file_paths))
        kinds = sorted(set(kinds))
        if not file_paths:
            return
        with self._connect() as conn:
            fph = ",".join("?" * len(file_paths))
            kph = ",".join("?" * len(kinds))
            conn.execute(
                f"""DELETE FROM edges WHERE repo_id = ? AND kind IN ({kph}) AND src_id IN (
                        SELECT id FROM nodes WHERE repo_id = ? AND file_path IN ({fph}))""",
                [repo_id] + kinds + [repo_id] + file_paths)
            conn.commit()

    def update_layout(self, repo_id: str, coords: Dict[int, Tuple[float, float, float, int]]) -> None:
        """Write precomputed 3D positions + Louvain cluster per node."""
        if not coords:
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE nodes SET x = ?, y = ?, z = ?, cluster = ? WHERE repo_id = ? AND id = ?",
                [(x, y, z, c, repo_id, node_id)
                 for node_id, (x, y, z, c) in coords.items()])
            conn.commit()

    def wipe_repo(self, repo_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM edges WHERE repo_id = ?", (repo_id,))
            conn.execute("DELETE FROM nodes WHERE repo_id = ?", (repo_id,))
            conn.commit()
        log_event(logger, "graph.repo_wiped", repo_id=repo_id)

    # -- reads ------------------------------------------------------------------------

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> Dict[str, Any]:
        return {k: row[k] for k in row.keys()}

    def get_nodes(self, repo_id: str, node_ids: Iterable[int]) -> List[Dict[str, Any]]:
        node_ids = sorted(set(node_ids))
        if not node_ids:
            return []
        out: List[Dict[str, Any]] = []
        with self._connect() as conn:
            for i in range(0, len(node_ids), 500):
                batch = node_ids[i:i + 500]
                ph = ",".join("?" * len(batch))
                out.extend(self._row_to_node(r) for r in conn.execute(
                    f"SELECT * FROM nodes WHERE repo_id = ? AND id IN ({ph}) ORDER BY id",
                    [repo_id] + batch).fetchall())
        return out

    def all_nodes(self, repo_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return [self._row_to_node(r) for r in conn.execute(
                "SELECT * FROM nodes WHERE repo_id = ? ORDER BY id", (repo_id,)).fetchall()]

    def all_edges(self, repo_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT src_id, dst_id, kind, confidence FROM edges WHERE repo_id = ? "
                "ORDER BY src_id, dst_id, kind", (repo_id,)).fetchall()]

    def get_nodes_by_chunk_ids(self, repo_id: str, chunk_ids: List[int]) -> List[Dict[str, Any]]:
        """The chunk → node join that powers graph expansion in retrieval."""
        chunk_ids = sorted(set(c for c in chunk_ids if c is not None))
        if not chunk_ids:
            return []
        out: List[Dict[str, Any]] = []
        with self._connect() as conn:
            for i in range(0, len(chunk_ids), 500):
                batch = chunk_ids[i:i + 500]
                ph = ",".join("?" * len(batch))
                out.extend(self._row_to_node(r) for r in conn.execute(
                    f"SELECT * FROM nodes WHERE repo_id = ? AND chunk_id IN ({ph}) ORDER BY id",
                    [repo_id] + batch).fetchall())
        return out

    def find_nodes_by_name(self, repo_id: str, name: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return [self._row_to_node(r) for r in conn.execute(
                "SELECT * FROM nodes WHERE repo_id = ? AND (name = ? OR qualified_name = ?) ORDER BY id",
                (repo_id, name, name)).fetchall()]

    def neighbors(self, repo_id: str, seed_ids: List[int],
                  kinds: Optional[List[str]] = None, depth: int = 1,
                  direction: str = "both", max_nodes: int = 50
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        BFS out to `depth` hops from the seeds. Cycle-safe (visited set),
        bounded (depth cap + max_nodes cap) — correctness requirement #4.
        Returns (nodes, edges) for everything discovered, seeds excluded from
        the node list but included in edges.
        """
        depth = max(1, min(depth, 4))
        kinds = kinds or list(EDGE_KINDS)
        visited: Set[int] = set(seed_ids)
        found_edges: List[Dict[str, Any]] = []
        discovered: List[int] = []
        frontier = deque((sid, 0) for sid in sorted(set(seed_ids)))

        with self._connect() as conn:
            kph = ",".join("?" * len(kinds))
            while frontier and len(discovered) < max_nodes:
                node_id, d = frontier.popleft()
                if d >= depth:
                    continue
                rows = []
                if direction in ("out", "both"):
                    rows += [(r["dst_id"], r) for r in conn.execute(
                        f"SELECT src_id, dst_id, kind, confidence FROM edges "
                        f"WHERE repo_id = ? AND src_id = ? AND kind IN ({kph}) "
                        f"ORDER BY dst_id, kind",
                        [repo_id, node_id] + kinds).fetchall()]
                if direction in ("in", "both"):
                    rows += [(r["src_id"], r) for r in conn.execute(
                        f"SELECT src_id, dst_id, kind, confidence FROM edges "
                        f"WHERE repo_id = ? AND dst_id = ? AND kind IN ({kph}) "
                        f"ORDER BY src_id, kind",
                        [repo_id, node_id] + kinds).fetchall()]
                for other_id, edge_row in rows:
                    found_edges.append(dict(edge_row))
                    if other_id not in visited and len(discovered) < max_nodes:
                        visited.add(other_id)
                        discovered.append(other_id)
                        frontier.append((other_id, d + 1))

        # Dedup edges (both directions can find the same edge).
        seen, edges = set(), []
        for e in found_edges:
            key = (e["src_id"], e["dst_id"], e["kind"])
            if key not in seen:
                seen.add(key)
                edges.append(e)
        return self.get_nodes(repo_id, discovered), edges

    def trace_path(self, repo_id: str, src_id: int, dst_id: int,
                   max_depth: int = 6) -> Optional[List[int]]:
        """Shortest directed path src → dst (BFS, bounded). The Phase 2 exit
        criterion checks this against hand-labeled ground truth in CI."""
        if src_id == dst_id:
            return [src_id]
        visited = {src_id}
        parent: Dict[int, int] = {}
        frontier = deque([(src_id, 0)])
        with self._connect() as conn:
            while frontier:
                node_id, d = frontier.popleft()
                if d >= max_depth:
                    continue
                for row in conn.execute(
                        "SELECT dst_id FROM edges WHERE repo_id = ? AND src_id = ? ORDER BY dst_id",
                        (repo_id, node_id)).fetchall():
                    nxt = row["dst_id"]
                    if nxt in visited:
                        continue
                    visited.add(nxt)
                    parent[nxt] = node_id
                    if nxt == dst_id:
                        path = [dst_id]
                        while path[-1] != src_id:
                            path.append(parent[path[-1]])
                        return list(reversed(path))
                    frontier.append((nxt, d + 1))
        return None

    def subgraph(self, repo_id: str, center: Optional[int] = None,
                 file_path: Optional[str] = None,
                 kinds: Optional[List[str]] = None,
                 depth: int = 2, limit: int = 500) -> Dict[str, Any]:
        """
        Bounded subgraph for the 3D UI (never a full dump at scale — but for
        small/medium repos the architecture overview IS the whole graph,
        capped at `limit` nodes).
        """
        kinds = kinds or list(EDGE_KINDS)
        if center is not None:
            seeds = [center]
            nodes, edges = self.neighbors(repo_id, seeds, kinds, depth,
                                          "both", max_nodes=limit)
            nodes = self.get_nodes(repo_id, [center]) + nodes
        elif file_path is not None:
            with self._connect() as conn:
                seed_ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM nodes WHERE repo_id = ? AND file_path = ? ORDER BY id",
                    (repo_id, file_path)).fetchall()]
            neigh_nodes, edges = self.neighbors(repo_id, seed_ids, kinds, depth,
                                                "both", max_nodes=limit)
            nodes = self.get_nodes(repo_id, seed_ids) + neigh_nodes
        else:
            with self._connect() as conn:
                nodes = [self._row_to_node(r) for r in conn.execute(
                    "SELECT * FROM nodes WHERE repo_id = ? ORDER BY id LIMIT ?",
                    (repo_id, limit)).fetchall()]
                ids = {n["id"] for n in nodes}
                kph = ",".join("?" * len(kinds))
                edges = [dict(r) for r in conn.execute(
                    f"SELECT src_id, dst_id, kind, confidence FROM edges "
                    f"WHERE repo_id = ? AND kind IN ({kph}) ORDER BY src_id, dst_id, kind",
                    [repo_id] + kinds).fetchall()]
                edges = [e for e in edges if e["src_id"] in ids and e["dst_id"] in ids]
        return {"nodes": nodes, "edges": edges}

    def stats(self, repo_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            n_nodes = conn.execute("SELECT COUNT(*) c FROM nodes WHERE repo_id = ?",
                                   (repo_id,)).fetchone()["c"]
            by_kind = {r["kind"]: r["c"] for r in conn.execute(
                "SELECT kind, COUNT(*) c FROM edges WHERE repo_id = ? GROUP BY kind ORDER BY kind",
                (repo_id,)).fetchall()}
            low_conf = conn.execute(
                "SELECT COUNT(*) c FROM edges WHERE repo_id = ? AND confidence < 0.5",
                (repo_id,)).fetchone()["c"]
        return {"nodes": n_nodes, "edges_by_kind": by_kind, "low_confidence_edges": low_conf}


graph_store = GraphStore()
