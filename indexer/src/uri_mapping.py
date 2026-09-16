"""URI -> 标准化标题 resolution.

Upstream keys every transcript by its URI (its path relative to `contents/`,
including the .md suffix) and publishes the authoritative URI -> 标准化标题 table
in `URI映射.md`. That file is the source of truth: a general rule covers the vast
majority of transcripts, but 29 of them — the `misc/` specials, officially
duplicated episode numbers, the negative-numbered 产经破壁机 issues — carry titles
no rule can derive. So the table is parsed, and the general rule is kept only as
a fallback for a URI the table does not list (a transcript added upstream before
the table catches up).
"""

import logging
import re

from .paths import URI_MAPPING_FILE

logger = logging.getLogger(__name__)

# Directory name under contents/ -> the channel's Chinese name, as used by the
# 标准化标题 in URI映射.md.
CHANNEL_NAMES = {
    "ShuiQianXiaoXi": "睡前消息",
    "CanKaoXinXi": "参考信息",
    "GaoJian": "高见",
    "JiangDianHeiHua": "讲点黑话",
    "ChanJingPoBiJi": "产经破壁机",
}

# A row of any markdown table in URI映射.md: the first cell is the URI, the
# second the 标准化标题. Both the 例外表 (3 columns) and the per-channel tables
# (2 columns) share that shape, and the separator/header rows are skipped by the
# `.md` check on the first cell.
_TABLE_ROW_RE = re.compile(r"^\|(?P<cells>.*)\|\s*$")

# Episode filename under a 百期文件夹: a zero-padded number, optionally with the
# `.5` 番外 suffix.
_EPISODE_STEM_RE = re.compile(r"(?P<number>\d{4})(?P<extra>\.5)?$")


def load_uri_titles() -> dict[str, str]:
    """Parse URI映射.md into a {uri: 标准化标题} dict.

    Returns an empty dict if the file is missing, so a mapping problem degrades
    to rule-derived titles rather than failing the whole indexing run.
    """
    try:
        text = URI_MAPPING_FILE.read_text(encoding="utf-8")
    except OSError:
        logger.warning(
            f"URI mapping file not readable at {URI_MAPPING_FILE}; "
            "falling back to rule-derived titles"
        )
        return {}

    titles: dict[str, str] = {}
    for line in text.splitlines():
        match = _TABLE_ROW_RE.match(line.strip())
        if not match:
            continue
        cells = [cell.strip() for cell in match.group("cells").split("|")]
        if len(cells) < 2:
            continue
        uri, title = cells[0], cells[1]
        if not uri.endswith(".md") or not title:
            continue
        # The 例外表 repeats rows that also appear in the per-channel tables;
        # upstream keeps the two consistent, so last-write-wins is harmless.
        titles[uri] = title

    logger.info(f"Loaded {len(titles)} URI -> title mappings from {URI_MAPPING_FILE}")
    return titles


def derive_title(uri: str) -> str | None:
    """Derive a 标准化标题 from a URI using the general rule.

    `{栏目}/{百期文件夹}/{NNNN}.md` -> `{栏目中文}{去零期号}`, with the `.5` 番外
    suffix preserved. Returns None for anything the rule does not cover (the
    `misc/` specials and other exceptions), which is precisely when the caller
    must rely on the upstream table.
    """
    parts = uri.split("/")
    if len(parts) != 3 or not parts[2].endswith(".md"):
        return None

    channel = CHANNEL_NAMES.get(parts[0])
    if channel is None:
        return None

    match = _EPISODE_STEM_RE.fullmatch(parts[2][: -len(".md")])
    if match is None:
        return None

    return f"{channel}{int(match.group('number'))}{match.group('extra') or ''}"


def resolve_title(uri: str, titles: dict[str, str]) -> str:
    """Best available 标准化标题 for a URI.

    Prefers the upstream table, falls back to the general rule, and finally to
    the URI itself — a citation labelled with a URI is ugly but still correct and
    still clickable, which beats dropping the reference.
    """
    title = titles.get(uri)
    if title:
        return title

    derived = derive_title(uri)
    if derived:
        logger.warning(f"{uri} missing from URI mapping; derived title {derived!r}")
        return derived

    logger.warning(f"{uri} has no mapped or derivable title; using the URI as title")
    return uri
