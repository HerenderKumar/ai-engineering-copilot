"""
Prompt builder (Phase 3 upgrade) — file-grouped context + STRUCTURAL PREAMBLE.

The old prompt gave the LLM a bag of snippets. The upgrade (handoff §6.7)
prepends a compact list of true relationships pulled from the knowledge
graph, e.g.:

    STRUCTURAL RELATIONSHIPS (from the code knowledge graph):
    - app.billing.Billing.charge calls app.users.fetch_user_tier
    - app.orders.create calls app.billing.Billing.charge

so the model reasons over dependencies instead of guessing them — and the
citations it produces can name real call chains. Retrieval attaches these
lines to chunks as `graph_context` (see retrieval._graph_expand); chunks
found via graph expansion are labeled so the model knows why they're there.
"""

import logging
from collections import defaultdict
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class PromptBuilder:

    SYSTEM_INSTRUCTION = (
        "You are a Senior AI Systems Engineer and an expert developer. "
        "Analyze the provided codebase context and answer the user's question accurately. "
        "Strict Guidelines:\n"
        "1. Base your answer ONLY on the provided context.\n"
        "2. If the answer cannot be determined from the context, state that clearly.\n"
        "3. Provide code snippets where relevant, and explain the logic step-by-step.\n"
        "4. Always reference the specific file paths (and line ranges when shown) "
        "when explaining code.\n"
        "5. Use the STRUCTURAL RELATIONSHIPS section to explain how components "
        "connect (who calls what); do not invent relationships not listed or visible.\n"
        "6. Do not hallucinate dependencies, functions, or files that are not in the context.\n\n"
    )

    @staticmethod
    def build_rag_prompt(query: str, retrieved_chunks: List[Dict[str, Any]]) -> str:
        if not retrieved_chunks:
            logger.warning("No chunks provided to prompt builder.")
            return (
                "You are an AI assistant. The user asked a codebase question, but no relevant "
                "context could be retrieved from the repository. Inform the user of this and "
                f"attempt to answer their question generally if possible.\n\nQuestion: {query}"
            )

        # ---- structural preamble (Phase 3) ----
        relationship_lines: List[str] = []
        for chunk in retrieved_chunks:
            for line in chunk.get("graph_context", []) or []:
                if line not in relationship_lines:
                    relationship_lines.append(line)
        preamble = ""
        if relationship_lines:
            preamble = (
                "STRUCTURAL RELATIONSHIPS (from the code knowledge graph):\n"
                + "\n".join(f"- {line}" for line in relationship_lines[:15])
                + "\n\n"
            )

        # ---- file-grouped code context ----
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for chunk in retrieved_chunks:
            grouped[chunk.get("file_path", "unknown_file")].append(chunk)

        context_parts: List[str] = []
        for file_path, chunks in grouped.items():
            context_parts.append(f"### FILE: {file_path} ###")
            for idx, chunk in enumerate(chunks):
                content = chunk.get("content", "")
                # Strip legacy 'File: ...' prefixes from pre-Phase-1 chunks.
                content = content.replace(f"File: {file_path}\n\n", "")
                span = ""
                if chunk.get("start_line"):
                    span = f" (lines {chunk['start_line']}-{chunk.get('end_line', '?')})"
                via = " [included via code-graph relationship]" if chunk.get("via_graph") else ""
                context_parts.append(f"--- Snippet {idx + 1}{span}{via} ---")
                context_parts.append(content)
            context_parts.append("")

        return (
            f"{PromptBuilder.SYSTEM_INSTRUCTION}"
            f"{preamble}"
            "CODEBASE CONTEXT:\n"
            "-----------------\n"
            f"{chr(10).join(context_parts)}\n"
            "-----------------\n"
            f"USER QUESTION: {query}\n\n"
            "YOUR ANSWER:"
        )


build_rag_prompt = PromptBuilder.build_rag_prompt
