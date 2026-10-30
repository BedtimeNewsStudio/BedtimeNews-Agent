"""Snapshot-qualified keyword retrieval from search_postings, and page loading.

rank_documents() recalls and scores every relevant transcript in one statement
and returns light rows (no bodies). The caller cuts, reorders, filters and
pages them; page_documents() then loads bodies and word offsets for one page.
"""

from psycopg2 import sql

from .search_query import QueryUnit, Variant, sql_inputs

# A core unit in more than this share of transcripts says little about the
# topic (中国, 经济, 发展); it ranks but does not decide relevance while rarer
# core units exist.
COMMON_SHARE = 0.5
# Score multiplier when two consecutive query units occur within 12 characters
# of each other, in query order, on one line.
PROXIMITY_BONUS = 1.3
# score *= COVERAGE_FLOOR + (1 - COVERAGE_FLOOR) * coverage
COVERAGE_FLOOR = 0.1


def rank_documents(cursor, schema: str, units: list[QueryUnit]) -> list[dict]:
    """Every relevant transcript with its body score, best first.

    Recall reads only the query words' rows of search_postings. A variant with
    several words (a phrase) matches where all its words sit at their offsets.

    Relevance gate: the gate units are the core units that are not common;
    failing that, all core units; failing that, every unit. A transcript must
    match enough of them: all of up to two, two of three or four, half of more.
    When no transcript reaches that, the requirement drops to the best coverage
    any transcript has, so a query never empties just because its words never
    occur together.
    """
    params = sql_inputs(units)
    params.update(
        common=COMMON_SHARE,
        proximity=PROXIMITY_BONUS,
        floor=COVERAGE_FLOOR,
    )
    cursor.execute(
        sql.SQL(
            """
            WITH units AS (
                SELECT * FROM unnest(
                    %(terms)s::text[], %(weights)s::double precision[], %(cores)s::boolean[]
                ) WITH ORDINALITY AS u(term, weight, core, ordinal)
            ), variants AS (
                SELECT * FROM unnest(
                    %(variant_units)s::bigint[], %(variant_texts)s::text[],
                    %(variant_sizes)s::integer[], %(variant_anchors)s::text[],
                    %(variant_anchor_offsets)s::integer[]
                ) WITH ORDINALITY AS v(unit, text, size, anchor, anchor_offset, variant)
            ), parts AS (
                SELECT * FROM unnest(
                    %(part_variants)s::bigint[], %(part_words)s::text[],
                    %(part_offsets)s::integer[]
                ) AS p(variant, word, part_offset)
            ), postings AS MATERIALIZED (
                SELECT p.term AS word, e.key AS doc_id, e.value AS offsets
                FROM {s}.search_postings p CROSS JOIN LATERAL jsonb_each(p.postings) e
                WHERE p.term = ANY(%(words)s::text[])
            ), candidates AS MATERIALIZED (
                -- (variant, transcript) pairs where every word of the variant occurs.
                SELECT v.variant, v.unit, v.text, v.size, v.anchor, v.anchor_offset,
                       x.doc_id, jsonb_object_agg(x.word, x.offsets) AS offsets
                FROM variants v JOIN parts p USING (variant)
                JOIN postings x ON x.word = p.word
                GROUP BY v.variant, v.unit, v.text, v.size, v.anchor, v.anchor_offset, x.doc_id
                HAVING count(DISTINCT x.word) = (
                    SELECT count(DISTINCT q.word) FROM parts q WHERE q.variant = v.variant)
            ), hits AS MATERIALIZED (
                SELECT c.unit, c.text, c.doc_id,
                    CASE WHEN c.size = 1 THEN c.offsets -> c.anchor ELSE (
                        SELECT COALESCE(jsonb_agg(st.start ORDER BY st.start), '[]'::jsonb)
                        FROM (
                            SELECT a.position::integer - c.anchor_offset AS start
                            FROM jsonb_array_elements_text(c.offsets -> c.anchor) AS a(position)
                        ) st
                        WHERE st.start >= 0 AND NOT EXISTS (
                            SELECT 1 FROM parts p
                            WHERE p.variant = c.variant
                              AND NOT c.offsets -> p.word @> to_jsonb(st.start + p.part_offset)
                        )
                    ) END AS positions
                FROM candidates c
            ), matches AS MATERIALIZED (
                SELECT h.unit, h.doc_id, sum(jsonb_array_length(h.positions))::double precision AS tf
                FROM hits h WHERE jsonb_array_length(h.positions) > 0
                GROUP BY h.unit, h.doc_id
            ), frequencies AS (
                SELECT unit, count(*)::double precision AS df FROM matches GROUP BY unit
            ), tiers AS (
                SELECT f.unit, CASE
                    WHEN u.core AND f.df <= %(common)s * st.document_count THEN 1
                    WHEN u.core THEN 2 ELSE 3 END AS tier
                FROM frequencies f JOIN units u ON u.ordinal = f.unit
                CROSS JOIN {s}.search_statistics st
            ), gate AS (
                SELECT unit FROM tiers WHERE tier = (SELECT min(tier) FROM tiers)
            ), base AS MATERIALIZED (
                SELECT m.doc_id,
                    sum(u.weight * ln(1 + (st.document_count - f.df + 0.5) / (f.df + 0.5)) *
                        m.tf * 2.2 / (m.tf + 1.2 *
                            (0.25 + 0.75 * s.token_count / NULLIF(st.average_token_count, 0))))
                    * (%(floor)s + (1 - %(floor)s) * sum(u.weight)
                        / (SELECT sum(weight) FROM units)) AS base_score,
                    count(*) FILTER (WHERE m.unit IN (SELECT unit FROM gate)) AS gate_hits,
                    array_agg(u.term ORDER BY u.ordinal) AS matched_terms
                FROM matches m JOIN units u ON u.ordinal = m.unit
                JOIN frequencies f ON f.unit = m.unit
                JOIN {s}.search_documents s ON s.doc_id = m.doc_id
                CROSS JOIN {s}.search_statistics st
                GROUP BY m.doc_id
            ), required AS (
                SELECT LEAST(
                    CASE WHEN k <= 2 THEN k WHEN k <= 4 THEN 2 ELSE ceil(k / 2.0) END,
                    (SELECT max(gate_hits) FROM base)
                ) AS hits
                FROM (SELECT count(*) AS k FROM gate) g
            ), relevant AS MATERIALIZED (
                SELECT b.* FROM base b
                WHERE b.gate_hits >= GREATEST(1, (SELECT hits FROM required))
            ), events AS (
                -- Pair k: ends of unit k, then starts of unit k + 1.
                SELECT h.doc_id, h.unit AS pair,
                       a.position::integer + char_length(h.text) AS position, 0 AS kind
                FROM hits h CROSS JOIN LATERAL jsonb_array_elements_text(h.positions) AS a(position)
                WHERE h.doc_id IN (SELECT doc_id FROM relevant)
                UNION ALL
                SELECT h.doc_id, h.unit - 1, a.position::integer, 1
                FROM hits h CROSS JOIN LATERAL jsonb_array_elements_text(h.positions) AS a(position)
                WHERE h.unit > 1 AND h.doc_id IN (SELECT doc_id FROM relevant)
            ), close_pairs AS (
                SELECT doc_id, first_end, position FROM (
                    SELECT e.*, max(CASE WHEN kind = 0 THEN position END) OVER (
                        PARTITION BY doc_id, pair ORDER BY position, kind
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS first_end
                    FROM events e
                ) z
                WHERE kind = 1 AND position - first_end BETWEEN 0 AND 12
            ), near AS (
                SELECT DISTINCT c.doc_id
                FROM close_pairs c JOIN {s}.search_documents s USING (doc_id)
                WHERE strpos(substr(s.normalized_text, c.first_end + 1,
                    c.position - c.first_end), chr(10)) = 0
            )
            SELECT r.doc_id, r.matched_terms,
                   r.base_score * CASE WHEN r.doc_id IN (SELECT doc_id FROM near)
                       THEN %(proximity)s ELSE 1 END AS body_score,
                   t.source_title, t.channel, t.publication_date
            FROM relevant r JOIN {s}.transcripts t USING (doc_id)
            ORDER BY body_score DESC, r.doc_id COLLATE "C"
            """
        ).format(s=sql.Identifier(schema)),
        params,
    )
    return [dict(row) for row in cursor.fetchall()]


def variant_positions(offsets: dict[str, list[int]], variant: Variant) -> list[int]:
    """Starts where every word of the variant sits at its offset."""
    words = {word: set(offsets.get(word, ())) for word, _ in variant.parts}
    anchor, anchor_offset = variant.anchor
    return [
        start
        for p in offsets.get(anchor, ())
        if (start := p - anchor_offset) >= 0
        and all(start + offset in words[word] for word, offset in variant.parts)
    ]


def page_documents(
    cursor, schema: str, doc_ids: list[str], units: list[QueryUnit]
) -> dict[str, dict]:
    """Bodies, sections, titles and matched variant offsets for one page."""
    if not doc_ids:
        return {}
    cursor.execute(
        sql.SQL(
            """
            SELECT s.doc_id, d.title, t.source_title, t.channel, t.publication_date,
                   s.body_text, s.normalized_text, s.sections
            FROM {s}.search_documents s
            JOIN {s}.documents d USING (doc_id)
            JOIN {s}.transcripts t USING (doc_id)
            WHERE s.doc_id = ANY(%s)
            """
        ).format(s=sql.Identifier(schema)),
        (doc_ids,),
    )
    pages = {row["doc_id"]: dict(row, positions={}) for row in cursor.fetchall()}
    words = sorted({w for u in units for v in u.variants for w, _ in v.parts})
    if not words:
        return pages
    cursor.execute(
        sql.SQL(
            "SELECT p.term, e.key AS doc_id, e.value AS offsets "
            "FROM {s}.search_postings p CROSS JOIN LATERAL jsonb_each(p.postings) e "
            "WHERE p.term = ANY(%s) AND e.key = ANY(%s)"
        ).format(s=sql.Identifier(schema)),
        (words, doc_ids),
    )
    offsets: dict[str, dict[str, list[int]]] = {}
    for row in cursor.fetchall():
        offsets.setdefault(row["doc_id"], {})[row["term"]] = row["offsets"]
    for doc_id, page in pages.items():
        for unit in units:
            for variant in unit.variants:
                if hits := variant_positions(offsets.get(doc_id, {}), variant):
                    page["positions"][variant.text] = hits
    return pages


def lexicon_near(cursor, schema: str, term: str) -> list[tuple[str, int]]:
    """Indexed words of similar length sharing the first or last character."""
    cursor.execute(
        sql.SQL(
            "SELECT term, document_frequency FROM {s}.search_postings "
            "WHERE char_length(term) BETWEEN %s AND %s AND document_frequency >= 2 "
            "AND (left(term, 1) = %s OR right(term, 1) = %s)"
        ).format(s=sql.Identifier(schema)),
        (len(term) - 1, len(term) + 1, term[:1], term[-1:]),
    )
    return [(row["term"], row["document_frequency"]) for row in cursor.fetchall()]


def transcript_titles(cursor, schema: str) -> list[dict]:
    cursor.execute(
        sql.SQL(
            "SELECT doc_id, source_title, channel, publication_date FROM {s}.transcripts"
        ).format(s=sql.Identifier(schema))
    )
    return [dict(row) for row in cursor.fetchall()]
