"""
Frontend server for the BedtimeNews Agentic RAG chat.

Responsibilities:
- Serve the static single-page UI from ./static
- Server-render the transcript pages (/, /transcripts?channel=…,
  /transcripts/<URI>), robots.txt, sitemap.xml and short links (/s/<id>)
- Expose the sample questions as JSON at /api/starters
- Proxy the chat stream (/chat) and the transcript APIs (/api/transcripts…)
  to the internal agent backend (the sample overlay separately binds the
  backend API to loopback; there is no frontend search proxy)
- Report liveness (/healthz) and readiness (/readyz)

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

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from html.parser import HTMLParser
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlencode, urlsplit
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starters import CATEGORIES, SEARCH_STARTERS

logger = logging.getLogger(__name__)

AGENT_BACKEND_HOST = os.environ.get("AGENT_BACKEND_HOST", "agent")
AGENT_BACKEND_PORT = os.environ.get("AGENT_BACKEND_PORT", "8000")
# Plain HTTP: the default deployment reaches the agent on private compose networks.
AGENT_BASE_URL = f"http://{AGENT_BACKEND_HOST}:{AGENT_BACKEND_PORT}"  # NOSONAR
CHAT_ENDPOINT = f"{AGENT_BASE_URL}/chat"
TRANSCRIPTS_ENDPOINT = f"{AGENT_BASE_URL}/transcripts"
SEARCH_ENDPOINT = f"{AGENT_BASE_URL}/search"
# Internal-only: the agent's landing-desk payload. Rendered into the home page
# server-side; never proxied, so no public API exposes the newest list.
LATEST_ENDPOINT = f"{AGENT_BASE_URL}/latest"

STATIC_DIR = Path(__file__).parent / "static"
PAGE_TEMPLATE = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
SITE_NAME = "睡前消息知识库"
SITE_DESCRIPTION = "在同一处阅读、全文搜索睡前消息文稿，并随时向知识库 Agent 提问。"
CHANNEL_ORDER = (
    "ShuiQianXiaoXi",
    "CanKaoXinXi",
    "JiangDianHeiHua",
    "GaoJian",
    "ChanJingPoBiJi",
)
CHANNEL_LABELS = {
    "ShuiQianXiaoXi": "睡前消息",
    "CanKaoXinXi": "参考信息",
    "JiangDianHeiHua": "讲点黑话",
    "GaoJian": "高见",
    "ChanJingPoBiJi": "产经破壁机",
}
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_public_base_url(value: str) -> str:
    """Return a trusted public origin, never a request-controlled Host value."""
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_BASE_URL must be an HTTP(S) origin without a path")
    return f"{parsed.scheme}://{parsed.netloc}"


PUBLIC_BASE_URL = _normalize_public_base_url(
    os.environ.get("PUBLIC_BASE_URL", "https://bedtime.blog")
)

# Shared upstream client: reuses connections to the agent across requests
# instead of paying a new connection pool + TCP handshake per chat.
_client: httpx.AsyncClient | None = None

# The agent's uvicorn drops idle keep-alive connections after 5s (its default
# timeout_keep_alive). httpx also defaults to 5s, so a request could pick a
# pooled connection the agent was closing at that moment and fail with
# "Server disconnected without sending a response". Expiring ours first avoids
# that race; _get_upstream retries the rare leftover case for idempotent GETs.
UPSTREAM_KEEPALIVE_EXPIRY = 3.0


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(keepalive_expiry=UPSTREAM_KEEPALIVE_EXPIRY),
    )
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


# No interactive API docs: every path here is public, and the generated
# /docs, /redoc and /openapi.json pages serve no reader.
app = FastAPI(
    title="睡前消息知识库",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Compress text assets. Starlette excludes text/event-stream from compression,
# which is what keeps the /chat stream flushing event-by-event.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Legacy twin of CSP frame-ancestors 'none', for browsers without it.
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Data endpoints are fetched by pages, never landed on: keep them out of
    # search indexes without blocking crawlers that render the pages.
    if request.url.path.startswith("/api/"):
        response.headers["X-Robots-Tag"] = "noindex"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        "connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


class HeadAsGetMiddleware:
    """Answer HEAD exactly like GET, minus the body.

    Routes are registered for GET only, so without this a HEAD fell through to
    the static mount: a 404 for every SSR page, robots.txt and sitemap.xml, and
    the bare static shell for "/". Crawlers and link checkers that probe with
    HEAD then treat live pages as missing. Rewriting the method lets HEAD share
    the GET route's status, ETag, Content-Type and Content-Length.

    Added last so it is the outermost layer and every other middleware (gzip
    included) sees a GET.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return

        async def send_without_body(message: Message) -> None:
            if message["type"] == "http.response.body":
                if message.get("more_body", False):
                    return
                message = {"type": "http.response.body", "body": b""}
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, send_without_body)


app.add_middleware(HeadAsGetMiddleware)


def _sse_error(message: str) -> bytes:
    """Emit a terminal SSE error event the frontend understands, then close."""
    event = json.dumps({"type": "error", "content": message}, ensure_ascii=False)
    return f"data: {event}\n\ndata: [DONE]\n\n".encode()


def _resolve_version() -> str:
    """What to show in the masthead.

    APP_VERSION is set by compose to APP_IMAGE_TAG, falling back to IMAGE_TAG,
    so a deployed release reports the tag actually running rather than whatever
    the source tree last declared. Falling back to the installed package keeps a bare
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


class _UpstreamError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class _BodyTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _absolute_url(path: str) -> str:
    return f"{PUBLIC_BASE_URL}{path}"


def _channel_path(channel: str) -> str:
    return f"/transcripts?{urlencode({'channel': channel})}"


def _transcript_path(doc_id: str) -> str:
    encoded = "/".join(quote(part, safe="") for part in doc_id.split("/"))
    return f"/transcripts/{encoded}"


# ---------------------------------------------------------------- latest desk
# The home page renders the agent's in-memory latest-desk payload (fixed size,
# rebuilt per snapshot) straight into the HTML: no public endpoint, and the
# landing load makes zero transcript requests. A 30 s micro-cache here only
# absorbs request bursts; freshness is the agent's snapshot switch.
LATEST_DESK_TTL_SECONDS = 30.0
_latest_desk_html_cache: dict[str, Any] = {}


def _render_latest_desk(items: list[dict[str, Any]]) -> str:
    """The landing desk markup, mirroring the reader projection's anchors."""
    if not items:
        return '<p class="latest-state">暂无文稿。</p>'
    rows = []
    for item in items:
        doc_id = str(item.get("doc_id") or "")
        path = escape(_transcript_path(doc_id), quote=True)
        title = item.get("source_title") or item.get("canonical_title") or doc_id
        meta = item.get("publication_date") or ""
        meta_html = (
            f'<span class="latest-item-meta">{escape(str(meta))}</span>' if meta else ""
        )
        heading_links = []
        for heading in item.get("headings") or []:
            text = str(heading.get("text") or "")
            if not text:
                continue
            anchor = str(heading.get("anchor") or "")
            fragment = f"#{quote(anchor, safe='')}" if anchor else ""
            heading_links.append(
                f'<li><a href="{path}{fragment}" data-route="">{escape(text)}</a></li>'
            )
        rows.append(
            f'<li class="latest-item">'
            f'<a class="latest-item-link" href="{path}" data-route="">'
            f'<span class="latest-item-title">{escape(str(title))}</span>{meta_html}</a>'
            f'<ul class="latest-item-headings">{"".join(heading_links)}</ul>'
            f"</li>"
        )
    end_chips = "".join(
        f'<a class="latest-end-chip" href="/transcripts?channel={channel}"'
        f' data-route="">{escape(CHANNEL_LABELS[channel])}</a>'
        for channel in CHANNEL_ORDER
    )
    return (
        f'<ol class="latest-list" id="latest-list">{"".join(rows)}</ol>'
        f'<div class="latest-end" id="latest-end">'
        f'<p class="latest-end-label">浏览全部文稿</p>'
        f'<div class="latest-end-chips" id="latest-end-chips">{end_chips}</div>'
        f"</div>"
    )


async def _latest_desk_html() -> str:
    """Rendered desk from the agent, or the last one on upstream failure."""
    cached = _latest_desk_html_cache.get("html")
    if cached and (
        time.monotonic() - _latest_desk_html_cache.get("at", 0.0)
        < LATEST_DESK_TTL_SECONDS
    ):
        return cached
    try:
        payload = await _fetch_upstream_json(LATEST_ENDPOINT)
        items = payload.get("items") or []
        html = _render_latest_desk(items)
        _latest_desk_html_cache.clear()
        _latest_desk_html_cache["at"] = time.monotonic()
        _latest_desk_html_cache["html"] = html
        _latest_desk_html_cache["items"] = items
        return html
    except _UpstreamError:
        # A stale desk beats an empty one; the note only shows when nothing
        # has ever rendered.
        if cached:
            return cached
        return '<p class="latest-state">文稿目录暂时不可用，请稍后重试。</p>'


def _latest_item_list_json_ld() -> dict[str, Any] | None:
    """ItemList for /latest, from the items behind the desk just rendered."""
    items = _latest_desk_html_cache.get("items") or []
    elements = []
    for position, item in enumerate(items, start=1):
        doc_id = str(item.get("doc_id") or "")
        if not doc_id:
            continue
        elements.append(
            {
                "@type": "ListItem",
                "position": position,
                "url": _absolute_url(_transcript_path(doc_id)),
                "name": str(
                    item.get("source_title") or item.get("canonical_title") or doc_id
                ),
            }
        )
    if not elements:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "最新文稿",
        "itemListOrder": "https://schema.org/ItemListOrderDescending",
        "numberOfItems": len(elements),
        "itemListElement": elements,
    }


def _validate_transcript_uri(doc_id: str) -> str:
    if not doc_id or "\x00" in doc_id or "\\" in doc_id:
        raise ValueError("invalid transcript URI")
    if any(part in {"", ".", ".."} for part in doc_id.split("/")):
        raise ValueError("invalid transcript URI")
    path = PurePosixPath(doc_id)
    if path.is_absolute() or path.suffix.lower() != ".md":
        raise ValueError("invalid transcript URI")
    return path.as_posix()


# Bitcoin/IPFS base58 alphabet: drops 0/O/I/l so short codes never mix up
# visually similar characters when read aloud or retyped.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_SET = frozenset(_BASE58_ALPHABET)
SHORT_ID_LENGTH = 8


def _base58_encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    if n == 0:
        return _BASE58_ALPHABET[0]
    digits = []
    while n:
        n, remainder = divmod(n, 58)
        digits.append(_BASE58_ALPHABET[remainder])
    return "".join(reversed(digits))


def _short_id_for(doc_id: str) -> str:
    """Deterministic short code for a doc_id: no lookup table to maintain,
    no state that can drift from the source of truth. Derived purely from
    the URI, so renaming a document's path also changes its short link."""
    digest = hashlib.sha256(doc_id.encode()).digest()
    code = _base58_encode(digest[:6])
    if len(code) < SHORT_ID_LENGTH:
        code = _BASE58_ALPHABET[0] * (SHORT_ID_LENGTH - len(code)) + code
    return code[-SHORT_ID_LENGTH:]


def _etag_for_bytes(content: bytes) -> str:
    return f'"{hashlib.sha256(content).hexdigest()}"'


def _etag_matches(request: Request, etag: str) -> bool:
    supplied = request.headers.get("if-none-match", "")
    return supplied == "*" or etag in {part.strip() for part in supplied.split(",")}


def _generated_response(
    request: Request,
    content: str,
    *,
    media_type: str,
    status_code: int = 200,
    cache_control: str = "no-cache",
) -> Response:
    encoded = content.encode("utf-8")
    etag = _etag_for_bytes(encoded)
    headers = {"Cache-Control": cache_control, "ETag": etag}
    if status_code == 200 and _etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=encoded,
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


async def _get_upstream(
    client: httpx.AsyncClient,
    url: str,
    headers: dict | None = None,
    params: dict | None = None,
) -> httpx.Response:
    """GET from the agent, retrying once if a reused connection was already closed.

    Safe to retry because these reads are idempotent. Other transport errors
    propagate so callers can answer with a controlled 503.
    """
    try:
        return await client.get(url, headers=headers, params=params)
    except httpx.RemoteProtocolError:
        return await client.get(url, headers=headers, params=params)


async def _fetch_upstream_json(url: str) -> dict:
    client = _client
    if client is None:
        raise _UpstreamError(503, "文稿服务尚未就绪")
    try:
        response = await _get_upstream(client, url)
    except httpx.TransportError as exc:
        raise _UpstreamError(503, "文稿服务暂时不可用") from exc
    if response.status_code == 404:
        raise _UpstreamError(404, "文稿不存在")
    if response.status_code != 200:
        raise _UpstreamError(503, "文稿服务暂时不可用")
    try:
        payload = response.json()
    except ValueError as exc:
        raise _UpstreamError(503, "文稿服务返回无效数据") from exc
    if not isinstance(payload, dict):
        raise _UpstreamError(503, "文稿服务返回无效数据")
    return payload


def _index_items(payload: dict) -> list[dict]:
    items = payload.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise _UpstreamError(503, "文稿目录返回无效数据")
    return items


_SHORT_ID_CACHE: dict[str, str] | None = None
_SHORT_ID_BUILT_AT = 0.0
# A forced rebuild costs a full index query, and any well-formed unknown short
# ID asks for one; this bounds that to one upstream query per interval.
SHORT_ID_MIN_REFRESH_SECONDS = 60.0
_now = time.monotonic


async def _short_id_map(*, force_refresh: bool = False) -> dict[str, str]:
    """short_id -> doc_id, rebuilt from the live transcript index.

    There is no persisted mapping: short IDs are a pure function of doc_id
    (see _short_id_for), so reversing one just means hashing every known
    doc_id and matching. Kept in memory indefinitely rather than on a timer —
    at this corpus size (~2000 docs) the whole map is a few hundred KB, so
    the only reason to rebuild is a short_id the cached map doesn't have,
    which a newly published document would trigger on its first lookup.
    Forced rebuilds are rate-limited by SHORT_ID_MIN_REFRESH_SECONDS.
    """
    global _SHORT_ID_CACHE, _SHORT_ID_BUILT_AT
    if _SHORT_ID_CACHE is not None and (
        not force_refresh or _now() - _SHORT_ID_BUILT_AT < SHORT_ID_MIN_REFRESH_SECONDS
    ):
        return _SHORT_ID_CACHE
    items = _index_items(await _fetch_upstream_json(TRANSCRIPTS_ENDPOINT))
    mapping: dict[str, str] = {}
    for item in items:
        try:
            doc_id = _validate_transcript_uri(str(item.get("doc_id") or ""))
        except ValueError:
            continue
        mapping[_short_id_for(doc_id)] = doc_id
    _SHORT_ID_CACHE = mapping
    _SHORT_ID_BUILT_AT = _now()
    return mapping


def _display_title(item: dict) -> str:
    return str(
        item.get("source_title")
        or item.get("canonical_title")
        or item.get("doc_id")
        or "文稿"
    )


_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _markdown_filename(title: str) -> str:
    stem = _UNSAFE_FILENAME.sub("", title).strip() or "transcript"
    if not stem.lower().endswith(".md"):
        stem = f"{stem}.md"
    return stem


def _content_disposition(filename: str) -> str:
    return f"attachment; filename=\"transcript.md\"; filename*=UTF-8''{quote(filename)}"


def _description_from_body(body_html: str, title: str) -> str:
    parser = _BodyTextExtractor()
    parser.feed(body_html)
    parser.close()
    text = " ".join(" ".join(parser.parts).split())
    if not text:
        return f"阅读《{title}》完整文稿。"
    excerpt = text[:160].rstrip()
    if len(text) > len(excerpt):
        excerpt += "…"
    return excerpt


def _safe_json_script(payload: dict) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        serialized.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        # Written as escapes: the raw LINE/PARAGRAPH SEPARATOR characters are
        # invisible and easily stripped by an editor or formatter.
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _valid_date(value: object) -> str | None:
    if not isinstance(value, str) or not ISO_DATE_RE.fullmatch(value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _lastmod(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if result := _valid_date(value):
        return result
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _render_channel_nav(active_channel: str | None = None) -> str:
    links = []
    for channel in CHANNEL_ORDER:
        current = "true" if channel == active_channel else "false"
        links.append(
            f'<a class="channel-chip" data-channel="{escape(channel, quote=True)}" '
            f'href="{escape(_channel_path(channel), quote=True)}" '
            f'aria-current="{current}">{escape(CHANNEL_LABELS[channel])}</a>'
        )
    return "".join(links)


def _archive_sort_key(item: dict) -> tuple[bool, str, str]:
    publication_date = _valid_date(item.get("publication_date")) or ""
    return bool(publication_date), publication_date, str(item.get("doc_id") or "")


def _render_archive_groups(items: list[dict], channel: str) -> str:
    scoped = sorted(
        (item for item in items if item.get("channel") == channel),
        key=_archive_sort_key,
        reverse=True,
    )
    rows = []
    for item in scoped:
        try:
            doc_id = _validate_transcript_uri(str(item.get("doc_id") or ""))
        except ValueError as exc:
            raise _UpstreamError(503, "文稿目录含无效 URI") from exc
        date_value = _valid_date(item.get("publication_date"))
        time_html = ""
        if date_value:
            escaped_date = escape(date_value)
            time_html = f'<time datetime="{escaped_date}">{escaped_date}</time>'
        href = escape(_transcript_path(doc_id), quote=True)
        label = escape(_display_title(item))
        rows.append(
            f'<li><a href="{href}"><span class="archive-item-title">'
            f"{label}</span>{time_html}</a></li>"
        )
    return f'<section class="archive-group"><ol class="archive-list">{"".join(rows)}</ol></section>'


SITE_LOGO_WIDTH = 128
SITE_LOGO_HEIGHT = 128


def _seo_head(
    *,
    title: str,
    description: str,
    canonical_url: str,
    og_type: str,
    article: dict | None = None,
    extra_json_ld: list[dict] | None = None,
    noindex: bool = False,
) -> str:
    image_url = _absolute_url("/bedtimenews.webp")
    tags = []
    if noindex:
        # A noindex page names no canonical: pointing elsewhere while asking
        # not to be indexed sends search engines two conflicting signals.
        tags.append('<meta name="robots" content="noindex" />')
    else:
        tags.append(
            f'<link rel="canonical" href="{escape(canonical_url, quote=True)}" />'
        )
    tags.extend(
        [
            f'<meta property="og:site_name" content="{escape(SITE_NAME, quote=True)}" />',
            f'<meta property="og:title" content="{escape(title, quote=True)}" />',
            f'<meta property="og:description" content="{escape(description, quote=True)}" />',
            f'<meta property="og:type" content="{escape(og_type, quote=True)}" />',
            f'<meta property="og:url" content="{escape(canonical_url, quote=True)}" />',
            '<meta property="og:locale" content="zh_CN" />',
            f'<meta property="og:image" content="{escape(image_url, quote=True)}" />',
            '<meta property="og:image:type" content="image/webp" />',
            f'<meta property="og:image:width" content="{SITE_LOGO_WIDTH}" />',
            f'<meta property="og:image:height" content="{SITE_LOGO_HEIGHT}" />',
            '<meta name="twitter:card" content="summary" />',
            f'<meta name="twitter:title" content="{escape(title, quote=True)}" />',
            f'<meta name="twitter:description" content="{escape(description, quote=True)}" />',
            f'<meta name="twitter:image" content="{escape(image_url, quote=True)}" />',
        ]
    )
    if article:
        if published := _valid_date(article.get("publication_date")):
            tags.append(
                f'<meta property="article:published_time" content="{escape(published, quote=True)}" />'
            )
        if modified := _lastmod(article.get("updated_at")):
            tags.append(
                f'<meta property="article:modified_time" content="{escape(modified, quote=True)}" />'
            )
        tags.append(
            '<script type="application/ld+json">'
            + _safe_json_script(article["json_ld"])
            + "</script>"
        )
    tags.extend(
        '<script type="application/ld+json">' + _safe_json_script(extra) + "</script>"
        for extra in extra_json_ld or []
    )
    return "\n  ".join(tags)


def _breadcrumb_json_ld(*crumbs: tuple[str, str]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": position,
                "name": name,
                "item": _absolute_url(path),
            }
            for position, (name, path) in enumerate(crumbs, start=1)
        ],
    }


@dataclass(frozen=True)
class _ArchiveShell:
    title: str = ""
    groups: str = ""
    visible: bool = False


@dataclass(frozen=True)
class _ReaderShell:
    title: str = ""
    body: str = ""
    state: str = ""
    visible: bool = False


# Frozen, so one shared instance is safe as the default for _render_shell.
_NO_ARCHIVE = _ArchiveShell()
_NO_READER = _ReaderShell()


def _render_shell(
    *,
    title: str,
    description: str,
    canonical_path: str,
    og_type: str = "website",
    active_channel: str | None = None,
    body_view: str = "chat",
    body_panel: str = "",
    ssr_doc_id: str = "",
    archive: _ArchiveShell = _NO_ARCHIVE,
    reader: _ReaderShell = _NO_READER,
    article: dict | None = None,
    extra_json_ld: list[dict] | None = None,
    noindex: bool = False,
    latest_desk: str = "",
    search_query: str | None = None,
) -> str:
    canonical_url = _absolute_url(canonical_path)
    replacements = {
        "__PAGE_TITLE__": escape(title),
        "__PAGE_DESCRIPTION__": escape(description, quote=True),
        "__PUBLIC_BASE_URL__": escape(PUBLIC_BASE_URL, quote=True),
        "<!--SSR_HEAD-->": _seo_head(
            title=title,
            description=description,
            canonical_url=canonical_url,
            og_type=og_type,
            article=article,
            extra_json_ld=extra_json_ld,
            noindex=noindex,
        ),
        "__BODY_VIEW__": escape(body_view, quote=True),
        "__BODY_PANEL__": escape(body_panel, quote=True),
        "__BODY_MODE__": "chat" if search_query is None else "search",
        "__SEARCH_HIDDEN__": "hidden" if search_query is None else "",
        "__SEARCH_IDLE__": "" if search_query else " is-idle",
        "__SEARCH_QUERY__": escape(search_query or "", quote=True),
        "__SSR_DOC_ID__": escape(ssr_doc_id, quote=True),
        "__CHANNEL_NAV__": _render_channel_nav(active_channel),
        "__READING_INERT__": "" if body_view == "browse" else "inert",
        "__ARCHIVE_HIDDEN__": "" if archive.visible else "hidden",
        "__ARCHIVE_TITLE__": escape(archive.title),
        "__ARCHIVE_GROUPS__": archive.groups,
        "__ARCHIVE_STATE_HIDDEN__": "hidden" if archive.groups else "",
        "__ARCHIVE_STATE__": "" if archive.groups else "此栏目暂无文稿。",
        "__READER_HIDDEN__": "" if reader.visible else "hidden",
        "__READER_TITLE__": escape(reader.title),
        "__READER_BODY__": reader.body,
        "__READER_STATE__": escape(reader.state),
        "__LATEST_HIDDEN__": "" if latest_desk else "hidden",
        "__LATEST_DESK__": latest_desk,
    }
    rendered = PAGE_TEMPLATE
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


async def _render_error(status_code: int, message: str, path: str = "/") -> str:
    title = "文稿未找到" if status_code == 404 else "文稿服务暂时不可用"
    return _render_shell(
        title=f"{title} · {SITE_NAME}",
        description=message,
        canonical_path=path,
        body_view="browse",
        body_panel="reader",
        latest_desk=await _latest_desk_html(),
        reader=_ReaderShell(title=title, state=message, visible=True),
        noindex=True,
    )


def _render_sitemap(items: list[dict]) -> str:
    documents: dict[str, dict] = {}
    for item in items:
        try:
            doc_id = _validate_transcript_uri(str(item.get("doc_id") or ""))
        except ValueError as exc:
            raise _UpstreamError(503, "文稿目录含无效 URI") from exc
        documents[doc_id] = item

    valid_channels = [
        channel
        for channel in CHANNEL_ORDER
        if any(item.get("channel") == channel for item in documents.values())
    ]
    all_lastmods = [
        value
        for item in documents.values()
        if (value := _lastmod(item.get("updated_at")))
    ]
    latest = max(all_lastmods, default=None)
    urls: list[tuple[str, str | None]] = [
        (_absolute_url("/"), latest),
        (_absolute_url("/latest"), latest),
    ]
    for channel in valid_channels:
        channel_lastmods = [
            value
            for item in documents.values()
            if item.get("channel") == channel
            and (value := _lastmod(item.get("updated_at")))
        ]
        urls.append(
            (
                _absolute_url(_channel_path(channel)),
                max(channel_lastmods, default=None),
            )
        )
    urls.extend(
        (
            _absolute_url(_transcript_path(doc_id)),
            _lastmod(documents[doc_id].get("updated_at")),
        )
        for doc_id in sorted(documents)
    )

    entries = []
    for location, lastmod in urls:
        parts = [f"    <loc>{xml_escape(location)}</loc>"]
        if lastmod:
            parts.append(f"    <lastmod>{xml_escape(lastmod)}</lastmod>")
        entries.append("  <url>\n" + "\n".join(parts) + "\n  </url>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )


# Instance label of this deployment (the compose project name in blue-green
# deployments), reported by /healthz so a deployment tool can tell instances
# apart. Defaults to "local" for a single self-hosted instance.
APP_INSTANCE = os.environ.get("APP_INSTANCE", "").strip() or "local"

# /readyz waits at most this long for the agent's /health, which is served
# from memory and answers in milliseconds when the agent is up.
READY_TIMEOUT_S = 3.0
HEALTH_ENDPOINT = f"{AGENT_BASE_URL}/health"


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness of this web process alone.

    It never calls the agent: a proxy or orchestrator restarting `web` because
    the agent or database is down would only make things worse. Readiness of
    the whole instance is /readyz.
    """
    return JSONResponse(
        {"status": "ok", "version": APP_VERSION, "instance": APP_INSTANCE},
        headers={"Cache-Control": "no-store"},
    )


def _trimmed_readiness(payload: object) -> dict:
    """Keep only what a public caller may see of the agent's /health body.

    The full body includes indexer_status.last_error, which can carry internal
    error text, so it is never forwarded; /readyz is reachable through the
    public proxy like every other path on web.
    """
    body = payload if isinstance(payload, dict) else {}
    snapshot = body.get("snapshot") if isinstance(body.get("snapshot"), dict) else None
    trimmed: dict = {
        "ready": bool(body.get("ready")),
        "snapshot": (
            {
                key: snapshot.get(key)
                for key in ("id", "format_version", "embedding_space")
            }
            if snapshot
            else None
        ),
    }
    if not trimmed["ready"]:
        reason = body.get("reason")
        trimmed["reason"] = reason if isinstance(reason, str) else "agent not ready"
    return trimmed


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness of this instance: the status code of its agent's /health.

    200 when the agent reaches the database and has a readable RAG snapshot
    selected, otherwise 503. The body is trimmed (see _trimmed_readiness).
    Deployment tools gate traffic on this; it is cheap to poll because the
    agent serves /health from memory.
    """
    headers = {"Cache-Control": "no-store"}
    client = _client
    if client is None:
        return JSONResponse(
            {"ready": False, "snapshot": None, "reason": "web is starting"},
            status_code=503,
            headers=headers,
        )
    try:
        response = await client.get(HEALTH_ENDPOINT, timeout=READY_TIMEOUT_S)
    except httpx.HTTPError:
        return JSONResponse(
            {"ready": False, "snapshot": None, "reason": "agent unreachable"},
            status_code=503,
            headers=headers,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    trimmed = _trimmed_readiness(payload)
    ready = response.status_code == 200 and trimmed["ready"]
    if not ready:
        trimmed["ready"] = False
        trimmed.setdefault("reason", f"agent /health returned {response.status_code}")
    return JSONResponse(trimmed, status_code=200 if ready else 503, headers=headers)


@app.get("/api/starters")
async def get_starters() -> JSONResponse:
    """Sample questions plus the keyword-search starters for the empty state."""
    return JSONResponse({"categories": CATEGORIES, "search": SEARCH_STARTERS})


# A ChatRequest holds at most a 2000-character question and eight prior
# turns of 2000 + 1000 characters: well under 128 KiB of UTF-8. Anything
# larger is not a chat request and is refused before it reaches the agent.
MAX_CHAT_BODY_BYTES = 128 * 1024


async def _read_capped_body(request: Request, limit: int) -> bytes | None:
    """The request body, or None as soon as it exceeds ``limit`` bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for part in request.stream():
        body += part
        if len(body) > limit:
            return None
    return bytes(body)


@app.post("/chat", response_model=None)
async def chat(request: Request) -> Response:
    """Proxy the chat SSE stream from the internal agent to the browser."""
    body = await _read_capped_body(request, MAX_CHAT_BODY_BYTES)
    if body is None:
        return JSONResponse({"detail": "问题过长。"}, status_code=413)

    async def event_stream() -> AsyncGenerator[bytes]:
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
        except Exception:  # the client still gets a terminal event
            # The exception text stays in the log: it can name internal hosts
            # and ports, which a public error message must not.
            logger.exception("Chat stream from the agent failed")
            yield _sse_error("信号中断，请稍后重试。")

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
        response = await _get_upstream(client, upstream_url, headers=headers)
    except httpx.TransportError:
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


@app.get("/api/search")
async def keyword_search(
    request: Request,
    q: Annotated[str, Query(max_length=512)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=16384)] = None,
    sort: Annotated[Literal["relevance", "date"], Query()] = "relevance",
    channel: Annotated[
        str | None, Query(max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    ] = None,
    year: Annotated[int | None, Query(ge=1900, le=2100)] = None,
) -> Response:
    """Proxy the agent's keyword search for the 搜索 pane.

    Status codes pass through untouched — the pane distinguishes 410 (stale
    cursor: search again) from 422 (query problem) from 503 (service down).
    """
    client = _client
    if client is None:
        return JSONResponse(
            {"detail": "搜索服务尚未就绪"},
            status_code=503,
            headers={"Cache-Control": "no-cache"},
        )
    params: dict[str, str] = {"q": q, "limit": str(limit), "sort": sort}
    if channel is not None:
        params["channel"] = channel
    if year is not None:
        params["year"] = str(year)
    if cursor is not None:
        params["cursor"] = cursor
    try:
        response = await _get_upstream(client, SEARCH_ENDPOINT, params=params)
    except httpx.TransportError:
        return JSONResponse(
            {"detail": "搜索服务暂时不可用，请稍后重试。"},
            status_code=503,
            headers={"Cache-Control": "no-cache"},
        )
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
        headers={"Cache-Control": "no-cache"},
    )


# Clicks go to stdout on their own handler: the service configures no root
# logging, which would otherwise drop INFO records.
_click_logger = logging.getLogger("search.clicks")
_click_logger.setLevel(logging.INFO)
_click_logger.propagate = False
if not _click_logger.handlers:
    _click_handler = logging.StreamHandler()
    _click_handler.setFormatter(logging.Formatter("%(message)s"))
    _click_logger.addHandler(_click_handler)
SEARCH_CLICK_MAX_BYTES = 2048


class SearchClick(BaseModel):
    """One opened search result, as the 搜索 pane reports it. Anonymous: no
    identity, address or session is accepted or logged."""

    model_config = {"extra": "forbid"}
    q: str = Field(min_length=1, max_length=512)
    doc_id: str = Field(min_length=1, max_length=500, pattern=r"^[A-Za-z0-9_./-]+\.md$")
    rank: int = Field(ge=1, le=10000)
    match: Literal["keyword", "semantic", "both"]
    mode: Literal["keyword", "hybrid"]
    snapshot_id: str = Field(max_length=128, pattern=r"^[A-Za-z0-9_]*$")


@app.post("/api/search/click", status_code=204)
async def search_click(request: Request) -> Response:
    """Log one result click as a JSON line for tuning (scripts/search_clicks.py)."""
    body = await request.body()
    if len(body) > SEARCH_CLICK_MAX_BYTES:
        return Response(status_code=413)
    try:
        click = SearchClick.model_validate_json(body)
    except ValueError:
        return Response(status_code=422)
    _click_logger.info(
        "search_click %s", json.dumps(click.model_dump(), ensure_ascii=False)
    )
    return Response(status_code=204)


@app.get("/api/transcripts")
async def transcript_index(request: Request) -> Response:
    return await _proxy_transcript_json(request, TRANSCRIPTS_ENDPOINT)


async def _transcript_markdown_download(upstream_url: str) -> Response:
    try:
        article = await _fetch_upstream_json(upstream_url)
    except _UpstreamError as exc:
        status_code = 404 if exc.status_code == 404 else 503
        message = (
            "这篇文稿不存在。"
            if status_code == 404
            else "文稿服务暂时不可用，请稍后重试。"
        )
        return JSONResponse(
            {"detail": message},
            status_code=status_code,
            headers={"Cache-Control": "no-cache"},
        )
    markdown = article.get("body_markdown")
    if not isinstance(markdown, str) or not markdown:
        return JSONResponse(
            {"detail": "文稿源暂不可用"},
            status_code=404,
            headers={"Cache-Control": "no-cache"},
        )
    filename = _markdown_filename(_display_title(article))
    return Response(
        content=markdown.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Content-Disposition": _content_disposition(filename),
        },
    )


@app.get("/api/transcripts/{doc_id:path}")
async def transcript_detail(
    doc_id: str,
    request: Request,
    download: Annotated[str | None, Query()] = None,
) -> Response:
    # Validate before building the upstream URL: a path such as "../health"
    # would otherwise be normalized by httpx into a request for another agent
    # endpoint, reachable from the public internet through this proxy.
    try:
        canonical = _validate_transcript_uri(doc_id)
    except ValueError:
        return JSONResponse(
            {"detail": "文稿不存在"},
            status_code=404,
            headers={"Cache-Control": "no-cache"},
        )
    upstream = f"{TRANSCRIPTS_ENDPOINT}/{quote(canonical, safe='/')}"
    if download == "markdown":
        return await _transcript_markdown_download(upstream)
    return await _proxy_transcript_json(request, upstream)


@app.get("/", include_in_schema=False)
async def home(request: Request) -> Response:
    website_json_ld = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": SITE_NAME,
        "description": SITE_DESCRIPTION,
        "url": _absolute_url("/"),
        "inLanguage": "zh-CN",
    }
    html = _render_shell(
        title=SITE_NAME,
        description=SITE_DESCRIPTION,
        canonical_path="/",
        extra_json_ld=[website_json_ld],
        latest_desk=await _latest_desk_html(),
    )
    return _generated_response(request, html, media_type="text/html")


SEARCH_QUERY_MAX = 512


@app.get("/search", include_in_schema=False)
async def search_page(
    request: Request, q: Annotated[str | None, Query()] = None
) -> Response:
    # The 搜索 tab at its own address. With ?q= the page opens with the query
    # in the box and the client runs it at once, like a results page. The
    # results themselves come from /api/search, so result pages stay noindex.
    query = (q or "").strip()[:SEARCH_QUERY_MAX]
    title = f"{query} - 搜索 · {SITE_NAME}" if query else f"搜索 · {SITE_NAME}"
    html = _render_shell(
        title=title,
        description=f"按关键词搜索{SITE_NAME}的全部文稿正文。",
        canonical_path="/search",
        latest_desk=await _latest_desk_html(),
        noindex=bool(query),
        search_query=query,
    )
    return _generated_response(request, html, media_type="text/html")


@app.get("/opensearch.xml", include_in_schema=False)
async def opensearch_description(request: Request) -> Response:
    # Lets browsers offer bedtime.blog as a site search (e.g. Chrome's
    # "Tab to search"), pointing at the /search?q= results page.
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/"'
        ' xmlns:moz="http://www.mozilla.org/2006/browser/search/">\n'
        f"  <ShortName>{escape(SITE_NAME)}</ShortName>\n"
        f"  <Description>按关键词搜索{escape(SITE_NAME)}的全部文稿正文</Description>\n"
        "  <InputEncoding>UTF-8</InputEncoding>\n"
        f'  <Image width="{SITE_LOGO_WIDTH}" height="{SITE_LOGO_HEIGHT}"'
        f' type="image/webp">{escape(_absolute_url("/bedtimenews.webp"))}</Image>\n'
        '  <Url type="text/html" method="get"'
        f' template="{escape(_absolute_url("/search"))}?q={{searchTerms}}"/>\n'
        f"  <moz:SearchForm>{escape(_absolute_url('/search'))}</moz:SearchForm>\n"
        "</OpenSearchDescription>\n"
    )
    return _generated_response(
        request, body, media_type="application/opensearchdescription+xml"
    )


@app.get("/latest", include_in_schema=False)
async def latest_page(request: Request) -> Response:
    # The same desk the home page shows beside the chat, at its own address so
    # it can be linked and opened directly (mobile lands on the list itself).
    breadcrumb = _breadcrumb_json_ld(("首页", "/"), ("最新文稿", "/latest"))
    desk = await _latest_desk_html()
    item_list = _latest_item_list_json_ld()
    html = _render_shell(
        title=f"最新文稿 · {SITE_NAME}",
        description=f"{SITE_NAME}最近发布的文稿，按发布日期倒序排列。",
        canonical_path="/latest",
        latest_desk=desk,
        body_view="browse",
        body_panel="latest",
        extra_json_ld=[breadcrumb, item_list] if item_list else [breadcrumb],
    )
    return _generated_response(request, html, media_type="text/html")


@app.get("/index.html", include_in_schema=False)
async def index_redirect() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=308)


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt(request: Request) -> Response:
    content = f"User-agent: *\nAllow: /\nSitemap: {_absolute_url('/sitemap.xml')}\n"
    return _generated_response(
        request,
        content,
        media_type="text/plain",
        cache_control="public, max-age=3600, must-revalidate",
    )


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request) -> Response:
    try:
        items = _index_items(await _fetch_upstream_json(TRANSCRIPTS_ENDPOINT))
        sitemap = _render_sitemap(items)
    except _UpstreamError:
        return PlainTextResponse(
            "Sitemap temporarily unavailable\n",
            status_code=503,
            headers={"Cache-Control": "no-cache"},
        )
    return _generated_response(
        request,
        sitemap,
        media_type="application/xml",
        cache_control="public, max-age=300, must-revalidate",
    )


@app.get("/s/{short_id}", include_in_schema=False)
async def short_link(short_id: str) -> Response:
    if len(short_id) != SHORT_ID_LENGTH or not _BASE58_SET.issuperset(short_id):
        return PlainTextResponse("文稿不存在", status_code=404)
    try:
        mapping = await _short_id_map()
        doc_id = mapping.get(short_id)
        if doc_id is None:
            # Not in the resident cache — could be a document published
            # since the cache was built. Rebuild (rate-limited) before giving up.
            mapping = await _short_id_map(force_refresh=True)
            doc_id = mapping.get(short_id)
    except _UpstreamError:
        return PlainTextResponse("文稿服务暂时不可用", status_code=503)
    if doc_id is None:
        return PlainTextResponse("文稿不存在", status_code=404)
    # 302, not 301: a renamed doc_id changes its short_id, so an old short
    # link should be free to start 404ing rather than stick around cached.
    return RedirectResponse(url=_transcript_path(doc_id), status_code=302)


@app.get("/transcripts", include_in_schema=False)
async def transcript_archive(
    request: Request,
    channel: Annotated[list[str] | None, Query()] = None,
) -> Response:
    if channel is None:
        return RedirectResponse(url="/", status_code=308)
    if len(channel) != 1 or channel[0] not in CHANNEL_LABELS:
        html = await _render_error(404, "这个栏目不存在。", "/transcripts")
        return _generated_response(
            request, html, media_type="text/html", status_code=404
        )
    selected = channel[0]
    canonical_path = _channel_path(selected)
    try:
        items = _index_items(await _fetch_upstream_json(TRANSCRIPTS_ENDPOINT))
        scoped = [item for item in items if item.get("channel") == selected]
        if not scoped:
            raise _UpstreamError(404, "栏目不存在")
        groups = _render_archive_groups(items, selected)
    except _UpstreamError as exc:
        status_code = 404 if exc.status_code == 404 else 503
        message = (
            "这个栏目不存在。"
            if status_code == 404
            else "文稿目录暂时不可用，请稍后重试。"
        )
        html = await _render_error(status_code, message, canonical_path)
        return _generated_response(
            request, html, media_type="text/html", status_code=status_code
        )

    channel_title = CHANNEL_LABELS[selected]
    page_title = f"{channel_title}文稿 · {SITE_NAME}"
    description = f"浏览{channel_title}栏目全部 {len(scoped)} 篇文稿，按北京时间发布日期倒序排列。"
    breadcrumb = _breadcrumb_json_ld(("首页", "/"), (channel_title, canonical_path))
    html = _render_shell(
        title=page_title,
        description=description,
        canonical_path=canonical_path,
        active_channel=selected,
        latest_desk=await _latest_desk_html(),
        body_view="browse",
        body_panel="archive",
        archive=_ArchiveShell(title=channel_title, groups=groups, visible=True),
        extra_json_ld=[breadcrumb],
    )
    return _generated_response(request, html, media_type="text/html")


@app.get("/transcripts/{doc_id:path}", include_in_schema=False)
async def transcript_page(doc_id: str, request: Request) -> Response:
    try:
        canonical_doc_id = _validate_transcript_uri(doc_id)
    except ValueError:
        html = await _render_error(404, "这篇文稿不存在。", request.url.path)
        return _generated_response(
            request, html, media_type="text/html", status_code=404
        )

    canonical_path = _transcript_path(canonical_doc_id)
    encoded = quote(canonical_doc_id, safe="/")
    try:
        article = await _fetch_upstream_json(f"{TRANSCRIPTS_ENDPOINT}/{encoded}")
        if article.get("doc_id") != canonical_doc_id or not isinstance(
            article.get("body_html"), str
        ):
            raise _UpstreamError(503, "文稿服务返回无效数据")
    except _UpstreamError as exc:
        status_code = 404 if exc.status_code == 404 else 503
        message = (
            "这篇文稿不存在。"
            if status_code == 404
            else "文稿服务暂时不可用，请稍后重试。"
        )
        html = await _render_error(status_code, message, canonical_path)
        return _generated_response(
            request, html, media_type="text/html", status_code=status_code
        )

    display_title = _display_title(article)
    description = _description_from_body(article["body_html"], display_title)
    canonical_url = _absolute_url(canonical_path)
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": display_title,
        "description": description,
        "url": canonical_url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical_url},
        "inLanguage": "zh-CN",
        "publisher": {
            "@type": "Organization",
            "name": SITE_NAME,
            "logo": {
                "@type": "ImageObject",
                "url": _absolute_url("/bedtimenews.webp"),
            },
        },
    }
    if published := _valid_date(article.get("publication_date")):
        json_ld["datePublished"] = published
    if modified := _lastmod(article.get("updated_at")):
        json_ld["dateModified"] = modified
    article_metadata = {
        "publication_date": article.get("publication_date"),
        "updated_at": article.get("updated_at"),
        "json_ld": json_ld,
    }
    channel = str(article.get("channel") or "")
    active_channel = channel if channel in CHANNEL_LABELS else None
    breadcrumb_crumbs = [("首页", "/")]
    if active_channel:
        breadcrumb_crumbs.append(
            (CHANNEL_LABELS[active_channel], _channel_path(active_channel))
        )
    breadcrumb_crumbs.append((display_title, canonical_path))
    breadcrumb = _breadcrumb_json_ld(*breadcrumb_crumbs)
    html = _render_shell(
        title=f"{display_title} · {SITE_NAME}",
        description=description,
        canonical_path=canonical_path,
        og_type="article",
        active_channel=active_channel,
        latest_desk=await _latest_desk_html(),
        body_view="browse",
        body_panel="reader",
        ssr_doc_id=canonical_doc_id,
        reader=_ReaderShell(
            title=display_title, body=article["body_html"], visible=True
        ),
        article=article_metadata,
        extra_json_ld=[breadcrumb],
    )
    return _generated_response(request, html, media_type="text/html")


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
