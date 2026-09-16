"""Fallback 标准化标题 derivation from a document URI.

Titles normally reach the agent from rag.documents, which the indexer fills from
the upstream URI映射.md. This module covers the gap: a chunk indexed before the
title sync ran, or a transcript upstream added but has not yet listed. It
implements only the general rule — the 29 documented exceptions cannot be
derived from their URI at all, and for those the URI itself is shown instead.
"""

import re

# Directory name under contents/ -> the channel's Chinese name.
CHANNEL_NAMES = {
    "ShuiQianXiaoXi": "睡前消息",
    "CanKaoXinXi": "参考信息",
    "GaoJian": "高见",
    "JiangDianHeiHua": "讲点黑话",
    "ChanJingPoBiJi": "产经破壁机",
}

_EPISODE_STEM_RE = re.compile(r"(?P<number>\d{4})(?P<extra>\.5)?$")


def derive_title(uri: str) -> str | None:
    """`ShuiQianXiaoXi/0501-0600/0588.md` -> `睡前消息588`, else None."""
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
