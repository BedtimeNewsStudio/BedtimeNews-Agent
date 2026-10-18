"""Snapshot-qualified word retrieval and BM25/coverage/proximity ranking."""

from psycopg2 import sql

from .search_text import proximity_patterns, term_weight


def query_search(
    cursor, schema: str, terms: list[str], queries: list[str], limit: int, offset: int
):
    """Recall any indexed query word and rank the entire union before paging.

    The regex is only a cheap proximity prefilter. Sorted start/end events find
    the closest preceding first-word end for each second-word occurrence,
    without either substring matches or a Cartesian product of postings.
    """
    cursor.execute(
        sql.SQL(
            """
            WITH input AS (
                SELECT term, query::tsquery, weight, ordinal
                FROM unnest(%s::text[], %s::text[], %s::double precision[])
                    WITH ORDINALITY AS i(term, query, weight, ordinal)
            ), pairs AS (
                SELECT term, lead(term) OVER (ORDER BY ordinal) AS next_term FROM input
            ), matches AS MATERIALIZED (
                SELECT s.doc_id, i.term, i.weight, i.ordinal, s.token_count,
                       s.token_data -> i.term AS positions,
                       jsonb_array_length(s.token_data -> i.term)::double precision AS tf
                FROM input i JOIN {s}.search_documents s
                  ON s.search_terms @@ i.query AND s.token_data ? i.term
            ), frequencies AS (
                SELECT term, count(*)::double precision AS df FROM matches GROUP BY term
            ), base AS MATERIALIZED (
                SELECT m.doc_id,
                    sum(m.weight * ln(1 + (st.document_count - f.df + 0.5) / (f.df + 0.5)) *
                        m.tf * 2.2 / (m.tf + 1.2 *
                            (0.25 + 0.75 * m.token_count / NULLIF(st.average_token_count, 0))))
                    * (0.25 + 0.75 * sum(m.weight) / (SELECT sum(weight) FROM input)) AS base_score,
                    array_agg(m.term ORDER BY m.ordinal) AS matched_terms,
                    jsonb_object_agg(m.term, m.positions) AS matched_positions
                FROM matches m JOIN frequencies f USING (term)
                CROSS JOIN {s}.search_statistics st
                GROUP BY m.doc_id
            ), ranked AS MATERIALIZED (
                SELECT b.*, b.base_score * CASE
                    WHEN cardinality(b.matched_terms) > 1
                    AND s.normalized_text ~ ANY(%s::text[])
                    AND EXISTS (
                        SELECT 1 FROM pairs p CROSS JOIN LATERAL (
                            SELECT position, kind,
                                max(CASE WHEN kind = 0 THEN position END) OVER (
                                    ORDER BY position, kind
                                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                                ) AS first_end
                            FROM (
                                SELECT a.position::integer + char_length(p.term) AS position, 0 AS kind
                                FROM jsonb_array_elements_text(b.matched_positions -> p.term) AS a(position)
                                UNION ALL
                                SELECT z.position::integer, 1
                                FROM jsonb_array_elements_text(b.matched_positions -> p.next_term) AS z(position)
                            ) events
                        ) nearby
                        WHERE p.next_term IS NOT NULL AND nearby.kind = 1
                          AND nearby.position - nearby.first_end BETWEEN 0 AND 12
                          AND strpos(substr(s.normalized_text, nearby.first_end + 1,
                              nearby.position - nearby.first_end), chr(10)) = 0
                    )
                    THEN 1.15 ELSE 1 END AS score
                FROM base b JOIN {s}.search_documents s USING (doc_id)
            ), page AS (
                SELECT r.doc_id, r.score, r.matched_terms, r.matched_positions,
                       d.title, t.source_title, t.channel, t.publication_date,
                       s.body_text, s.normalized_text, s.sections
                FROM ranked r JOIN {s}.search_documents s USING (doc_id)
                JOIN {s}.documents d USING (doc_id)
                JOIN {s}.transcripts t USING (doc_id)
                ORDER BY r.score DESC, r.doc_id COLLATE "C" ASC
                LIMIT %s OFFSET %s
            )
            SELECT (SELECT count(*) FROM ranked) AS total,
                   COALESCE((SELECT jsonb_agg(to_jsonb(p)
                       ORDER BY p.score DESC, p.doc_id COLLATE "C" ASC)
                       FROM page p), '[]'::jsonb) AS results
            """
        ).format(s=sql.Identifier(schema)),
        (
            terms,
            queries,
            [term_weight(t) for t in terms],
            proximity_patterns(terms),
            limit,
            offset,
        ),
    )
    return cursor.fetchone()
