"""Keyword search: ranking pipeline, snippets and snapshot-bound pagination.

One search builds a result set once per (snapshot, query): keyword ranking from
search_postings, relative cutoff and title bonus; for questions, semantic recall
fused by reciprocal rank and an optional rerank. Filters, date order and pages
are then slices of that cached set, so paging never re-ranks.
"""

import base64
import binascii
import json
import re
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Literal

import psycopg2
from fastapi import HTTPException
from psycopg2 import sql
from pydantic import BaseModel, Field

from .cache import LRUCache
from .search_db import (
    lexicon_near,
    page_documents,
    rank_documents,
    transcript_titles,
    variant_positions,
)
from .search_query import QueryUnit, is_question, query_units, split_phrases
from .search_semantic import SemanticHit, fuse, rerank, rerank_enabled, semantic_hits
from .search_text import (
    MAX_TERM_LENGTH,
    MAX_TERMS,
    SEARCH_VERSION,
    analyze,
    normalize,
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

SEARCH_FORMAT = 5
CURSOR_VERSION = 5
# Results whose body score is below this share of the best body score are
# dropped. The title bonus only reorders; it never pushes others out.
RELATIVE_CUTOFF = 0.2
# Up to this much extra score when query units also appear in the episode title.
TITLE_BONUS = 1.0
# Question results the reranker reorders, and the text it reads of each.
RERANK_TOP = 10
RERANK_CHARS = 600
SEMANTIC_EXCERPT = 160

_cache = LRUCache(capacity=500, max_memory_mb=16)
_result_sets = LRUCache(capacity=200, max_memory_mb=32)
_titles: dict[str, dict[str, dict]] = {}
_titles_lock = Lock()
_models = ThreadPoolExecutor(max_workers=4, thread_name_prefix="search-models")


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
    # keyword: matched query words; semantic: close in meaning only; both.
    match: Literal["keyword", "semantic", "both"]
    snippets: list[SearchSnippet]
    matched_terms: list[str]


class Facet(BaseModel):
    value: str
    count: int


class SearchFacets(BaseModel):
    channels: list[Facet]
    years: list[Facet]


class SearchResponse(BaseModel):
    query: str
    terms: list[str]
    snapshot_id: str
    # keyword, or hybrid when a question also got semantic recall.
    mode: Literal["keyword", "hybrid"]
    reranked: bool
    # A corrected query when a query word occurs nowhere but a close one does.
    suggestion: str | None
    sort: Literal["relevance", "date"]
    channel: str | None
    year: int | None
    # Counts over all results, before the channel and year filters.
    facets: SearchFacets
    total: int
    results: list[SearchResult]
    next_cursor: str | None


class _Cursor(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    version: int = Field(ge=CURSOR_VERSION, le=CURSOR_VERSION)
    snapshot_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_]+$")
    terms: list[str] = Field(min_length=1, max_length=MAX_TERMS)
    sort: Literal["relevance", "date"]
    channel: str | None
    year: int | None
    offset: int = Field(ge=0, le=2**31 - 1)
    limit: int = Field(ge=1, le=50)


@dataclass(frozen=True)
class SearchOptions:
    sort: Literal["relevance", "date"] = "relevance"
    channel: str | None = None
    year: int | None = None


DEFAULT_OPTIONS = SearchOptions()


@dataclass
class Entry:
    doc_id: str
    score: float
    matched_terms: list[str]
    match: Literal["keyword", "semantic", "both"]
    channel: str
    publication_date: str | None


@dataclass
class ResultSet:
    entries: list[Entry]
    units: list[QueryUnit]
    mode: Literal["keyword", "hybrid"] = "keyword"
    reranked: bool = False
    suggestion: str | None = None
    semantic: dict[str, SemanticHit] = field(default_factory=dict)


def parse_units(query: str) -> list[QueryUnit]:
    if len(query) > 512 or "\x00" in query:
        raise HTTPException(
            422, "Query must be at most 512 characters and contain no NUL"
        )
    units = query_units(query)
    if (
        not units
        or len(units) > MAX_TERMS
        or any(len(u.term) > MAX_TERM_LENGTH for u in units)
    ):
        raise HTTPException(
            422, "Provide 1–64 searchable words, each at most 64 characters"
        )
    return units


def parse_terms(query: str) -> list[str]:
    return [u.term for u in parse_units(query)]


def _decode_cursor(
    value: str, terms: list[str], limit: int, options: SearchOptions | None = None
) -> _Cursor:
    options = options or DEFAULT_OPTIONS
    try:
        if len(value) > 16384 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("invalid encoding")
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = _Cursor.model_validate(json.loads(raw))
        if (
            decoded.terms != terms
            or decoded.limit != limit
            or (decoded.sort, decoded.channel, decoded.year)
            != (options.sort, options.channel, options.year)
        ):
            raise ValueError("query, filters or page size changed")
        return decoded
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise HTTPException(
            422, "Invalid search cursor or changed query/filters/page size"
        ) from exc


def _encode_cursor(
    snapshot_id: str,
    terms: list[str],
    offset: int,
    limit: int,
    options: SearchOptions = DEFAULT_OPTIONS,
) -> str:
    data = _Cursor(
        version=CURSOR_VERSION,
        snapshot_id=snapshot_id,
        terms=terms,
        sort=options.sort,
        channel=options.channel,
        year=options.year,
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


def _title_index(db, snapshot: Snapshot) -> dict[str, dict]:
    """Per snapshot: each transcript's title words, channel and date."""
    with _titles_lock:
        cached = _titles.get(snapshot.snapshot_id)
    if cached is not None:
        return cached
    index = {
        row["doc_id"]: dict(row, words=analyze(row["source_title"]))
        for row in transcript_titles(db, snapshot.schema_name)
    }
    with _titles_lock:
        for old in list(_titles)[:-1]:
            del _titles[old]
        _titles[snapshot.snapshot_id] = index
    return index


def title_factor(words: dict[str, list[int]], units: list[QueryUnit]) -> float:
    """1 + TITLE_BONUS x (weight of units whose variant is in the title words)."""
    found = sum(
        unit.weight
        for unit in units
        if any(variant_positions(words, v) for v in unit.variants)
    )
    return 1 + TITLE_BONUS * found / sum(unit.weight for unit in units)


def keyword_entries(rows: list[dict], units: list[QueryUnit], titles) -> list[Entry]:
    """Cut far-weaker bodies, then reorder by the title bonus."""
    best = max((row["body_score"] for row in rows), default=0)
    entries = [
        Entry(
            doc_id=row["doc_id"],
            score=row["body_score"]
            * title_factor(titles.get(row["doc_id"], {}).get("words", {}), units),
            matched_terms=row["matched_terms"],
            match="keyword",
            channel=row["channel"],
            publication_date=row["publication_date"],
        )
        for row in rows
        if row["body_score"] >= RELATIVE_CUTOFF * best
    ]
    return sorted(entries, key=lambda e: (-e.score, e.doc_id))


def _edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def suggest(db, schema: str, query: str, units, matched: set[str]) -> str | None:
    """The query with each unmatched core word replaced by its closest indexed word.

    A word may have lost a neighbouring stopword character in analysis (泽连斯其
    keeps 泽连斯), so the span with that character is tried too. Closest means
    fewest edits, then a same-length substitution, then the most frequent word.
    """
    suggestion = normalize(query)
    changed = False
    for unit in units:
        if unit.term in matched or not unit.core:
            continue
        at = suggestion.find(unit.term)
        spans = [unit.term]
        if at >= 0:
            end = at + len(unit.term)
            if end < len(suggestion) and suggestion[end].isalnum():
                spans.append(suggestion[at : end + 1])
            if at > 0 and suggestion[at - 1].isalnum():
                spans.append(suggestion[at - 1 : end])
        best = None
        for span in spans:
            limit = 1 if len(span) <= 4 else 2
            for term, df in lexicon_near(db, schema, span):
                distance = _edit_distance(span, term)
                rank = (distance, abs(len(term) - len(span)), -df, term)
                if 0 < distance <= limit and (best is None or rank < best[0]):
                    best = (rank, span, term)
        if best:
            suggestion = suggestion.replace(best[1], best[2], 1)
            changed = True
    return suggestion if changed else None


def _excerpt(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= SEMANTIC_EXCERPT else text[:SEMANTIC_EXCERPT] + "…"


def _rerank_text(db, snapshot, entries, result_set, titles) -> list[str]:
    pages = page_documents(
        db, snapshot.schema_name, [e.doc_id for e in entries], result_set.units
    )
    texts = []
    for entry in entries:
        title = titles.get(entry.doc_id, {}).get("source_title", "")
        hit = result_set.semantic.get(entry.doc_id)
        if hit is not None:
            body = hit.text
        else:
            page = pages.get(entry.doc_id, {})
            body = " ".join(
                part.text
                for snippet in make_snippets(
                    page.get("body_text", ""),
                    page.get("normalized_text", ""),
                    page.get("sections", []),
                    list(page.get("positions", {})),
                    page.get("positions", {}),
                )
                for part in snippet.parts
            )
        texts.append(f"{title}\n{body}"[:RERANK_CHARS])
    return texts


def build_result_set(db, snapshot: Snapshot, query: str, units) -> ResultSet:
    # The embedding call is the slow part of a question: start it first.
    semantic = (
        _models.submit(
            semantic_hits, snapshot.schema_name, snapshot.format_version, query
        )
        if is_question(query, units)
        else None
    )
    titles = _title_index(db, snapshot)
    rows = rank_documents(db, snapshot.schema_name, units)
    used = units
    split = split_phrases(units)
    if not rows and split and split != units:
        rows = rank_documents(db, snapshot.schema_name, split)
        used = split
    matched = (
        {term for row in rows for term in row["matched_terms"]}
        if used is units
        else set()
    )
    result = ResultSet(entries=keyword_entries(rows, used, titles), units=used)
    result.suggestion = suggest(db, snapshot.schema_name, query, units, matched)

    if semantic is not None:
        hits = semantic.result()
        if hits:
            result.mode = "hybrid"
            result.semantic = {hit.doc_id: hit for hit in hits}
            keyword = {e.doc_id: e for e in result.entries}
            fused = fuse(list(keyword), [hit.doc_id for hit in hits])
            entries = []
            for doc_id, score in fused.items():
                if doc_id in keyword:
                    entry = replace(keyword[doc_id], score=score)
                    if doc_id in result.semantic:
                        entry.match = "both"
                elif doc_id in titles:
                    entry = Entry(
                        doc_id=doc_id,
                        score=score,
                        matched_terms=[],
                        match="semantic",
                        channel=titles[doc_id]["channel"],
                        publication_date=titles[doc_id]["publication_date"],
                    )
                else:
                    continue
                entries.append(entry)
            result.entries = sorted(entries, key=lambda e: (-e.score, e.doc_id))
            top = result.entries[:RERANK_TOP]
            if rerank_enabled() and len(top) > 1:
                scores = rerank(query, _rerank_text(db, snapshot, top, result, titles))
                if scores is not None:
                    for entry, score in zip(top, scores, strict=True):
                        # Above every fused score (< 0.04), in reranked order.
                        entry.score = 1 + score
                    result.entries[: len(top)] = sorted(
                        top, key=lambda e: (-e.score, e.doc_id)
                    )
                    result.reranked = True
    return result


def facets(entries: list[Entry]) -> SearchFacets:
    channels: dict[str, int] = {}
    years: dict[str, int] = {}
    for entry in entries:
        channels[entry.channel] = channels.get(entry.channel, 0) + 1
        if entry.publication_date and entry.publication_date[:4].isdigit():
            year = entry.publication_date[:4]
            years[year] = years.get(year, 0) + 1
    return SearchFacets(
        channels=[
            Facet(value=k, count=v)
            for k, v in sorted(channels.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        years=[Facet(value=k, count=v) for k, v in sorted(years.items(), reverse=True)],
    )


def select(entries: list[Entry], options: SearchOptions) -> list[Entry]:
    chosen = [
        e
        for e in entries
        if (options.channel is None or e.channel == options.channel)
        and (
            options.year is None
            or (e.publication_date or "").startswith(str(options.year))
        )
    ]
    if options.sort == "date":
        # Newest first; undated transcripts last; ties keep relevance order.
        order = {e.doc_id: i for i, e in enumerate(chosen)}
        chosen.sort(
            key=lambda e: (
                e.publication_date is None,
                "" if e.publication_date is None else _invert(e.publication_date),
                order[e.doc_id],
            )
        )
    return chosen


def _invert(date: str) -> str:
    return "".join(chr(0x10FFFF - ord(c)) for c in date)


def _semantic_snippet(hit: SemanticHit) -> SearchSnippet:
    return SearchSnippet(
        heading=hit.heading,
        heading_anchor=hit.heading_anchor,
        parts=[SnippetPart(text=_excerpt(hit.text), highlight=False)],
    )


def _result(entry: Entry, page: dict, result_set: ResultSet) -> SearchResult:
    snippets = []
    if entry.match != "semantic":
        positions = page.get("positions", {})
        snippets = make_snippets(
            page["body_text"],
            page["normalized_text"],
            page["sections"],
            list(positions),
            positions,
        )
    if not snippets and entry.doc_id in result_set.semantic:
        snippets = [_semantic_snippet(result_set.semantic[entry.doc_id])]
    return SearchResult(
        doc_id=entry.doc_id,
        title=page["title"],
        source_title=page["source_title"],
        channel=page["channel"],
        publication_date=page["publication_date"],
        score=entry.score,
        match=entry.match,
        snippets=snippets,
        matched_terms=entry.matched_terms,
    )


def _search_snapshot(db, current: Snapshot) -> Snapshot:
    """The snapshot whose words were segmented with this agent's word list.

    Query and index must share the analyzer: a word added to search_words.txt
    is looked up whole, and a snapshot segmented without it would miss it. So
    search reads the newest published snapshot built with this SEARCH_VERSION
    (preferring the one chat uses), and reports not ready until the indexer
    publishes one. An older agent keeps reading the older lineage, which
    snapshot GC keeps for 7 days after it is superseded.
    """
    db.execute(
        "SELECT snapshot_id, schema_name, format_version, embedding_space "
        "FROM rag_meta.snapshots WHERE status = 'published' AND format_version = %s "
        "AND embedding_space = %s AND stats->>'search_version' = %s "
        "AND (%s = '' OR snapshot_id = %s) "
        "ORDER BY snapshot_id = %s DESC, published_at DESC, snapshot_id DESC LIMIT 1",
        (
            SEARCH_FORMAT,
            agent_embedding_space(),
            str(SEARCH_VERSION),
            settings.rag_snapshot,
            settings.rag_snapshot,
            current.snapshot_id,
        ),
    )
    row = db.fetchone()
    if row is None:
        raise HTTPException(503, "Search index is not ready")
    return Snapshot(**row)


@retry_on_transient_error()
def search(
    query: str,
    limit: int = 20,
    cursor: str | None = None,
    options: SearchOptions = DEFAULT_OPTIONS,
) -> SearchResponse:
    units = parse_units(query)
    terms = [u.term for u in units]
    decoded = (
        _decode_cursor(cursor, terms, limit, options) if cursor is not None else None
    )
    offset = decoded.offset if decoded else 0
    try:
        current = snapshot_manager.require() if not decoded else None
        with read_cursor() as db:
            if decoded:
                db.execute(
                    "SELECT snapshot_id, schema_name, format_version, embedding_space "
                    "FROM rag_meta.snapshots WHERE snapshot_id = %s AND status = 'published' "
                    "AND format_version = %s AND embedding_space = %s "
                    "AND stats->>'search_version' = %s",
                    (
                        decoded.snapshot_id,
                        SEARCH_FORMAT,
                        agent_embedding_space(),
                        str(SEARCH_VERSION),
                    ),
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
                assert current is not None
                snapshot = _search_snapshot(db, current)
            if snapshot.format_version != SEARCH_FORMAT:
                raise HTTPException(503, "Search index is not ready")
            # Hold table locks through cache lookup too: GC cannot make a cached
            # response appear to continue a snapshot that has already vanished.
            db.execute(
                sql.SQL(
                    "LOCK TABLE {s}.search_documents, {s}.search_statistics, "
                    "{s}.search_postings, {s}.documents, {s}.transcripts "
                    "IN ACCESS SHARE MODE"
                ).format(s=sql.Identifier(snapshot.schema_name))
            )
            set_key = json.dumps(
                [snapshot.snapshot_id, normalize(query), [repr(u) for u in units]],
                ensure_ascii=True,
            )
            key = json.dumps(
                [set_key, limit, offset, options.sort, options.channel, options.year],
                ensure_ascii=True,
            )
            cached = _cache.get(key)
            if cached is not None:
                return cached.model_copy(update={"query": query})
            result_set = _result_sets.get(set_key)
            if result_set is None:
                result_set = build_result_set(db, snapshot, query, units)
                _result_sets.put(set_key, result_set)
            chosen = select(result_set.entries, options)
            page = chosen[offset : offset + limit]
            pages = page_documents(
                db, snapshot.schema_name, [e.doc_id for e in page], result_set.units
            )
            results = [_result(e, pages[e.doc_id], result_set) for e in page]
            next_offset = offset + len(results)
            response = SearchResponse(
                query=query,
                terms=terms,
                snapshot_id=snapshot.snapshot_id,
                mode=result_set.mode,
                reranked=result_set.reranked,
                suggestion=result_set.suggestion,
                sort=options.sort,
                channel=options.channel,
                year=options.year,
                facets=facets(result_set.entries),
                total=len(chosen),
                results=results,
                next_cursor=_encode_cursor(
                    snapshot.snapshot_id, terms, next_offset, limit, options
                )
                if results and next_offset < len(chosen)
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
