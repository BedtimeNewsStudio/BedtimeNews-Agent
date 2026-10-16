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

from .anchors import encode_fragment, topic_anchors
from .document_loader import clean_text, extract_body
from .models import TranscriptProjection

# Bump whenever the stored projection changes so stale rows re-render without
# touching RAG embeddings (which still key only on normalized ## 正文).
#   5: topic h2 elements carry the shared heading anchor as id.
TRANSCRIPT_PROJECTION_VERSION = 5

_OLD_TRANSCRIPT_HOST = "bedtimenewsstudio.github.io"
_OLD_TRANSCRIPT_PREFIX = "/BedtimeNews-Transcripts/contents/"
# Character classes instead of a lazy `.+?` followed by `\s*$`: that shape
# backtracks quadratically on a title line with a long run of trailing spaces.
# Trailing whitespace is removed by _plain_title.
_TITLE_RE = re.compile(r"^#[ \t]+([^\r\n]+)", re.MULTILINE)
# The value is optional and stays on the marker's own line: a date line with
# nothing after it must not pick up the next line. `\r?` lets CRLF files match.
_DATE_LINE_RE = re.compile(
    r"^\*\*发布日期\*\*[ \t]*([^ \t\r\n][^\r\n]*)?\r?$", re.MULTILINE
)
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
    "h2": ["id"],
    "h3": ["id"],
    "h4": ["id"],
    "h5": ["id"],
    "h6": ["id"],
    "td": ["align"],
    "th": ["align"],
}
_H2_TAG = re.compile(r"<h2>(.*?)</h2>", re.IGNORECASE | re.DOTALL)


def _plain_title(value: str) -> str:
    value = _MARKDOWN_LINK_RE.sub(r"\1", value)
    return value.replace("**", "").replace("__", "").replace("`", "").strip()


def _publication_date(markdown: str) -> str | None:
    match = _DATE_LINE_RE.search(markdown)
    if not match:
        return None
    value = (match.group(1) or "").strip()
    if not value:
        return None
    date = _ISO_DATE_RE.search(value)
    return date.group(1) if date else _plain_title(value)


def _reader_markdown(markdown: str) -> str:
    """Full source for the reader, minus the H1 already shown in page chrome.

    RAG still indexes only cleaned ## 正文. The website must also carry the
    publication line (B站 / YouTube / …) and the full ## 附录, including the
    footnote definitions the body cites.
    """
    return _LEADING_H1_RE.sub("", markdown, count=1).strip()


def _split_fragment(href: str) -> tuple[str, str]:
    """Return ``(path, raw fragment)``. The fragment is empty when absent."""
    path, sep, fragment = href.partition("#")
    return path, fragment if sep else ""


def _append_fragment(url: str, fragment: str) -> str:
    """Keep a fragment that was already on the link. Do not invent one."""
    if not fragment:
        return url
    return f"{url}#{encode_fragment(unquote(fragment))}"


def _normalize_relative_uri(base_uri: str, href: str) -> str | None:
    """Resolve a relative Markdown link without permitting path traversal."""
    path, _fragment = _split_fragment(href)
    decoded = unquote(path.split("?", 1)[0])
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

    path, fragment = _split_fragment(href)
    parsed = urlparse(path)
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
            return _append_fragment(f"/transcripts/{quote(uri, safe='/')}", fragment)
        return href
    if scheme == "mailto":
        return href
    if scheme:
        return None

    uri = _normalize_relative_uri(base_uri, path)
    if not uri:
        return None
    return _append_fragment(f"/transcripts/{quote(uri, safe='/')}", fragment)


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
    return _stamp_topic_ids(cleaner.clean(rendered), _body_topic_anchors(markdown))


def _body_topic_anchors(markdown: str) -> list[str]:
    """Anchors for topic ``##`` lines, in the same order the chunker sees."""
    body = clean_text(extract_body(markdown))
    return topic_anchors(body)


def _h2_text(inner: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()


def _stamp_topic_ids(rendered: str, anchors: list[str]) -> str:
    """Write topic anchors onto body ``h2`` elements. ``## 正文`` and ``## 附录`` stay bare."""
    index = 0
    in_body = False

    def replace(match: re.Match[str]) -> str:
        nonlocal index, in_body
        inner = match.group(1)
        text = _h2_text(inner)
        if text == "正文":
            in_body = True
            return match.group(0)
        if text == "附录":
            in_body = False
            return match.group(0)
        if not in_body or index >= len(anchors):
            return match.group(0)
        anchor = anchors[index]
        index += 1
        if not anchor:
            return match.group(0)
        return f'<h2 id="{html.escape(anchor, quote=True)}">{inner}</h2>'

    return _H2_TAG.sub(replace, rendered)


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
        body_markdown=markdown,
        source_hash=hashlib.sha256(raw_bytes).hexdigest(),
        projection_version=TRANSCRIPT_PROJECTION_VERSION,
    )


__all__ = [
    "TRANSCRIPT_PROJECTION_VERSION",
    "project_transcript",
]
