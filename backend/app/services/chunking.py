"""
Unified syntax-aware chunker — THE Phase 0 first fix (handoff §4).

The original bug: `ingestion.py` imported `CodeChunker` and called
`chunker.chunk_text(...)`, but this module only defined `ASTChunker` with
`get_chunks()` and no `overlap_size` support → ImportError on startup.

The fix (locked decision #11): ONE chunker class, `CodeChunker`, exposing
`chunk_text(text, file_path)`, with `overlap_size` support. `ASTChunker` and
`get_chunks` remain as aliases so nothing else can break again.

What a chunk is
---------------
LLMs and embedding models work on limited windows, so files are split into
pieces ("chunks"). Naive splitting cuts functions in half; this chunker uses
the tree-sitter syntax tree to split at *logical boundaries* (whole classes /
functions), packing small neighbors together up to `max_chunk_size`, and only
falls back to line-based splitting (with overlap) for oversized nodes or
unparseable files.

Context headers (Phase 1, handoff §6.1)
---------------------------------------
Each chunk is prefixed with a compact structural header before embedding:

    [Path: src/checkout/billing.py] [Lang: python] [Class: Billing]
    [Imports: stripe, ledger] [Calls: fetch_user_tier, update_ledger]
    <code>

The vector then encodes *topology + semantics*: a query about "user tier
lookups during checkout" can surface `billing.py` even when the code shares
no words with the query. Rules (or enrichment backfires):
  * embed only STABLE facts — path, signature, parent class, imports,
    outgoing calls. Never incoming callers (volatile: they change when other
    files change, which would silently stale this chunk's vector).
  * keep headers SHORT — over-stuffing makes chunks mutually similar and
    *drops* recall, so every list is capped.

Every chunk dict:
  {file_path, chunk_index, content, start_line, end_line, language}
`content` = header + code (what gets embedded AND stored).
Line spans are 1-based; they power file:line citations and the
chunk ↔ graph-node join (a node lives in the chunk containing its start line).
"""

import logging
import os
from typing import Any, Dict, List, Optional

from app.services import parsing
from app.services.parsing import (  # re-exported for backward compatibility
    IGNORED_DIRS, IGNORED_EXTENSIONS, LANGUAGE_MAP,
)

logger = logging.getLogger(__name__)

MAX_HEADER_IMPORTS = 6
MAX_HEADER_CALLS = 6
MAX_HEADER_DEFS = 5


class CodeChunker:
    """One chunker for the whole platform (the unified contract)."""

    def __init__(self, max_chunk_size: int = 1500, overlap_size: int = 200):
        self.max_chunk_size = max_chunk_size
        # Overlap applies to the line-based fallback; AST boundaries make
        # overlap unnecessary between whole-definition chunks.
        self.overlap_size = max(0, min(overlap_size, max_chunk_size // 2))

    # -- public API -----------------------------------------------------------

    def is_processable_file(self, file_path: str) -> bool:
        return parsing.is_processable_file(file_path)

    def chunk_text(self, text: str, file_path: str, tree=None) -> List[Dict[str, Any]]:
        """
        Split one file into chunks. `tree` lets ingestion pass an already
        parsed tree ("parse once, use twice" — the graph builder reuses it).
        """
        lang = parsing.language_for(file_path)
        if lang is None:
            return self._line_chunks(text, file_path, lang=None)

        if tree is None:
            tree = parsing.parse(text, lang)
        if tree is None:  # parse failure → per-file isolation, degrade to lines
            return self._line_chunks(text, file_path, lang)

        try:
            # tree-sitter node offsets are BYTE offsets — slice the encoded
            # source, never the str (multi-byte chars shift str indices).
            data = text.encode("utf-8")
            imports = parsing.extract_imports(tree, text, lang)
            import_names = []
            for imp in imports:
                label = imp["module"] or ""
                if label and label not in import_names:
                    import_names.append(label)

            chunks: List[Dict[str, Any]] = []
            batch: List[Any] = []          # consecutive small top-level nodes
            batch_size = 0

            def flush_batch():
                nonlocal batch, batch_size
                if not batch:
                    return
                code = data[batch[0].start_byte:batch[-1].end_byte].decode(
                    "utf-8", errors="replace")
                chunks.append(self._make_chunk(
                    code, file_path, len(chunks), lang, tree, text, import_names,
                    start_line=batch[0].start_point[0] + 1,
                    end_line=batch[-1].end_point[0] + 1,
                    start_byte=batch[0].start_byte,
                    end_byte=batch[-1].end_byte,
                ))
                batch, batch_size = [], 0

            for node in tree.root_node.children:
                node_len = node.end_byte - node.start_byte
                if node_len > self.max_chunk_size:
                    flush_batch()
                    # Oversized class/function → split by lines WITH overlap
                    node_text = data[node.start_byte:node.end_byte].decode(
                        "utf-8", errors="replace")
                    for piece in self._split_lines(node_text):
                        offset = node.start_point[0] + 1
                        chunks.append(self._make_chunk(
                            piece["code"], file_path, len(chunks), lang,
                            tree, text, import_names,
                            start_line=offset + piece["rel_start"],
                            end_line=offset + piece["rel_end"],
                            start_byte=node.start_byte,  # header facts from whole node
                            end_byte=node.end_byte,
                        ))
                    continue
                if batch_size + node_len > self.max_chunk_size:
                    flush_batch()
                batch.append(node)
                batch_size += node_len
            flush_batch()

            return chunks or self._line_chunks(text, file_path, lang)

        except Exception as e:
            logger.error(f"AST chunking failed for {file_path}, falling back to lines: {e}")
            return self._line_chunks(text, file_path, lang)

    # Backward-compatible alias (the old ASTChunker API).
    get_chunks = chunk_text

    # -- internals -------------------------------------------------------------

    def _make_chunk(self, code: str, file_path: str, index: int, lang: str,
                    tree, full_text: str, import_names: List[str],
                    start_line: int, end_line: int,
                    start_byte: int, end_byte: int) -> Dict[str, Any]:
        header = self._build_header(
            file_path, lang, tree, full_text, import_names, start_byte, end_byte
        )
        return {
            "file_path": file_path,
            "chunk_index": index,
            "content": f"{header}\n{code}",
            "start_line": start_line,
            "end_line": end_line,
            "language": lang,
        }

    def _build_header(self, file_path: str, lang: Optional[str], tree,
                      full_text: str, import_names: List[str],
                      start_byte: int, end_byte: int) -> str:
        """Compact, stable-facts-only context header (§6.1)."""
        parts = [f"[Path: {file_path}]"]
        if lang:
            parts.append(f"[Lang: {lang}]")
        if tree is not None and lang in parsing.FULL_SUPPORT | parsing.DEFS_SUPPORT:
            try:
                defs = [d for d in parsing.extract_definitions(tree, full_text, lang)
                        if d["start_byte"] < end_byte and d["end_byte"] > start_byte]
                classes = sorted({d["parent"] for d in defs if d["parent"]} |
                                 {d["name"] for d in defs if d["kind"] == "class"})
                if classes:
                    parts.append(f"[Class: {', '.join(classes[:MAX_HEADER_DEFS])}]")
                fn_names = [d["name"] for d in defs if d["kind"] in ("function", "method")]
                if fn_names:
                    parts.append(f"[Defs: {', '.join(fn_names[:MAX_HEADER_DEFS])}]")
                if import_names:
                    parts.append(f"[Imports: {', '.join(import_names[:MAX_HEADER_IMPORTS])}]")
                def_names = set(fn_names)
                calls = parsing.extract_calls(tree, full_text, lang, start_byte, end_byte)
                out_calls = []
                for c in calls:  # outgoing only, skip self-recursion noise
                    if c["name"] not in def_names and c["name"] not in out_calls:
                        out_calls.append(c["name"])
                if out_calls:
                    parts.append(f"[Calls: {', '.join(out_calls[:MAX_HEADER_CALLS])}]")
            except Exception as e:
                logger.debug(f"header enrichment skipped for {file_path}: {e}")
        return " ".join(parts)

    def _split_lines(self, text: str) -> List[Dict[str, Any]]:
        """Split text into ≤max_chunk_size pieces at line boundaries, carrying
        `overlap_size` characters of trailing context into the next piece."""
        lines = text.splitlines(keepends=True)
        pieces, current, current_len, rel_start = [], [], 0, 0
        for line_no, line in enumerate(lines):
            if current_len + len(line) > self.max_chunk_size and current:
                pieces.append({"code": "".join(current),
                               "rel_start": rel_start, "rel_end": line_no - 1})
                # Overlap: seed next piece with the tail of this one
                tail, tail_len, back = [], 0, len(current) - 1
                while back >= 0 and tail_len < self.overlap_size:
                    tail.insert(0, current[back])
                    tail_len += len(current[back])
                    back -= 1
                current, current_len = tail, tail_len
                rel_start = max(0, line_no - len(tail))
            current.append(line)
            current_len += len(line)
        if current:
            pieces.append({"code": "".join(current),
                           "rel_start": rel_start, "rel_end": len(lines) - 1 if lines else 0})
        return pieces

    def _line_chunks(self, text: str, file_path: str, lang: Optional[str]) -> List[Dict[str, Any]]:
        """Fallback for unparseable files: line splitting + minimal header."""
        header = f"[Path: {file_path}]" + (f" [Lang: {lang}]" if lang else "")
        return [{
            "file_path": file_path,
            "chunk_index": i,
            "content": f"{header}\n{p['code']}",
            "start_line": p["rel_start"] + 1,
            "end_line": p["rel_end"] + 1,
            "language": lang,
        } for i, p in enumerate(self._split_lines(text))]


# Backward-compatible alias: old imports of ASTChunker keep working forever.
ASTChunker = CodeChunker


def process_directory(repo_path: str) -> List[Dict[str, Any]]:
    """Chunk every processable file under a directory (used by eval/tools)."""
    chunker = CodeChunker()
    all_chunks: List[Dict[str, Any]] = []
    for root, _, files in os.walk(repo_path):
        for file in files:
            rel_path = os.path.relpath(os.path.join(root, file), repo_path)
            if not chunker.is_processable_file(rel_path):
                continue
            try:
                with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                    content = f.read()
                all_chunks.extend(chunker.chunk_text(content, rel_path))
            except Exception as e:
                logger.error(f"Error reading {rel_path}: {e}")
    return all_chunks
