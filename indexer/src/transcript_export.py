"""Parse upstream Markdown into a safe, reader-facing transcript projection."""

import hashlib
import html
import re
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlparse

import bleach
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from mdit_py_plugins.footnote import footnote_plugin

from .models import TranscriptProjection

# Bump whenever reader HTML shape changes so stale projections re-render without
# touching RAG embeddings (which still key only on normalized ## 正文).
TRANSCRIPT_PROJECTION_VERSION = 3

_OLD_TRANSCRIPT_HOST = "bedtimenewsstudio.github.io"
_OLD_TRANSCRIPT_PREFIX = "/BedtimeNews-Transcripts/contents/"
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_DATE_LINE_RE = re.compile(r"^\*\*发布日期\*\*\s*(.+?)\s*$", re.MULTILINE)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_LEADING_H1_RE = re.compile(r"^\s*#\s+.+?(?:\n+|$)")

_ALLOWED_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "code",
        "del",
        "em",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)
# Footnote plugin emits id/href pairs on refs and backrefs; keep those so the
# reader can jump body <-> appendix without trusting anything else.
_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "rel", "class", "aria-label", "id"],
    "li": ["id", "class"],
    "ol": ["id", "class"],
    "p": ["id", "class"],
    "section": ["id", "class"],
    "span": ["class", "id"],
    "sup": ["id", "class"],
    "td": ["align"],
    "th": ["align"],
}


def _plain_title(value: str) -> str:
    value = _MARKDOWN_LINK_RE.sub(r"\1", value)
    return value.replace("**", "").replace("__", "").replace("`", "").strip()


def _publication_date(markdown: str) -> str | None:
    match = _DATE_LINE_RE.search(markdown)
    if not match:
        return None
    value = match.group(1).strip()
    date = _ISO_DATE_RE.search(value)
    return date.group(1) if date else _plain_title(value)


def _reader_markdown(markdown: str) -> str:
    """Full source for the reader, minus the H1 already shown in page chrome.

    RAG still indexes only cleaned ## 正文. The website must also carry the
    publication line (B站 / YouTube / …) and the full ## 附录, including the
    footnote definitions the body cites.
    """
    return _LEADING_H1_RE.sub("", markdown, count=1).strip()


def _normalize_relative_uri(base_uri: str, href: str) -> str | None:
    """Resolve a relative Markdown link without permitting path traversal."""
    decoded = unquote(href.split("#", 1)[0].split("?", 1)[0])
    if decoded.startswith(("/", "\\")) or "\\" in decoded:
        return None
    base_parts = list(PurePosixPath(base_uri).parent.parts)
    for part in PurePosixPath(decoded).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not base_parts:
                return None
            base_parts.pop()
        else:
            base_parts.append(part)
    if not base_parts:
        return None
    resolved = PurePosixPath(*base_parts)
    if resolved.suffix.lower() != ".md":
        return None
    return resolved.as_posix()


def _rewrite_href(base_uri: str, href: str) -> str | None:
    """Return a safe external/fragment/local-reader URL, or remove the link."""
    href = href.strip()
    if not href:
        return None
    if href.startswith("#"):
        return href
    if href.startswith("//"):
        return None

    parsed = urlparse(href)
    scheme = parsed.scheme.lower()
    if scheme in {"javascript", "vbscript", "file", "data"}:
        return None
    if scheme in {"http", "https"}:
        if (
            parsed.hostname == _OLD_TRANSCRIPT_HOST
            and parsed.path.startswith(_OLD_TRANSCRIPT_PREFIX)
            and parsed.path.endswith(".html")
        ):
            relative = unquote(parsed.path.removeprefix(_OLD_TRANSCRIPT_PREFIX))
            uri = f"{relative.removesuffix('.html')}.md"
            return f"/transcripts/{quote(uri, safe='/')}"
        return href
    if scheme == "mailto":
        return href
    if scheme:
        return None

    uri = _normalize_relative_uri(base_uri, href)
    return f"/transcripts/{quote(uri, safe='/')}" if uri else None


class _TranscriptRenderer(RendererHTML):
    """Renderer that localizes transcript links and suppresses remote images."""

    def link_open(self, tokens, idx, options, env):
        token = tokens[idx]
        href = str(token.attrGet("href") or "")
        rewritten = _rewrite_href(str(env["doc_id"]), href)
        if rewritten is None:
            token.attrs.pop("href", None)
        else:
            token.attrSet("href", rewritten)
            if rewritten.startswith("/transcripts/"):
                token.attrSet("class", "transcript-inline-link")
            elif rewritten.startswith(("http://", "https://", "mailto:")):
                token.attrSet("rel", "noopener noreferrer")
        return self.renderToken(tokens, idx, options, env)

    def image(self, tokens, idx, options, env):
        alt = self.renderInlineAsText(tokens[idx].children, options, env).strip()
        label = f"图片：{alt}" if alt else "图片"
        return f'<span class="transcript-image-note">[{html.escape(label)}]</span>'


def _render_markdown(doc_id: str, markdown: str) -> str:
    parser = MarkdownIt(
        "commonmark",
        {"html": True, "linkify": False, "typographer": False},
        renderer_cls=_TranscriptRenderer,
    )
    parser.enable("table")
    parser.use(footnote_plugin)
    rendered = parser.render(markdown, {"doc_id": doc_id})
    cleaner = bleach.Cleaner(
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=frozenset({"http", "https", "mailto"}),
        strip=True,
        strip_comments=True,
    )
    return cleaner.clean(rendered)


def project_transcript(
    doc_id: str,
    raw_bytes: bytes,
    canonical_title: str,
) -> TranscriptProjection:
    """Build the safe database projection for one canonical transcript URI."""
    markdown = raw_bytes.decode("utf-8")
    title_match = _TITLE_RE.search(markdown)
    source_title = (
        _plain_title(title_match.group(1)) if title_match else canonical_title
    )
    return TranscriptProjection(
        doc_id=doc_id,
        canonical_title=canonical_title,
        source_title=source_title,
        channel=PurePosixPath(doc_id).parts[0],
        publication_date=_publication_date(markdown),
        body_html=_render_markdown(doc_id, _reader_markdown(markdown)),
        source_hash=hashlib.sha256(raw_bytes).hexdigest(),
        projection_version=TRANSCRIPT_PROJECTION_VERSION,
    )


__all__ = [
    "TRANSCRIPT_PROJECTION_VERSION",
    "project_transcript",
]
