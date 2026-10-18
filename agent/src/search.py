"""Traditional keyword search, bounded snippets and snapshot-bound pagination."""

import base64
import binascii
import json
import re
from bisect import bisect_left

import psycopg2
from fastapi import HTTPException
from psycopg2 import sql
from pydantic import BaseModel, Field

from .cache import LRUCache
from .search_db import query_search
from .search_text import (
    MAX_TERM_LENGTH,
    MAX_TERMS,
    analyze,
    lexeme,
    query_terms,
    term_weight,
)
from .snapshots import (
    Snapshot,
    SnapshotUnavailable,
    agent_embedding_space,
    settings,
    snapshot_manager,
)
from .vector_db import read_cursor, retry_on_transient_error

SEARCH_FORMAT = 4
CURSOR_VERSION = 3
_cache = LRUCache(capacity=500, max_memory_mb=16)


class SnippetPart(BaseModel):
    text: str
    highlight: bool


class SearchSnippet(BaseModel):
    heading: str | None
    heading_anchor: str
    parts: list[SnippetPart]


class SearchResult(BaseModel):
    doc_id: str
    title: str
    source_title: str
    channel: str
    publication_date: str | None
    score: float
    snippets: list[SearchSnippet]
    matched_terms: list[str]


class SearchResponse(BaseModel):
    query: str
    terms: list[str]
    snapshot_id: str
    total: int
    results: list[SearchResult]
    next_cursor: str | None


class _Cursor(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    version: int = Field(ge=CURSOR_VERSION, le=CURSOR_VERSION)
    snapshot_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_]+$")
    terms: list[str] = Field(min_length=1, max_length=MAX_TERMS)
    offset: int = Field(ge=0, le=2**31 - 1)
    limit: int = Field(ge=1, le=50)


def parse_terms(query: str) -> list[str]:
    if len(query) > 512 or "\x00" in query:
        raise HTTPException(
            422, "Query must be at most 512 characters and contain no NUL"
        )
    terms = query_terms(query)
    if (
        not terms
        or len(terms) > MAX_TERMS
        or any(len(t) > MAX_TERM_LENGTH for t in terms)
    ):
        raise HTTPException(
            422, "Provide 1–64 searchable words, each at most 64 characters"
        )
    return terms


def keyword_query(term: str) -> str:
    return lexeme(term)


def _decode_cursor(value: str, terms: list[str], limit: int) -> _Cursor:
    try:
        if len(value) > 16384 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("invalid encoding")
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = _Cursor.model_validate(json.loads(raw))
        if decoded.terms != terms or decoded.limit != limit:
            raise ValueError("query or page size changed")
        return decoded
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise HTTPException(
            422, "Invalid search cursor or changed query/page size"
        ) from exc


def _encode_cursor(snapshot_id: str, terms: list[str], offset: int, limit: int) -> str:
    data = _Cursor(
        version=CURSOR_VERSION,
        snapshot_id=snapshot_id,
        terms=terms,
        offset=offset,
        limit=limit,
    )
    return (
        base64.urlsafe_b64encode(data.model_dump_json().encode()).decode().rstrip("=")
    )


def _occurrences(
    positions: dict[str, list[int]], terms: list[str], start: int, end: int
):
    for index, term in enumerate(terms):
        for pos in positions.get(term, []):
            if start <= pos and pos + len(term) <= end:
                yield pos, pos + len(term), index


def make_snippets(
    body: str,
    normalized: str,
    sections: list[dict],
    terms: list[str],
    positions: dict[str, list[int]] | None = None,
) -> list[SearchSnippet]:
    """Choose up to three dense, distinct-term windows within topic boundaries."""
    positions = positions if positions is not None else analyze(normalized)
    candidates = []
    for section in sections:
        start, end = section["start"], section["end"]
        hits = list(_occurrences(positions, terms, start, end))
        # Bucket windows bound selection cost even for very frequent single chars.
        windows: set[tuple[int, int]] = set()
        for left, right, _ in hits:
            window_start = max(start, ((left - start) // 40) * 40 + start - 40)
            window_end = min(end, max(window_start + 120, right))
            windows.add((window_start, window_end))
        hits.sort()
        starts = [h[0] for h in hits]
        longest = max(map(len, terms))
        for left, right in sorted(windows):
            selected = [
                h
                for h in hits[
                    bisect_left(starts, left - longest) : bisect_left(starts, right)
                ]
                if h[1] > left
            ]
            covered = {h[2] for h in selected}
            density = sum(term_weight(terms[h[2]]) for h in selected) / max(
                1, right - left
            )
            candidates.append(
                (
                    sum(term_weight(terms[i]) for i in covered),
                    density,
                    left,
                    right,
                    section,
                    selected,
                )
            )
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
    chosen = []
    for candidate in candidates:
        _, _, left, right, section, hits = candidate
        # Never cut through a matched keyword; adjust to nearby sentence boundaries.
        left = min(left, min(h[0] for h in hits))
        right = max(right, max(h[1] for h in hits))
        for pos in range(left, max(section["start"], left - 24) - 1, -1):
            if pos == section["start"] or body[pos - 1] in "。！？!?\n":
                left = pos
                break
        for pos in range(right, min(section["end"], right + 24) + 1):
            if pos == section["end"] or body[pos - 1] in "。！？!?\n":
                right = pos
                break
        overlapping = [
            i for i, other in enumerate(chosen) if left < other[3] and right > other[2]
        ]
        if overlapping:
            merged_left = min(left, *(chosen[i][2] for i in overlapping))
            merged_right = max(right, *(chosen[i][3] for i in overlapping))
            # Merge nearby windows without growing an excerpt to a whole topic.
            if merged_right - merged_left <= 200:
                merged_hits = list(
                    _occurrences(positions, terms, merged_left, merged_right)
                )
                merged = (
                    sum(term_weight(terms[i]) for i in {h[2] for h in merged_hits}),
                    len(merged_hits) / (merged_right - merged_left),
                    merged_left,
                    merged_right,
                    section,
                    merged_hits,
                )
                chosen[overlapping[0]] = merged
                for index in reversed(overlapping[1:]):
                    chosen.pop(index)
            continue
        if len(chosen) < 3:
            chosen.append((candidate[0], candidate[1], left, right, section, hits))
    snippets = []
    for _, _, left, right, section, _ in chosen:
        spans = sorted(
            (a, b) for a, b, _ in _occurrences(positions, terms, left, right)
        )
        merged: list[list[int]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        parts = []
        pos = left
        if left > section["start"]:
            parts.append(SnippetPart(text="…", highlight=False))
        for a, b in merged:
            if a > pos:
                parts.append(SnippetPart(text=body[pos:a], highlight=False))
            parts.append(SnippetPart(text=body[a:b], highlight=True))
            pos = b
        if pos < right:
            parts.append(SnippetPart(text=body[pos:right], highlight=False))
        if right < section["end"]:
            parts.append(SnippetPart(text="…", highlight=False))
        snippets.append(
            SearchSnippet(
                heading=section["heading"],
                heading_anchor=section["heading_anchor"],
                parts=parts,
            )
        )
    return snippets


@retry_on_transient_error()
def search(query: str, limit: int = 20, cursor: str | None = None) -> SearchResponse:
    terms = parse_terms(query)
    decoded = _decode_cursor(cursor, terms, limit) if cursor is not None else None
    offset = decoded.offset if decoded else 0
    try:
        current = snapshot_manager.require() if not decoded else None
        if current and current.format_version != SEARCH_FORMAT:
            raise HTTPException(503, "Search index is not ready")
        with read_cursor() as db:
            if decoded:
                db.execute(
                    "SELECT snapshot_id, schema_name, format_version, embedding_space "
                    "FROM rag_meta.snapshots WHERE snapshot_id = %s AND status = 'published' "
                    "AND format_version = %s AND embedding_space = %s",
                    (decoded.snapshot_id, SEARCH_FORMAT, agent_embedding_space()),
                )
                row = db.fetchone()
                if not row or (
                    settings.rag_snapshot
                    and settings.rag_snapshot != row["snapshot_id"]
                ):
                    raise HTTPException(
                        410, "Search cursor expired; restart the search"
                    )
                snapshot = Snapshot(**row)
            else:
                snapshot = current
            assert snapshot is not None
            if snapshot.format_version != SEARCH_FORMAT:
                raise HTTPException(503, "Search index is not ready")
            # Hold table locks through cache lookup too: GC cannot make a cached
            # response appear to continue a snapshot that has already vanished.
            db.execute(
                sql.SQL(
                    "LOCK TABLE {s}.search_documents, {s}.search_statistics, "
                    "{s}.documents, {s}.transcripts IN ACCESS SHARE MODE"
                ).format(s=sql.Identifier(snapshot.schema_name))
            )
            key = json.dumps(
                [snapshot.snapshot_id, terms, limit, offset], ensure_ascii=True
            )
            cached = _cache.get(key)
            if cached is not None:
                return cached.model_copy(update={"query": query})
            page = query_search(
                db,
                snapshot.schema_name,
                terms,
                [keyword_query(t) for t in terms],
                limit,
                offset,
            )
            results = []
            for item in page["results"]:
                snippets = make_snippets(
                    item.pop("body_text"),
                    item.pop("normalized_text"),
                    item.pop("sections"),
                    item["matched_terms"],
                    item.pop("matched_positions"),
                )
                results.append(SearchResult(**item, snippets=snippets))
            next_offset = offset + len(results)
            response = SearchResponse(
                query=query,
                terms=terms,
                snapshot_id=snapshot.snapshot_id,
                total=page["total"],
                results=results,
                next_cursor=_encode_cursor(
                    snapshot.snapshot_id, terms, next_offset, limit
                )
                if results and next_offset < page["total"]
                else None,
            )
            _cache.put(key, response)
            return response
    except SnapshotUnavailable as exc:
        raise HTTPException(503, "Search index is not ready") from exc
    except (psycopg2.errors.UndefinedTable, psycopg2.errors.InvalidSchemaName) as exc:
        if decoded:
            raise HTTPException(
                410, "Search cursor expired; restart the search"
            ) from exc
        raise HTTPException(503, "Search index is not ready") from exc
