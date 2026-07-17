"""
Labeled evaluation question sets (Phase 0).

Format: one list of questions per repo_id. Each question carries the files a
correct retrieval MUST surface (`relevant_files`, repo-relative paths). This
is the ground truth the metrics in metrics.py score against.

How to build a set for a new repo (15-30 questions is plenty):
  1. Ingest the repo.
  2. Write questions a real developer would ask (mix: "where is X done",
     "what calls Y", "why do we Z", exact-name and NO-exact-name phrasings).
  3. For each, list every file that genuinely contains the answer.

Keep questions honest — if you only ask questions containing exact function
names, BM25 alone will look perfect and you'll learn nothing.

The set below targets THIS repository itself (the copilot indexing its own
backend), so the eval runs out of the box:
    python -m app.eval.run_eval ai-copilot-self
after ingesting the repo under repo_id `ai-copilot-self`.
"""

QUESTION_SETS = {
    "ai-copilot-self": [
        {
            "question": "How do we avoid re-embedding an unchanged repository?",
            "relevant_files": ["app/services/ingestion.py", "app/services/vector_store.py"],
        },
        {
            "question": "Where are code files split into chunks at function boundaries?",
            "relevant_files": ["app/services/chunking.py"],
        },
        {
            "question": "How is the FAISS index searched for similar vectors?",
            "relevant_files": ["app/services/vector_store.py"],
        },
        {
            "question": "Where do we combine keyword search results with semantic search results?",
            "relevant_files": ["app/services/retrieval.py", "app/services/fusion.py"],
        },
        {
            "question": "How does the streaming chat endpoint send tokens to the browser?",
            "relevant_files": ["app/api/query.py", "app/services/llm/gemini.py"],
        },
        {
            "question": "Which component listens to the job queue and runs ingestion?",
            "relevant_files": ["app/workers/ingestion_worker.py"],
        },
        {
            "question": "Where are call and import relationships between functions extracted?",
            "relevant_files": ["app/services/graph_builder.py", "app/services/parsing.py"],
        },
        {
            "question": "How are graph nodes and edges persisted to the database?",
            "relevant_files": ["app/services/graph_store.py"],
        },
        {
            "question": "Where is the final prompt for the language model assembled?",
            "relevant_files": ["app/services/prompt_builder.py"],
        },
        {
            "question": "How does the system rank chunks after retrieving them?",
            "relevant_files": ["app/services/reranker.py", "app/services/retrieval.py"],
        },
        {
            "question": "Where are application settings and environment variables defined?",
            "relevant_files": ["app/core/config.py"],
        },
        {
            "question": "How is the 3D position of every graph node precomputed?",
            "relevant_files": ["app/services/graph_layout.py"],
        },
    ],
}

# Backward-compat: the old eval imported EVAL_QUESTIONS directly.
EVAL_QUESTIONS = QUESTION_SETS["ai-copilot-self"]
