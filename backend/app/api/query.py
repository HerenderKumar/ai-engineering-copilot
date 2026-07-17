import json
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import List

from app.services.retrieval import retrieve_context
from app.services.prompt_builder import build_rag_prompt
from app.services.llm.router import llm_client
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/query", tags=["Query Engine"])

# --- Pydantic Schemas ---

class QueryRequest(BaseModel):
    repo_id: str = Field(..., description="The unique namespace ID of the indexed repository.")
    query: str = Field(..., description="The developer's question about the codebase.")
    top_k: int = Field(settings.DEFAULT_TOP_K, description="Number of context chunks to retrieve.")

class SourceChunk(BaseModel):
    file_path: str
    chunk_index: int

class QueryResponse(BaseModel):
    repo_id: str
    answer: str
    sources: List[SourceChunk]

# --- Endpoints ---

@router.post("/", response_model=QueryResponse)
async def execute_query(request: QueryRequest):
    """Standard synchronous RAG query."""
    logger.info(f"Processing standard query for repo '{request.repo_id}': {request.query}")
    try:
        retrieved_chunks = retrieve_context(request.repo_id, request.query, request.top_k)
        sources = [SourceChunk(file_path=c["file_path"], chunk_index=c.get("chunk_index", 0)) for c in retrieved_chunks]
        prompt = build_rag_prompt(request.query, retrieved_chunks)
        answer = await llm_client.generate_response(prompt)

        return QueryResponse(repo_id=request.repo_id, answer=answer, sources=sources)
    except Exception as e:
        logger.error(f"Standard query failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stream")
async def stream_query(request: QueryRequest):
    """
    Executes a RAG query and streams the response via Server-Sent Events (SSE).
    Pipeline: Retrieve -> Yield Sources -> Build Prompt -> Stream LLM Tokens.
    """
    logger.info(f"Processing streaming query for repo '{request.repo_id}': {request.query}")

    async def event_generator():
        try:
            # 1. Retrieve Context (Hybrid Search + Re-ranking)
            retrieved_chunks = retrieve_context(request.repo_id, request.query, request.top_k)
            
            # Map sources for the UI
            sources = [{"file_path": c["file_path"], "chunk_index": c.get("chunk_index", 0)} for c in retrieved_chunks]
            
            # 2. Instantly yield the retrieved sources to the client
            # The UI can use this to show "Reading: main.py, auth.py..." while the LLM thinks
            sources_payload = json.dumps({"type": "sources", "data": sources})
            yield f"data: {sources_payload}\n\n"

            # 3. Build Prompt
            prompt = build_rag_prompt(request.query, retrieved_chunks)

            # 4. Stream LLM tokens
            async for text_chunk in llm_client.generate_stream(prompt):
                # Escape newlines and quotes safely via json.dumps
                chunk_payload = json.dumps({"type": "chunk", "data": text_chunk})
                yield f"data: {chunk_payload}\n\n"

            # 5. Signal completion
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            logger.error(f"Streaming query failed: {e}", exc_info=True)
            error_payload = json.dumps({"type": "error", "data": "Internal Server Error during streaming."})
            yield f"data: {error_payload}\n\n"

    # Return the StreamingResponse with the correct SSE media type
    return StreamingResponse(event_generator(), media_type="text/event-stream")