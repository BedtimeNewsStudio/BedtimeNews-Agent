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

import hashlib
import json
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, datetime
from html import escape
from html.parser import HTMLParser
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Annotated
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
from starlette.responses import Response
from starlette.types import Scope
from starters import CATEGORIES

AGENT_BACKEND_HOST = os.environ.get("AGENT_BACKEND_HOST", "agent")
AGENT_BACKEND_PORT = os.environ.get("AGENT_BACKEND_PORT", "8000")
AGENT_BASE_URL = f"http://{AGENT_BACKEND_HOST}:{AGENT_BACKEND_PORT}"  # noqa: S5332
CHAT_ENDPOINT = f"{AGENT_BASE_URL}/chat"
TRANSCRIPTS_ENDPOINT = f"{AGENT_BASE_URL}/transcripts"

STATIC_DIR = Path(__file__).parent / "static"
PAGE_TEMPLATE = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
SITE_NAME = "睡前消息知识库"
SITE_DESCRIPTION = "在同一处阅读睡前消息文稿，并随时向知识库 Agent 提问。"
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


def _short_path(doc_id: str) -> str:
    return f"/s/{_short_id_for(doc_id)}"


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


async def _fetch_upstream_json(url: str) -> dict:
    client = _client
    if client is None:
        raise _UpstreamError(503, "文稿服务尚未就绪")
    try:
        response = await client.get(url)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _UpstreamError(503, "文稿服务暂时不可用") from exc
    if response.status_code == 404:
        raise _UpstreamError(404, "文稿不存在")
    if response.status_code != 200:
        raise _UpstreamError(503, "文稿服务暂时不可用")
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise _UpstreamError(503, "文稿服务返回无效数据") from exc
    if not isinstance(payload, dict):
        raise _UpstreamError(503, "文稿服务返回无效数据")
    return payload


def _index_items(payload: dict) -> list[dict]:
    items = payload.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise _UpstreamError(503, "文稿目录返回无效数据")
    return items


_SHORT_ID_CACHE: dict[str, str] = {}


async def _short_id_map(*, force_refresh: bool = False) -> dict[str, str]:
    """short_id -> doc_id, rebuilt from the live transcript index.

    There is no persisted mapping: short IDs are a pure function of doc_id
    (see _short_id_for), so reversing one just means hashing every known
    doc_id and matching. Kept in memory indefinitely rather than on a timer —
    at this corpus size (~2000 docs) the whole map is a few hundred KB, so
    the only reason to rebuild is a short_id the cached map doesn't have,
    which a newly published document would trigger on its first lookup.
    """
    global _SHORT_ID_CACHE
    if _SHORT_ID_CACHE and not force_refresh:
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
    return mapping


def _display_title(item: dict) -> str:
    return str(
        item.get("source_title")
        or item.get("canonical_title")
        or item.get("doc_id")
        or "文稿"
    )


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
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
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
        tags.append('<meta name="robots" content="noindex" />')
    tags.extend(
        [
            f'<link rel="canonical" href="{escape(canonical_url, quote=True)}" />',
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
    for extra in extra_json_ld or []:
        tags.append(
            '<script type="application/ld+json">'
            + _safe_json_script(extra)
            + "</script>"
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
    archive_title: str = "",
    archive_groups: str = "",
    archive_visible: bool = False,
    reader_title: str = "",
    reader_body: str = "",
    reader_state: str = "",
    reader_visible: bool = False,
    article: dict | None = None,
    extra_json_ld: list[dict] | None = None,
    noindex: bool = False,
) -> str:
    canonical_url = _absolute_url(canonical_path)
    replacements = {
        "__PAGE_TITLE__": escape(title),
        "__PAGE_DESCRIPTION__": escape(description, quote=True),
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
        "__SSR_DOC_ID__": escape(ssr_doc_id, quote=True),
        "__SSR_CHANNEL__": escape(active_channel or "", quote=True),
        "__CHANNEL_NAV__": _render_channel_nav(active_channel),
        "__READING_INERT__": "" if body_view == "browse" else "inert",
        "__ARCHIVE_HIDDEN__": "" if archive_visible else "hidden",
        "__ARCHIVE_TITLE__": escape(archive_title),
        "__ARCHIVE_GROUPS__": archive_groups,
        "__ARCHIVE_STATE_HIDDEN__": "hidden" if archive_groups else "",
        "__ARCHIVE_STATE__": "" if archive_groups else "此栏目暂无文稿。",
        "__READER_HIDDEN__": "" if reader_visible else "hidden",
        "__READER_TITLE__": escape(reader_title),
        "__READER_BODY__": reader_body,
        "__READER_STATE__": escape(reader_state),
    }
    rendered = PAGE_TEMPLATE
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _render_error(status_code: int, message: str, path: str = "/") -> str:
    title = "文稿未找到" if status_code == 404 else "文稿服务暂时不可用"
    return _render_shell(
        title=f"{title} · {SITE_NAME}",
        description=message,
        canonical_path=path,
        body_view="browse",
        body_panel="reader",
        reader_title=title,
        reader_state=message,
        reader_visible=True,
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
    urls: list[tuple[str, str | None]] = [(_absolute_url("/"), latest)]
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
    for doc_id in sorted(documents):
        urls.append(
            (
                _absolute_url(_transcript_path(doc_id)),
                _lastmod(documents[doc_id].get("updated_at")),
            )
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
            # since the cache was built. Rebuild once before giving up.
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
        html = _render_error(404, "这个栏目不存在。", "/transcripts")
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
        html = _render_error(status_code, message, canonical_path)
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
        body_view="browse",
        body_panel="archive",
        archive_title=channel_title,
        archive_groups=groups,
        archive_visible=True,
        extra_json_ld=[breadcrumb],
    )
    return _generated_response(request, html, media_type="text/html")


@app.get("/transcripts/{doc_id:path}", include_in_schema=False)
async def transcript_page(doc_id: str, request: Request) -> Response:
    try:
        canonical_doc_id = _validate_transcript_uri(doc_id)
    except ValueError:
        html = _render_error(404, "这篇文稿不存在。", request.url.path)
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
        html = _render_error(status_code, message, canonical_path)
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
        body_view="browse",
        body_panel="reader",
        ssr_doc_id=canonical_doc_id,
        reader_title=display_title,
        reader_body=article["body_html"],
        reader_visible=True,
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
