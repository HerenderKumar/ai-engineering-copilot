"""
Regression: tree-sitter reports BYTE offsets into the UTF-8 encoded source,
so every slice of the decoded str must go through a byte-safe path. One
em-dash in a docstring used to shift every downstream extraction by +2,
producing garbage symbol names ("eate(o"), unresolvable imports/calls, and
corrupted chunk text — i.e. a silently broken graph for any real-world repo.
"""

import pytest

pytest.importorskip("tree_sitter_languages",
                    reason="requires the tree-sitter grammars")

from app.services import parsing
from app.services.chunking import CodeChunker

# Em-dash (3 bytes) + CJK (3 bytes each) + accented name — all BEFORE the code.
UNICODE_SOURCE = '''"""Fixture with multi-byte text — 处理订单 (naïve case)."""

from billing import Billing


def create(order):
    b = Billing()
    return b.charge(order.user_id, order.total)
'''


def test_definitions_survive_multibyte_prefix():
    tree = parsing.parse(UNICODE_SOURCE, "python")
    defs = parsing.extract_definitions(tree, UNICODE_SOURCE, "python")
    assert [d["name"] for d in defs] == ["create"]
    assert defs[0]["signature"].startswith("def create(order)")


def test_imports_survive_multibyte_prefix():
    tree = parsing.parse(UNICODE_SOURCE, "python")
    imports = parsing.extract_imports(tree, UNICODE_SOURCE, "python")
    assert imports == [{"module": "billing", "names": [("Billing", "Billing")],
                        "alias": None, "line": 3}]


def test_calls_survive_multibyte_prefix():
    tree = parsing.parse(UNICODE_SOURCE, "python")
    calls = parsing.extract_calls(tree, UNICODE_SOURCE, "python")
    assert {(c["name"], c["receiver"]) for c in calls} == {
        ("Billing", None), ("charge", "b")}


def test_chunk_content_survives_multibyte_prefix():
    chunks = CodeChunker().chunk_text(UNICODE_SOURCE, "orders.py")
    joined = "\n".join(c["content"] for c in chunks)
    # The code must appear intact — not byte-shifted — in what gets embedded.
    assert "def create(order):" in joined
    assert "return b.charge(order.user_id, order.total)" in joined
    # Header facts must carry the real import, not a shifted slice of it.
    assert "[Imports: billing]" in chunks[-1]["content"]
