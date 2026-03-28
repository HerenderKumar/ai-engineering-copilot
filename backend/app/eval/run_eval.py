from app.eval.questions import EVAL_QUESTIONS
from app.services.vector_store import load_vector_index
from app.services.embeddings import embed_texts
from app.services.rag import answer_question
import numpy as np


def run_evaluation(repo_id: str):
    index, metadata = load_vector_index(repo_id)

    results = []

    for item in EVAL_QUESTIONS:
        query_vec = embed_texts([item["question"]])[0]
        _, indices = index.search(
            np.array([query_vec]).astype("float32"), 5
        )

        context = [metadata[i] for i in indices[0]]
        response = answer_question(context, item["question"])

        passed = True

        if item["expect_sources"] and not response["sources"]:
            passed = False

        if not item["expect_sources"] and "cannot find" not in response["answer"].lower():
            passed = False

        results.append({
            "question": item["question"],
            "passed": passed,
            "answer": response["answer"],
            "sources": response["sources"]
        })

    return results
