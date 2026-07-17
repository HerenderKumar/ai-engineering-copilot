"""
Retrieval evaluation harness (Phase 0) — "a baseline number to beat".

The old file imported modules that never existed (app.services.rag,
load_vector_index, embed_texts) — it could not run. This version evaluates
the REAL retrieval pipeline end-to-end (rewrite → dense+sparse → RRF →
graph expansion → rerank) but stops BEFORE the LLM: retrieval quality is
what we gate phases on, and skipping generation makes the eval fast, free
and deterministic.

Usage:
    cd backend
    python -m app.eval.run_eval <repo_id> [--top-k 20]

For each labeled question it:
  1. runs retrieve_context(),
  2. reduces the ranked chunks to a ranked, deduplicated file list,
  3. scores recall@{5,10,20}, MRR, nDCG against `relevant_files`,
  4. prints per-question + aggregate JSON and saves a timestamped report
     under eval_results/ so you can diff runs across phases.
"""

import argparse
import json
import os
import sys
import time
from typing import List

from app.core.logging import configure_logging, log_event
import logging

from app.eval.metrics import aggregate, evaluate_ranking
from app.eval.questions import QUESTION_SETS

logger = logging.getLogger(__name__)

RESULTS_DIR = "eval_results"


def ranked_files(chunks: List[dict]) -> List[str]:
    """Ranked chunk list → ranked unique file list (order of first appearance)."""
    seen, files = set(), []
    for c in chunks:
        fp = c.get("file_path")
        if fp and fp not in seen:
            seen.add(fp)
            files.append(fp)
    return files


def run_evaluation(repo_id: str, top_k: int = 20) -> dict:
    # Import inside the function so `--help` works without heavy deps loaded.
    from app.services.retrieval import retrieve_context

    questions = QUESTION_SETS.get(repo_id)
    if not questions:
        raise SystemExit(
            f"No labeled question set for repo_id '{repo_id}'. "
            f"Add one to app/eval/questions.py (available: {list(QUESTION_SETS)})"
        )

    per_question = []
    detailed = []
    for item in questions:
        t0 = time.perf_counter()
        try:
            chunks = retrieve_context(repo_id, item["question"], top_k=top_k)
        except Exception as e:
            logger.error(f"retrieval failed for question '{item['question']}': {e}")
            chunks = []
        files = ranked_files(chunks)
        scores = evaluate_ranking(files, item["relevant_files"])
        per_question.append(scores)
        detailed.append({
            "question": item["question"],
            "relevant_files": item["relevant_files"],
            "retrieved_files": files[:top_k],
            "scores": scores,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        })

    summary = aggregate(per_question)
    report = {
        "repo_id": repo_id,
        "n_questions": len(questions),
        "top_k": top_k,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "aggregate": summary,
        "questions": detailed,
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = os.path.join(RESULTS_DIR, f"{repo_id}_{int(time.time())}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    log_event(logger, "eval.done", repo_id=repo_id, report=out_path, **summary)
    return report


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description="Run the retrieval eval for a repo")
    parser.add_argument("repo_id")
    parser.add_argument("--top-k", type=int, default=20)
    args = parser.parse_args()

    result = run_evaluation(args.repo_id, args.top_k)
    print(json.dumps(result["aggregate"], indent=2))
    sys.exit(0)
