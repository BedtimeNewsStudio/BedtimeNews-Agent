"""Heading anchors shared by the chunker and the reader projection.

One function, one result: ``heading_anchor`` in the database, the ``id`` on a
topic ``h2``, and the URL fragment are the same string. The stored value and
the HTML ``id`` are raw. Only the fragment is percent-encoded. An empty
string means no anchor, no ``id``, and a whole-document link.

The preamble is not passed in here, so it never takes a dedupe slot.
"""

import re
from urllib.parse import quote

# Exactly two hashes. ``###`` must not match.
H2_LINE = re.compile(r"^##(?!#)[ \t]+([^\r\n]+)", re.MULTILINE)

_LEADING_H2 = re.compile(r"^##(?!#)[ \t]+")
# Innermost link only, so ``[[text](url)](url2)`` unwraps in two passes.
_MD_LINK = re.compile(r"\[([^\[\]]*)\]\([^)]*\)")
_FOOTNOTE = re.compile(r"\[\^[^\]]*\]")
_WRAPPERS = ("**", "__", "`")


def iter_h2(text: str) -> list[tuple[int, str]]:
    """Body topic headings in order: ``(start offset, plain title)``."""
    return [(match.start(), match.group(1).strip()) for match in H2_LINE.finditer(text)]


def heading_anchor(text: str) -> str:
    """Turn one heading line, or its plain text, into an anchor.

    Does not deduplicate. An empty result means this heading gets no anchor.
    """
    value = _LEADING_H2.sub("", text.strip(), count=1)
    previous = None
    while previous != value:
        previous = value
        value = _MD_LINK.sub(r"\1", value)
    value = _FOOTNOTE.sub("", value).strip()
    value = _strip_wrapper(value).strip()
    value = re.sub(r"\s+", "-", value)
    return value.strip("-")


def _strip_wrapper(text: str) -> str:
    for wrapper in _WRAPPERS:
        size = len(wrapper)
        if (
            len(text) >= size * 2 + 1
            and text.startswith(wrapper)
            and text.endswith(wrapper)
            and wrapper not in text[size:-size]
        ):
            return text[size:-size]
    return text


def assign_heading_anchors(headings: list[str]) -> list[str]:
    """Document-order anchors. Empty results stay empty and take no slot.

    A repeated anchor becomes ``-2``, ``-3``, and so on, skipping any suffix
    that a later heading already owns.
    """
    used: set[str] = set()
    assigned: list[str] = []
    for heading in headings:
        anchor = heading_anchor(heading)
        if not anchor:
            assigned.append("")
            continue
        candidate = anchor
        suffix = 2
        while candidate in used:
            candidate = f"{anchor}-{suffix}"
            suffix += 1
        used.add(candidate)
        assigned.append(candidate)
    return assigned


def topic_anchors(body: str) -> list[str]:
    """Anchors for the topic ``##`` headings in one already-extracted body."""
    return assign_heading_anchors([title for _pos, title in iter_h2(body)])


def encode_fragment(anchor: str) -> str:
    """Percent-encode an anchor for a URL fragment. A hyphen stays literal."""
    return quote(anchor, safe="")


__all__ = [
    "H2_LINE",
    "assign_heading_anchors",
    "encode_fragment",
    "heading_anchor",
    "iter_h2",
    "topic_anchors",
]
