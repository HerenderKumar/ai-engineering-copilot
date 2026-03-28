import logging
from typing import List, Dict, Any
from collections import defaultdict

logger = logging.getLogger(__name__)

class PromptBuilder:
    """
    Constructs highly structured, file-grouped prompts for the LLM reasoning layer.
    """
    
    SYSTEM_INSTRUCTION = (
        "You are a Senior AI Systems Engineer and an expert developer. "
        "Analyze the provided codebase context and answer the user's question accurately. "
        "Strict Guidelines:\n"
        "1. Base your answer ONLY on the provided context.\n"
        "2. If the answer cannot be determined from the context, state that clearly.\n"
        "3. Provide code snippets where relevant, and explain the logic step-by-step.\n"
        "4. Always reference the specific file paths when explaining code.\n"
        "5. Do not hallucinate dependencies, functions, or files that are not in the context.\n\n"
        "CODEBASE CONTEXT:\n"
        "-----------------\n"
    )

    @staticmethod
    def build_rag_prompt(query: str, retrieved_chunks: List[Dict[str, Any]]) -> str:
        """
        Takes raw retrieved chunks, groups them by file path, and builds the final LLM prompt.
        """
        if not retrieved_chunks:
            logger.warning("No chunks provided to prompt builder.")
            return (
                "You are an AI assistant. The user asked a codebase question, but no relevant "
                "context could be retrieved from the repository. Inform the user of this and "
                f"attempt to answer their question generally if possible.\n\nQuestion: {query}"
            )

        # Group chunks by file path to maintain file-level coherence
        grouped_context = defaultdict(list)
        for chunk in retrieved_chunks:
            file_path = chunk.get("file_path", "unknown_file")
            content = chunk.get("content", "")
            grouped_context[file_path].append(content)

        # Build the context string
        context_parts = []
        for file_path, contents in grouped_context.items():
            context_parts.append(f"### FILE: {file_path} ###")
            for idx, content in enumerate(contents):
                # Clean up redundant 'File: ...' prefixes if the chunker already added them
                clean_content = content.replace(f"File: {file_path}\n\n", "")
                context_parts.append(f"--- Snippet {idx + 1} ---")
                context_parts.append(clean_content)
            context_parts.append("\n")

        formatted_context = "\n".join(context_parts)

        # Assemble the final prompt
        final_prompt = (
            f"{PromptBuilder.SYSTEM_INSTRUCTION}"
            f"{formatted_context}\n"
            "-----------------\n"
            f"USER QUESTION: {query}\n\n"
            "YOUR ANSWER:"
        )
        
        logger.debug("Successfully built structured RAG prompt.")
        return final_prompt

# Expose singleton-like function to match the exact import expected by app/api/query.py
build_rag_prompt = PromptBuilder.build_rag_prompt