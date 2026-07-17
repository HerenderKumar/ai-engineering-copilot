"""
Test bootstrap. CRITICAL ORDER: environment variables must be set BEFORE any
`app.*` import, because app.core.config builds its singleton at import time.
Keeps all test data in a temp dir (never touches real data/).
"""

import os
import sys
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="copilot_test_data_"))
os.environ.setdefault("REPOS_DIR", tempfile.mkdtemp(prefix="copilot_test_repos_"))
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("CACHE_ENABLED", "false")
os.environ.setdefault("LOG_JSON", "false")

# Make `app` importable when running pytest from backend/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture()
def fixture_repo(tmp_path):
    """Copy the hand-labeled sample repo into a temp dir (tests may mutate it)."""
    import shutil
    src = os.path.join(os.path.dirname(__file__), "fixtures", "sample_repo")
    dest = tmp_path / "sample_repo"
    shutil.copytree(src, dest)
    return str(dest)


@pytest.fixture()
def graph_env(tmp_path):
    """Isolated GraphStore instance (own SQLite file per test)."""
    from app.services.graph_store import GraphStore
    return GraphStore(base_dir=str(tmp_path / "graph_data"))
