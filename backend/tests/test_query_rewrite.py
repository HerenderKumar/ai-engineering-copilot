from app.services.query_rewrite import rewrite_query


def test_original_always_first():
    q = "How does ingestion work?"
    assert rewrite_query(q)[0] == q


def test_camel_case_expansion():
    variants = rewrite_query("How does getUserTier work?")
    assert any("get user tier" in v.lower() for v in variants)


def test_snake_case_expansion():
    variants = rewrite_query("where is file_hashes used")
    assert any("file hashes" in v.lower() for v in variants)


def test_cap_and_dedup():
    variants = rewrite_query("chunking")
    assert 1 <= len(variants) <= 3
    assert len(variants) == len({v.lower() for v in variants})


def test_empty():
    assert rewrite_query("   ") == []
