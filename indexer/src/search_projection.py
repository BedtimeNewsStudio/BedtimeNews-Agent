"""Complete-body search projection, independent of embedding chunk boundaries."""

import html
import logging
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .anchors import assign_heading_anchors, iter_h2
from .models import LoadedIndexableSource
from .search_text import SEARCH_VERSION as SEARCH_VERSION
from .search_text import analyze, lexeme, normalize

logger = logging.getLogger(__name__)
_FOOTNOTE = re.compile(r"\[\^[^\]\n]+\]")
_MARKDOWN = MarkdownIt("commonmark").enable("table")


class _VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _inline(tokens: list[Token]) -> str:
    parts = []
    for token in tokens:
        if token.type in {"text", "code_inline"}:
            parts.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif token.type == "image":
            parts.append(_inline(token.children or []))
        elif token.type == "html_inline":
            parts.append(token.content)
    return "".join(parts)


def plain_text(markdown: str) -> str:
    """Render visible Markdown text without destinations or format syntax."""
    blocks = []
    for token in _MARKDOWN.parse(_FOOTNOTE.sub("", markdown)):
        if token.type == "inline":
            # Parse HTML tags, but escape ordinary prose so literal '<' stays.
            parts = []
            for child in token.children or []:
                if child.type == "html_inline":
                    parts.append(child.content)
                else:
                    parts.append(html.escape(_inline([child])))
            parser = _VisibleHTML()
            parser.feed("".join(parts))
            blocks.append("".join(parser.parts))
        elif token.type == "html_block":
            parser = _VisibleHTML()
            parser.feed(token.content)
            blocks.append("".join(parser.parts))
        elif token.type in {"fence", "code_block"}:
            blocks.append(token.content.rstrip())
    return unicodedata.normalize("NFC", "\n\n".join(blocks).strip())


@dataclass(frozen=True)
class SearchProjection:
    doc_id: str
    body_text: str
    normalized_text: str
    body_hash: str
    sections: list[dict]
    token_data: dict[str, list[int]]

    @property
    def terms(self) -> list[str]:
        return sorted(lexeme(term) for term in self.token_data)

    @property
    def token_count(self) -> int:
        return sum(map(len, self.token_data.values()))


def project_search(source: LoadedIndexableSource) -> SearchProjection:
    body = source.document.text
    headings = iter_h2(body)
    anchors = assign_heading_anchors([title for _, title in headings])
    ranges: list[tuple[int, int, str | None, str]] = []
    if not headings or headings[0][0]:
        ranges.append((0, headings[0][0] if headings else len(body), None, ""))
    for i, (start, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(body)
        ranges.append((start, end, title, anchors[i]))
    sections = []
    pieces = []
    length = 0
    for start, end, heading, anchor in ranges:
        text = plain_text(body[start:end])
        if not text:
            continue
        if pieces:
            length += 2
        sections.append(
            {
                "start": length,
                "end": length + len(text),
                "heading": plain_text(heading) if heading else None,
                "heading_anchor": anchor,
            }
        )
        pieces.append(text)
        length += len(text)
    display = "\n\n".join(pieces)
    if "\x00" in display:
        raise ValueError(f"NUL in search body: {source.document.doc_id}")
    if not display:
        logger.warning("%s has an empty searchable body", source.document.doc_id)
    return SearchProjection(
        source.document.doc_id,
        display,
        normalize(display),
        source.body_hash,
        sections,
        analyze(display),
    )
