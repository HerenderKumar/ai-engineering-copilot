"""
Graph builder (Phase 2) — turns parsed source files into the knowledge graph.

TWO-PASS design (handoff §6.3). You cannot resolve a call while walking one
file, because the callee usually lives in another file. So:

  Pass 1 (REGISTER): parse every file → create File/Class/Function/Method
          nodes + CONTAINS edges, and collect raw facts (imports, call sites,
          base-class names) into an in-memory symbol table.
  Pass 2 (RESOLVE):  with the whole symbol table known, resolve each call /
          import / inheritance to a target node — import-aware, scored.

Confidence tiers (correctness req #2 — resolution is genuinely hard:
overloads, dynamic dispatch, aliases, shadowing; be honest about certainty):

    0.95  same-file definition        (lexical scope, near ground truth)
    0.90  imported symbol, exact      (`from billing import charge; charge()`)
    0.85  via imported module alias   (`import billing; billing.charge()`)
    0.60  unique global name match    (only one `charge` in the whole repo)
    0.30  ambiguous — MARKED not hidden; emitted to ≤3 candidates so the
          UI's confidence overlay can show them dashed/red for QA

Incremental reconciliation (correctness req #3): on changed files we delete
their nodes + all touching edges, rebuild them, then RE-RESOLVE (a) the
changed files, (b) files that previously pointed into them, and (c) files
that import them — so renaming a function heals every in-bound edge.

Per-file parse isolation: one unparseable file logs a warning and is skipped;
it can never poison the rest of the graph. Determinism (req #5): files and
definitions are processed in sorted order; ids are content-derived — the same
commit always produces the identical graph.

Language tiers: this is the tree-sitter "breadth tier" (~80%). The plan's
precision tier (LSP/pyright/tsserver or Kythe for the top languages) plugs in
behind `resolve()` later and simply overrides edges with confidence 1.0.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.logging import log_event, stage_timer
from app.services import parsing
from app.services.graph_store import GraphStore, graph_store

logger = logging.getLogger(__name__)

CONF_SAME_FILE = 0.95
CONF_IMPORTED_SYMBOL = 0.90
CONF_MODULE_ALIAS = 0.85
CONF_UNIQUE_GLOBAL = 0.60
CONF_AMBIGUOUS = 0.30
MAX_AMBIGUOUS_CANDIDATES = 3

# Resolution outcome distinct from None: the name's provenance is KNOWN to be
# an external module (imported but not indexed). Callers must not fall back to
# global/ambiguous guessing — that fabricates edges (stripe.create → our create).
EXTERNAL = object()


def module_name_of(rel_path: str) -> str:
    """'app/services/billing.py' -> 'app.services.billing' (language-agnostic best effort)."""
    base, _ = os.path.splitext(rel_path.replace("\\", "/"))
    return base.replace("/", ".")


class _FileFacts:
    """Everything pass 1 learned about one file, kept for pass 2."""
    __slots__ = ("rel_path", "language", "module", "file_node_id",
                 "defs", "imports", "calls", "def_nodes")

    def __init__(self, rel_path: str, language: str):
        self.rel_path = rel_path
        self.language = language
        self.module = module_name_of(rel_path)
        self.file_node_id: Optional[int] = None
        self.defs: List[Dict[str, Any]] = []
        self.imports: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []
        self.def_nodes: List[Dict[str, Any]] = []  # node dicts created in pass 1


class GraphBuilder:
    def __init__(self, repo_id: str, repo_root: str, store: GraphStore = graph_store):
        self.repo_id = repo_id
        self.repo_root = repo_root
        self.store = store

    # ---------------------------------------------------------------- public --

    def build_or_update(self, changed_files: List[str], deleted_files: List[str],
                        chunk_spans: Optional[Dict[str, List[Tuple[int, int, int]]]] = None
                        ) -> Dict[str, Any]:
        """
        Entry point called from ingestion.
          changed_files : repo-relative paths that were added/modified
          deleted_files : paths removed from the repo
          chunk_spans   : {file: [(start_line, end_line, faiss_id), ...]} —
                          the chunk↔node join data from the vector store
        """
        chunk_spans = chunk_spans or {}
        changed_files = sorted(set(f for f in changed_files if parsing.is_processable_file(f)))
        deleted_files = sorted(set(deleted_files))

        # 1. Reconciliation: drop stale state, learn which files must re-resolve.
        _, affected_a = self.store.delete_files(self.repo_id, changed_files + deleted_files)

        # 2. Pass 1 over the changed files.
        facts: Dict[str, _FileFacts] = {}
        parse_failures = 0
        for rel_path in changed_files:
            f = self._register_file(rel_path, chunk_spans.get(rel_path))
            if f is None:
                parse_failures += 1
            else:
                facts[rel_path] = f

        # 3. Files needing re-resolution: those that pointed into deleted nodes
        #    + those that import any changed/deleted file (their name lookups
        #    may now land differently).
        affected = set(affected_a) | self._importers_of(changed_files + deleted_files)
        affected -= set(facts)          # already rebuilt in full
        affected = {p for p in affected if os.path.exists(os.path.join(self.repo_root, p))}
        for rel_path in sorted(affected):
            f = self._collect_facts_only(rel_path)
            if f is not None:
                facts[rel_path] = f
        self.store.delete_out_edges(self.repo_id, sorted(affected))

        # 4. Pass 2: resolve calls/imports/inheritance across the whole repo.
        stats = self._resolve(facts)
        stats.update(files_built=len(changed_files), files_reresolved=len(affected),
                     files_deleted=len(deleted_files), parse_failures=parse_failures)
        log_event(logger, "graph.build_done", repo_id=self.repo_id, **stats)
        return stats

    # ---------------------------------------------------------------- pass 1 --

    def _read(self, rel_path: str) -> Optional[str]:
        try:
            with open(os.path.join(self.repo_root, rel_path), "r",
                      encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except Exception as e:
            logger.warning(f"graph: cannot read {rel_path}: {e}")
            return None

    def _register_file(self, rel_path: str,
                       spans: Optional[List[Tuple[int, int, int]]]) -> Optional[_FileFacts]:
        """Pass 1 for one file: create its nodes + CONTAINS edges, collect facts."""
        lang = parsing.language_for(rel_path)
        text = self._read(rel_path)
        if lang is None or text is None:
            return None
        tree = parsing.parse(text, lang)
        if tree is None:  # per-file isolation
            return None

        facts = _FileFacts(rel_path, lang)
        facts.defs = sorted(parsing.extract_definitions(tree, text, lang),
                            key=lambda d: (d["start_line"], d["name"]))
        facts.imports = parsing.extract_imports(tree, text, lang)
        facts.calls = parsing.extract_calls(tree, text, lang)

        # File node (kind='file') anchors CONTAINS and module-level calls.
        file_node = {
            "id": GraphStore.stable_node_id(self.repo_id, facts.module, rel_path, "file"),
            "kind": "file", "name": os.path.basename(rel_path),
            "qualified_name": facts.module, "file_path": rel_path,
            "start_line": 1, "end_line": text.count("\n") + 1,
            "signature": None, "language": lang,
            "chunk_id": self._chunk_for_line(spans, 1),
        }
        facts.file_node_id = file_node["id"]
        nodes = [file_node]
        contains: List[Dict[str, Any]] = []

        class_ids: Dict[str, int] = {}
        for d in facts.defs:
            qualified = (f"{facts.module}.{d['parent']}.{d['name']}"
                         if d["parent"] else f"{facts.module}.{d['name']}")
            node = {
                "id": GraphStore.stable_node_id(self.repo_id, qualified, rel_path,
                                                d.get("signature") or ""),
                "kind": d["kind"], "name": d["name"], "qualified_name": qualified,
                "file_path": rel_path, "start_line": d["start_line"],
                "end_line": d["end_line"], "signature": d.get("signature"),
                "language": lang,
                "chunk_id": self._chunk_for_line(spans, d["start_line"]),
            }
            d["node_id"] = node["id"]
            nodes.append(node)
            facts.def_nodes.append(node)
            if d["kind"] == "class":
                class_ids[d["name"]] = node["id"]

        for d in facts.defs:  # CONTAINS: file→def, class→method
            parent_id = class_ids.get(d["parent"]) if d["parent"] else facts.file_node_id
            contains.append({"src_id": parent_id or facts.file_node_id,
                             "dst_id": d["node_id"], "kind": "CONTAINS",
                             "confidence": 1.0})

        self.store.upsert_nodes(self.repo_id, nodes)
        self.store.upsert_edges(self.repo_id, contains)
        return facts

    def _collect_facts_only(self, rel_path: str) -> Optional[_FileFacts]:
        """Pass-1-lite for affected files: their nodes already exist — just
        re-extract imports/calls/defs so pass 2 can re-resolve their out-edges."""
        lang = parsing.language_for(rel_path)
        text = self._read(rel_path)
        if lang is None or text is None:
            return None
        tree = parsing.parse(text, lang)
        if tree is None:
            return None
        facts = _FileFacts(rel_path, lang)
        facts.defs = sorted(parsing.extract_definitions(tree, text, lang),
                            key=lambda d: (d["start_line"], d["name"]))
        facts.imports = parsing.extract_imports(tree, text, lang)
        facts.calls = parsing.extract_calls(tree, text, lang)
        facts.file_node_id = GraphStore.stable_node_id(
            self.repo_id, facts.module, rel_path, "file")
        # Attach existing node ids to defs (same deterministic derivation).
        for d in facts.defs:
            qualified = (f"{facts.module}.{d['parent']}.{d['name']}"
                         if d["parent"] else f"{facts.module}.{d['name']}")
            d["node_id"] = GraphStore.stable_node_id(
                self.repo_id, qualified, rel_path, d.get("signature") or "")
        return facts

    @staticmethod
    def _chunk_for_line(spans: Optional[List[Tuple[int, int, int]]],
                        line: int) -> Optional[int]:
        """Smallest chunk span containing `line` → its faiss_id (the join)."""
        if not spans:
            return None
        best, best_size = None, None
        for start, end, faiss_id in spans:
            if start <= line <= end:
                size = end - start
                if best_size is None or size < best_size:
                    best, best_size = faiss_id, size
        return best

    def _importers_of(self, files: List[str]) -> Set[str]:
        """Files whose IMPORTS edges point at any of `files`' file-nodes."""
        importers: Set[str] = set()
        targets = {GraphStore.stable_node_id(self.repo_id, module_name_of(p), p, "file")
                   for p in files}
        if not targets:
            return importers
        edges = self.store.all_edges(self.repo_id)
        node_files = {n["id"]: n["file_path"] for n in self.store.all_nodes(self.repo_id)}
        for e in edges:
            if e["kind"] == "IMPORTS" and e["dst_id"] in targets:
                src_file = node_files.get(e["src_id"])
                if src_file:
                    importers.add(src_file)
        return importers

    # ---------------------------------------------------------------- pass 2 --

    def _resolve(self, facts: Dict[str, _FileFacts]) -> Dict[str, Any]:
        """Resolve calls/imports/inheritance for every file in `facts` against
        the FULL repo symbol table (existing nodes + freshly built ones)."""
        with stage_timer(logger, "graph.resolve", repo_id=self.repo_id) as ctx:
            # Global symbol table from the store (covers unchanged files too).
            all_nodes = self.store.all_nodes(self.repo_id)
            by_name: Dict[str, List[Dict[str, Any]]] = {}
            by_qualified: Dict[str, Dict[str, Any]] = {}
            module_file_node: Dict[str, int] = {}
            module_defs: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
            for n in all_nodes:
                if n["kind"] == "file":
                    module_file_node[n["qualified_name"]] = n["id"]
                    continue
                by_name.setdefault(n["name"], []).append(n)
                by_qualified[n["qualified_name"]] = n
                mod = module_name_of(n["file_path"])
                module_defs.setdefault(mod, {}).setdefault(n["name"], []).append(n)

            edges: List[Dict[str, Any]] = []
            total_calls = resolved_calls = 0

            for rel_path in sorted(facts):
                f = facts[rel_path]
                local_defs: Dict[str, List[Dict[str, Any]]] = {}
                for d in f.defs:
                    local_defs.setdefault(d["name"], []).append(d)

                # Import maps for this file.
                alias_to_module: Dict[str, str] = {}       # np -> numpy, billing -> app.billing
                symbol_to_module: Dict[str, str] = {}      # charge -> app.billing
                for imp in f.imports:
                    module = self._normalize_module(imp["module"], f.module)
                    if imp.get("alias"):
                        alias_to_module[imp["alias"]] = module
                    for name, local in imp.get("names", []):
                        if name != "*":
                            symbol_to_module[local] = module
                    # IMPORTS edge file→file for internal modules.
                    target_file_node = module_file_node.get(module)
                    if target_file_node and f.file_node_id:
                        edges.append({"src_id": f.file_node_id, "dst_id": target_file_node,
                                      "kind": "IMPORTS", "confidence": 0.9})

                # INHERITS: class → base class node.
                for d in f.defs:
                    if d["kind"] != "class":
                        continue
                    for base in d.get("bases", []):
                        base_name = base.split(".")[-1]
                        target = self._resolve_name(
                            base_name, base, local_defs, symbol_to_module,
                            alias_to_module, module_defs, by_name, kind_filter="class")
                        if target and target is not EXTERNAL:
                            node_id, conf = target
                            edges.append({"src_id": d["node_id"], "dst_id": node_id,
                                          "kind": "INHERITS", "confidence": conf})

                # CALLS: call site → callee. Source = enclosing def, else file.
                for call in f.calls:
                    total_calls += 1
                    src_id = self._enclosing_def(f, call["line"]) or f.file_node_id
                    if src_id is None:
                        continue
                    receiver = call.get("receiver")
                    full = f"{receiver}.{call['name']}" if receiver else call["name"]
                    target = self._resolve_name(
                        call["name"], full, local_defs, symbol_to_module,
                        alias_to_module, module_defs, by_name)
                    if target is EXTERNAL:
                        continue  # known external — never guess (see EXTERNAL)
                    if target:
                        node_id, conf = target
                        if node_id != src_id:  # skip trivial self-loops
                            edges.append({"src_id": src_id, "dst_id": node_id,
                                          "kind": "CALLS", "confidence": conf})
                        resolved_calls += 1
                    elif call["name"] in by_name:  # ambiguous → marked, not hidden
                        candidates = sorted(by_name[call["name"]],
                                            key=lambda n: n["qualified_name"])
                        if len(candidates) <= MAX_AMBIGUOUS_CANDIDATES:
                            for cand in candidates:
                                if cand["id"] != src_id:
                                    edges.append({"src_id": src_id, "dst_id": cand["id"],
                                                  "kind": "CALLS",
                                                  "confidence": CONF_AMBIGUOUS})
                            resolved_calls += 1

            self.store.upsert_edges(self.repo_id, edges)
            resolution_rate = round(resolved_calls / total_calls, 3) if total_calls else 1.0
            ctx.update(edges=len(edges), calls_seen=total_calls,
                       resolution_rate=resolution_rate)
        return {"edges_written": len(edges), "calls_seen": total_calls,
                "resolution_rate": resolution_rate}

    @staticmethod
    def _normalize_module(module: str, current_module: str) -> str:
        """Best-effort relative-import normalization:
        '.billing' inside app.services.x → app.services.billing;
        './billing' (JS) → sibling module path."""
        if not module:
            return module
        if module.startswith("."):  # python relative or JS relative
            cleaned = module.lstrip("./")
            dots = len(module) - len(module.lstrip("."))
            parent_parts = current_module.split(".")[:-max(1, dots)] if "." in current_module else []
            cleaned = cleaned.replace("/", ".")
            return ".".join([p for p in parent_parts if p] + ([cleaned] if cleaned else []))
        return module.replace("/", ".")

    def _enclosing_def(self, f: _FileFacts, line: int) -> Optional[int]:
        """Smallest definition whose span contains this line (the call site's owner)."""
        best_id, best_size = None, None
        for d in f.defs:
            if d["start_line"] <= line <= d["end_line"]:
                size = d["end_line"] - d["start_line"]
                if best_size is None or size < best_size:
                    best_id, best_size = d.get("node_id"), size
        return best_id

    @staticmethod
    def _resolve_name(name: str, full_expr: str,
                      local_defs: Dict[str, List[Dict[str, Any]]],
                      symbol_to_module: Dict[str, str],
                      alias_to_module: Dict[str, str],
                      module_defs: Dict[str, Dict[str, List[Dict[str, Any]]]],
                      by_name: Dict[str, List[Dict[str, Any]]],
                      kind_filter: Optional[str] = None
                      ) -> Optional[Tuple[int, float]]:
        """The scoring ladder. Returns (node_id, confidence), EXTERNAL when the
        name provably comes from an unindexed module (callers must not guess),
        or None when nothing is known (ambiguity fallback allowed)."""

        def pick(cands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if kind_filter:
                cands = [c for c in cands if c.get("kind") == kind_filter]
            if not cands:
                return None
            return sorted(cands, key=lambda n: n["qualified_name"])[0]

        # 1. Same file.
        if name in local_defs:
            d = local_defs[name][0]
            if not kind_filter or d["kind"] == kind_filter:
                return d["node_id"], CONF_SAME_FILE

        # 2. Directly imported symbol: `from billing import charge` → charge()
        if name in symbol_to_module:
            mod = symbol_to_module[name]
            cand = pick(module_defs.get(mod, {}).get(name, []))
            if cand:
                return cand["id"], CONF_IMPORTED_SYMBOL
            # We KNOW where this name comes from; if that module is external
            # (no defs indexed) or lost the symbol, do NOT fall through to a
            # global name match — that would fabricate edges (false positives
            # corrupt the graph worse than missing edges).
            return EXTERNAL

        # 3. Receiver is a module alias: `billing.charge()` / `np.dot()`
        receiver = full_expr.rsplit(".", 1)[0] if "." in full_expr else None
        if receiver:
            root = receiver.split(".")[0]
            mod = alias_to_module.get(root) or symbol_to_module.get(root)
            if mod:
                cand = pick(module_defs.get(mod, {}).get(name, []))
                if cand:
                    return cand["id"], CONF_MODULE_ALIAS
                # receiver may be an imported class: Billing.charge()
                for cls_defs in module_defs.get(mod, {}).values():
                    for c in cls_defs:
                        if c["kind"] == "class" and c["name"] == root:
                            methods = [n for n in by_name.get(name, [])
                                       if n["kind"] == "method" and n["file_path"] == c["file_path"]]
                            got = pick(methods)
                            if got:
                                return got["id"], CONF_MODULE_ALIAS
                # Known provenance (imported module), no internal match →
                # external call. Stop here; never guess globally.
                return EXTERNAL

        # 4. Unique global match.
        cands = by_name.get(name, [])
        if kind_filter:
            cands = [c for c in cands if c.get("kind") == kind_filter]
        if len(cands) == 1:
            return cands[0]["id"], CONF_UNIQUE_GLOBAL

        return None  # unresolved or ambiguous (caller handles ambiguity)
