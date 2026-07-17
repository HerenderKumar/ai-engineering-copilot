"""
Analysis API — beginner-friendly repo explanations + OKF bundle emission.

Fixes vs the original:
  * This router was never mounted in main.py (dead code) — now mounted.
  * It looked for repos under storage/repos/, but ingestion deleted its temp
    clone, so every call 404'd. Ingestion now persists a working copy under
    settings.REPOS_DIR, which this router reads.

POST /analysis/{repo_id}/okf re-emits the curated-knowledge bundle on demand
(it also runs automatically after ingestion when OKF_ON_INGEST=true).
"""

import os

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.services.analysis.aggregator import run_full_analysis
from app.services.okf_emitter import emit_okf_bundle

router = APIRouter(prefix="/analysis", tags=["Analysis & Knowledge"])


def _repo_path_or_404(repo_id: str) -> str:
    repo_path = os.path.join(settings.REPOS_DIR, repo_id)
    if not os.path.exists(repo_path):
        raise HTTPException(
            status_code=404,
            detail="Repository not found. Ingest it first (POST /api/v1/ingest/).",
        )
    return repo_path


@router.get("/{repo_id}")
def analyze_repo(repo_id: str):
    """Run the heuristic analysis layer over the persisted working copy."""
    return {"repo_id": repo_id, "analysis": run_full_analysis(_repo_path_or_404(repo_id))}


@router.post("/{repo_id}/okf")
def emit_okf(repo_id: str):
    """Regenerate the OKF `.knowledge/` bundle + re-embed it (space='okf')."""
    result = emit_okf_bundle(repo_id, _repo_path_or_404(repo_id))
    if result.get("status") != "success":
        raise HTTPException(status_code=500, detail=result.get("error", "OKF emission failed"))
    return result
