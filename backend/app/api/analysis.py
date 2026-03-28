import os
from fastapi import APIRouter, HTTPException

from app.services.analysis.aggregator import run_full_analysis

router = APIRouter()


@router.get("/analysis/{repo_id}")
def analyze_repo(repo_id: str):
    """
    Run beginner-friendly analysis on an ingested repository.
    """

    repo_path = os.path.join("storage", "repos", repo_id)

    if not os.path.exists(repo_path):
        raise HTTPException(
            status_code=404,
            detail="Repository not found. Please ingest the repo first."
        )

    return run_full_analysis(repo_path)
