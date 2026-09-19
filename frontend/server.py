"""
Frontend server for the BedtimeNews Agentic RAG chat.

Responsibilities:
- Serve the static single-page UI from ./static
- Expose the sample questions as JSON at /api/starters
- Proxy the chat stream at /chat to the internal agent backend, which is not
  reachable from outside the Docker network

The agent's /chat endpoint speaks Server-Sent Events:
    data: {"type": "step", "step": "...", "content": "..."}
    data: {"type": "citations", "urls": {...}}
    data: {"type": "answer_chunk", "content": "..."}
    data: {"type": "answer_final", "content": "...", "grounded": true}
    data: {"type": "answer_meta", "grounded": true}
    data: {"type": "followups", "items": [...]}
    data: {"type": "error", "content": "..."}
    data: [DONE]
We stream those bytes straight through to the browser.
"""

import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib import metadata
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope
from starters import CATEGORIES

AGENT_BACKEND_HOST = os.environ.get("AGENT_BACKEND_HOST", "agent")
AGENT_BACKEND_PORT = os.environ.get("AGENT_BACKEND_PORT", "8000")
AGENT_BASE_URL = f"http://{AGENT_BACKEND_HOST}:{AGENT_BACKEND_PORT}"  # noqa: S5332
CHAT_ENDPOINT = f"{AGENT_BASE_URL}/chat"
TRANSCRIPTS_ENDPOINT = f"{AGENT_BASE_URL}/transcripts"

STATIC_DIR = Path(__file__).parent / "static"

# Shared upstream client: reuses connections to the agent across requests
# instead of paying a new connection pool + TCP handshake per chat.
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _client
    _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


app = FastAPI(title="睡前消息知识库", lifespan=lifespan)

# Compress text assets. Starlette excludes text/event-stream from compression,
# which is what keeps the /chat stream flushing event-by-event.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        "connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'"
    )
    return response


def _sse_error(message: str) -> bytes:
    """Emit a terminal SSE error event the frontend understands, then close."""
    event = json.dumps({"type": "error", "content": message}, ensure_ascii=False)
    return f"data: {event}\n\ndata: [DONE]\n\n".encode()


def _resolve_version() -> str:
    """What to show in the masthead.

    APP_VERSION is set from IMAGE_TAG by docker-compose, so a deployed release
    reports the image tag actually running rather than whatever the source tree
    last declared. Falling back to the installed package keeps a bare
    `uvicorn server:app` honest, and "dev" covers a checkout run in place.
    """
    tag = os.environ.get("APP_VERSION", "").strip()
    if tag and tag != "latest":
        return tag
    try:
        return metadata.version("bedtimenews-frontend")
    except metadata.PackageNotFoundError:
        return "dev"


APP_VERSION = _resolve_version()


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": APP_VERSION})


@app.get("/api/starters")
async def get_starters() -> JSONResponse:
    """Sample questions (grouped by category) for the empty-state list."""
    return JSONResponse({"categories": CATEGORIES})


@app.post("/chat")
async def chat(request: Request) -> StreamingResponse:
    """Proxy the chat SSE stream from the internal agent to the browser."""
    body = await request.body()

    async def event_stream() -> AsyncGenerator[bytes, None]:
        client = _client
        if client is None:
            yield _sse_error("档案服务尚未就绪，请稍后重试。")
            return
        try:
            async with client.stream(
                "POST",
                CHAT_ENDPOINT,
                content=body,
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 422:
                yield _sse_error("问题格式有误，请检查后重试。")
            elif code >= 500:
                yield _sse_error("档案服务暂时不可用，请稍后重试。")
            else:
                yield _sse_error(f"服务错误（代码 {code}），请稍后重试。")
        except httpx.TimeoutException:
            yield _sse_error("信号超时，请稍后重试。")
        except Exception as exc:  # noqa: BLE001 - surface anything to the client
            yield _sse_error(f"信号中断：{exc}")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _proxy_transcript_json(request: Request, upstream_url: str) -> Response:
    client = _client
    if client is None:
        return JSONResponse(
            {"detail": "文稿服务尚未就绪"},
            status_code=503,
            headers={"Cache-Control": "no-cache"},
        )
    headers = {}
    if etag := request.headers.get("if-none-match"):
        headers["If-None-Match"] = etag
    try:
        response = await client.get(upstream_url, headers=headers)
    except (httpx.TimeoutException, httpx.NetworkError):
        return JSONResponse(
            {"detail": "文稿服务暂时不可用"},
            status_code=503,
            headers={"Cache-Control": "no-cache"},
        )

    forwarded = {"Cache-Control": response.headers.get("cache-control", "no-cache")}
    if etag := response.headers.get("etag"):
        forwarded["ETag"] = etag
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
        headers=forwarded,
    )


@app.get("/api/transcripts")
async def transcript_index(request: Request) -> Response:
    return await _proxy_transcript_json(request, TRANSCRIPTS_ENDPOINT)


@app.get("/api/transcripts/{doc_id:path}")
async def transcript_detail(doc_id: str, request: Request) -> Response:
    encoded = quote(doc_id, safe="/")
    return await _proxy_transcript_json(request, f"{TRANSCRIPTS_ENDPOINT}/{encoded}")


@app.get("/transcripts", include_in_schema=False)
@app.get("/transcripts/{doc_id:path}", include_in_schema=False)
async def transcript_spa(doc_id: str = "") -> FileResponse:
    del doc_id
    return FileResponse(
        STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
    )


class CachedStaticFiles(StaticFiles):
    """StaticFiles that sets an explicit Cache-Control on every asset.

    Filenames here are not content-hashed, so anything that ships with a UI
    change has to revalidate on each load or a deploy would leave browsers on
    stale code. StaticFiles already sends an ETag, which makes that a 304 with
    an empty body rather than a full re-download.

    LONG_LIVED is the exception: assets that only change when someone
    deliberately replaces the file, and so can be cached outright.
    """

    LONG_LIVED = frozenset({"markdown-it.min.js", "bedtimenews.webp"})

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if path in self.LONG_LIVED:
            response.headers["Cache-Control"] = "public, max-age=604800"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


# Static assets and index.html (mounted last so API routes take precedence).
app.mount("/", CachedStaticFiles(directory=STATIC_DIR, html=True), name="static")
