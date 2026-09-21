"""FastAPI application for BedtimeNews Agentic RAG service."""

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .chat import nonstream_chat, stream_chat
from .models import ChatRequest, ChatResponse
from .settings import settings
from .vector_db import (
    close_connection_pool,
    get_transcript,
    list_transcripts,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Manage application lifespan."""
    logger.info("Starting up")
    logger.info(
        f"Using models: FAST={settings.generation.fast_model or settings.generation.model}, GENERATION={settings.generation.model}"
    )
    yield
    logger.info("Shutting down")
    close_connection_pool()


app = FastAPI(lifespan=lifespan)


# ============================================================================
# Chat Endpoint (Agentic RAG)
# ============================================================================
@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Intelligent Q&A using the BedtimeNews Agentic RAG system.

    Agentic RAG Pipeline:
    1. Routing: Determines if the question needs retrieval or constrained
       direct handling
    2. Query Rewriting: Optimizes search queries for better retrieval
    3. Retrieval: Multi-query semantic search
    4. Document Grading: Filters relevant documents using LLM
    5. Answer Generation: Synthesizes answer with citations

    Features:
    - Automatic routing (RAG vs greeting/out-of-scope handling)
    - Multi-query retrieval for better coverage
    - Document relevance grading
    - Markdown-link citations to the source episodes
    - Optional streaming support

    Args:
        request: Chat request with question and options

    Returns:
        ChatResponse with the generated answer,
        OR StreamingResponse of SSE events (if stream=True)
    """
    logger.debug(f"Chat: '{request.question[:100]}...', stream={request.stream}")

    # Exception is caught and transformed to error message SSE inside stream_chat,
    # no try-catch needed.
    if request.stream:
        return StreamingResponse(
            stream_chat(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        # The RAG pipeline is synchronous (seconds of LLM calls); run it in a
        # worker thread so it doesn't block the event loop for other requests.
        return await asyncio.to_thread(nonstream_chat, request)
    except Exception as e:
        logger.exception("Chat error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chat processing failed: {str(e)}",
        ) from e


def _validate_transcript_uri(doc_id: str) -> str:
    """Accept only canonical relative Markdown URIs, never filesystem paths."""
    if not doc_id or "\x00" in doc_id or "\\" in doc_id:
        raise HTTPException(status_code=404, detail="Transcript not found")
    if any(part in {"", ".", ".."} for part in doc_id.split("/")):
        raise HTTPException(status_code=404, detail="Transcript not found")
    path = PurePosixPath(doc_id)
    if path.is_absolute() or path.suffix.lower() != ".md":
        raise HTTPException(status_code=404, detail="Transcript not found")
    return path.as_posix()


def _etag_for(payload) -> str:
    encoded = json.dumps(
        jsonable_encoder(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f'"{hashlib.sha256(encoded).hexdigest()}"'


def _conditional_json(request: Request, payload, etag: str) -> Response:
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(jsonable_encoder(payload), headers=headers)


@app.get("/transcripts")
async def transcript_index(request: Request) -> Response:
    """Reader-navigation metadata reconstructed from upstream Markdown."""
    try:
        items = await asyncio.to_thread(list_transcripts)
    except Exception as exc:
        logger.exception("Transcript index unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Transcript service unavailable",
        ) from exc
    payload = {"items": items}
    return _conditional_json(request, payload, _etag_for(payload))


@app.get("/transcripts/{doc_id:path}")
async def transcript_detail(doc_id: str, request: Request) -> Response:
    """One sanitized reader document selected only by its exact DB URI."""
    canonical = _validate_transcript_uri(doc_id)
    try:
        article = await asyncio.to_thread(get_transcript, canonical)
    except Exception as exc:
        logger.exception("Transcript unavailable: %s", canonical)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Transcript service unavailable",
        ) from exc
    if article is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return _conditional_json(request, article, _etag_for(article))
