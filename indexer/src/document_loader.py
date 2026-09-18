"""Document loading, normalization, and indexing fingerprints."""

import hashlib
import logging
import re

from .models import Document, LoadedIndexableSource
from .paths import CONTENTS_DIR

logger = logging.getLogger(__name__)

# Increment this whenever the normalized text passed to chunking changes. Rows
# written by an older version are deliberately re-indexed even if the source
# bytes are unchanged.
BODY_NORMALIZATION_VERSION = 1

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


def load_indexable_source(
    uri: str, raw_bytes: bytes | None = None
) -> LoadedIndexableSource:
    """Load a transcript and fingerprint both its source and indexed body.

    ``body_hash`` is calculated from the exact normalized string stored in
    ``Document.text``. Title, publication date, appendix, and formatting that
    normalization removes therefore cannot trigger unnecessary embeddings.
    ``source_hash`` still observes every byte for non-RAG consumers such as the
    transcript reader.
    """
    file_path = CONTENTS_DIR / uri
    if raw_bytes is None:
        raw_bytes = file_path.read_bytes()

    content = raw_bytes.decode("utf-8")
    text = clean_text(extract_body(content, uri))
    document = Document(
        file_path=str(file_path),
        doc_id=uri,
        slug=uri_to_slug(uri),
        text=text,
    )
    return LoadedIndexableSource(
        document=document,
        source_hash=hashlib.sha256(raw_bytes).hexdigest(),
        body_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        body_normalization_version=BODY_NORMALIZATION_VERSION,
    )


def load_document(uri: str) -> Document:
    """Load one transcript by URI, returning its normalized 正文 only."""
    return load_indexable_source(uri).document


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
    """Normalize a transcript body for chunking and body hashing."""
    text = _strip_inline_html(text)

    # Normalize line endings to \n
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove multiple consecutive blank lines
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)

    return text.strip()


# Inline formatting tags that actually occur in transcript bodies, kept as an
# explicit whitelist rather than a catch-all `<[^>]+>`. The catch-all is unsafe
# on this corpus: prose contains bare comparison operators, so a sentence like
# "如果ΔP<H，目标生存；如果ΔP>H，目标摧毁" looks like a tag between the `<` and the
# next `>` and loses the whole clause in between. A whitelist cannot do that.
_INLINE_HTML_RE = re.compile(
    r"</?(?:br|u|b|i|em|strong|center|sup|sub|p)\s*/?>", re.IGNORECASE
)


def _strip_inline_html(text: str) -> str:
    """Drop inline formatting tags, keeping the text they wrap."""
    return _INLINE_HTML_RE.sub("", text)
