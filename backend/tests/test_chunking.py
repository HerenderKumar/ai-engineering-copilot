"""Chunker: the unified contract + context headers + overlap fallback."""

import pytest

pytest.importorskip("tree_sitter_languages",
                    reason="AST chunking requires the tree-sitter grammars")

from app.services.chunking import ASTChunker, CodeChunker


def test_unified_contract_exists():
    """The Phase 0 bug: ingestion imported CodeChunker.chunk_text with
    overlap_size — this locks the contract so it can't regress."""
    chunker = CodeChunker(max_chunk_size=1500, overlap_size=200)
    assert hasattr(chunker, "chunk_text")
    assert hasattr(chunker, "is_processable_file")
    assert ASTChunker is CodeChunker  # old name keeps working


def test_ast_chunking_with_context_header(fixture_repo):
    with open(f"{fixture_repo}/billing.py", encoding="utf-8") as f:
        text = f.read()
    chunks = CodeChunker().chunk_text(text, "billing.py")
    assert chunks, "expected at least one chunk"
    head = chunks[0]["content"]
    assert "[Path: billing.py]" in head
    assert "[Lang: python]" in head
    assert "Imports:" in head and "users" in head      # stable fact: imports
    assert "fetch_user_tier" in head                    # stable fact: outgoing call
    # 1-based line spans present (powers file:line citations + graph join)
    assert chunks[0]["start_line"] >= 1
    assert chunks[0]["end_line"] >= chunks[0]["start_line"]


def test_oversized_node_line_split_with_overlap():
    body = "\n".join(f"    x{i} = {i}  # padding line {i}" for i in range(200))
    text = f"def huge():\n{body}\n"
    chunker = CodeChunker(max_chunk_size=800, overlap_size=200)
    chunks = chunker.chunk_text(text, "huge.py")
    assert len(chunks) > 1
    # Overlap: some tail content of chunk N reappears at the head of chunk N+1.
    first_code = chunks[0]["content"].split("\n", 1)[1]
    second_code = chunks[1]["content"].split("\n", 1)[1]
    tail_line = first_code.rstrip("\n").splitlines()[-1].strip()
    assert tail_line in second_code


def test_unparseable_falls_back_to_lines():
    chunks = CodeChunker(max_chunk_size=100).chunk_text("just some text\n" * 30, "notes.xyz")
    assert chunks and all(c["content"].startswith("[Path: notes.xyz]") for c in chunks)


def test_ignores_vendored_paths():
    chunker = CodeChunker()
    assert not chunker.is_processable_file("node_modules/pkg/index.js")
    assert not chunker.is_processable_file(".git/hooks/x.py")
    assert chunker.is_processable_file("src/app.py")
