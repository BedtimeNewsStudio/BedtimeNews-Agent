"""Document loading and text cleaning."""

import logging
import re

from .models import Document
from .paths import CONTENTS_DIR

logger = logging.getLogger(__name__)

# Every transcript is laid out as a `# 标题` line, a `**发布日期**` line, then
# exactly one `## 正文` section followed by exactly one `## 附录` section (verified
# across all 1812 upstream transcripts). Only 正文 is indexed: 附录 holds fact
# corrections and verification notes about the transcript rather than anything
# the show said, and retrieving it produces citations that answer with the
# editors' footnotes instead of the episode.
BODY_HEADING = "## 正文"
APPENDIX_HEADING = "## 附录"


def uri_to_slug(uri: str) -> str:
    """Filesystem- and identifier-safe form of a URI, used to build chunk ids."""
    return uri.removesuffix(".md").replace("/", "_")


def load_document(uri: str) -> Document:
    """Load a single transcript by its URI.

    Args:
        uri: Document URI — path relative to CONTENTS_DIR including the .md
            suffix (e.g. "ShuiQianXiaoXi/0501-0600/0588.md"). This is also the
            doc_id used throughout the system.

    Returns:
        Document object whose text is the transcript's 正文 only.
    """
    file_path = CONTENTS_DIR / uri

    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    text = clean_text(extract_body(content, uri))

    return Document(
        id=f"doc_{uri_to_slug(uri)}",
        file_path=str(file_path),
        doc_id=uri,
        slug=uri_to_slug(uri),
        text=text,
    )


def extract_body(content: str, uri: str = "") -> str:
    """Return the `## 正文` section, excluding the `## 附录` section that follows.

    The body's own sub-headings are `##` too, so the section cannot be delimited
    by "the next heading of the same level" — it runs from the `## 正文` line to
    the `## 附录` line. Everything before 正文 (the `# 标题` line and the
    `**发布日期**` metadata line) is dropped along with everything from 附录 on.

    A transcript with no `## 正文` yields an empty string rather than raising:
    the file is then indexed as zero chunks and logged, which is preferable to
    failing a whole scheduled run over one malformed document upstream.
    """
    lines = content.splitlines()

    start = None
    for i, line in enumerate(lines):
        if line.strip() == BODY_HEADING:
            start = i + 1
            break

    if start is None:
        logger.warning(f"{uri or 'document'} has no {BODY_HEADING} section; skipping")
        return ""

    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].strip() == APPENDIX_HEADING:
            end = j
            break

    return "\n".join(lines[start:end])


def clean_text(text: str) -> str:
    """Clean text content: remove YAML front matter, HTML, normalize whitespace.

    Args:
        text: Raw text content

    Returns:
        Cleaned text
    """
    # Remove YAML front matter (--- ... ---)
    pattern = r"^---\s*\n(.*?)\n---\s*\n"
    match = re.match(pattern, text, re.DOTALL)
    if match:
        text = text[match.end() :]

    # Remove HTML sections
    text = _remove_html_sections(text)

    # Normalize line endings to \n
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove multiple consecutive blank lines
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)

    # Strip leading/trailing whitespace
    text = text.strip()

    return text


def _remove_html_sections(text: str) -> str:
    """Remove HTML sections and embedded content.

    Args:
        text: Markdown text with potential HTML

    Returns:
        Text with HTML sections removed
    """
    # Remove the Tabs section with video embeds
    text = re.sub(
        r"#\s+Tabs\s+\{\.tabset\}.*?(?=\n#{1,6}\s+|\Z)", "", text, flags=re.DOTALL
    )

    # Remove HTML comments
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    # Remove standalone HTML tags (div, iframe, etc.)
    text = re.sub(
        r"<(?:div|iframe|span)[^>]*>.*?</(?:div|iframe|span)>",
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(r"<(?:div|iframe|span)[^>]*/?>", "", text)

    # Remove any remaining empty div tags
    text = re.sub(r"<div[^>]*>\s*</div>", "", text, flags=re.DOTALL)

    # Remove font tags but keep inner text
    text = re.sub(r"<font[^>]*>(.*?)</font>", r"\1", text, flags=re.DOTALL)

    # Remove any remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)

    # Remove Markdown image syntax: ![alt text](url)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", "", text)

    # Remove standalone image placeholders
    text = re.sub(r"^图片\s*$", "", text, flags=re.MULTILINE)

    return text
