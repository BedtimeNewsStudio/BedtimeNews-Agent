"""Keyword API against real PostgreSQL, with adversarial document fixtures."""

import base64
import json
import math

import pytest
from fastapi.testclient import TestClient
from psycopg2 import sql
from psycopg2.extras import Json
from src import main, search
from src.cache import LRUCache
from src.search_text import analyze, lexeme, term_weight
from src.snapshots import snapshot_manager
from test_snapshots import REGISTRY, needs_db, register, selected
from test_snapshots import db as db  # Reuse isolated PostgreSQL database fixture.


def seed(conn, sid, bodies):
    register(conn, sid, fmt=4)
    schema = sql.Identifier(f"rag_{sid}")
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("""
            CREATE SCHEMA {s};
            CREATE TABLE {s}.search_documents (
                doc_id text PRIMARY KEY, body_text text, normalized_text text,
                token_count integer, token_data jsonb, search_terms tsvector, sections jsonb);
            CREATE INDEX search_gin ON {s}.search_documents USING gin(search_terms);
            CREATE TABLE {s}.search_statistics (document_count integer, average_token_count double precision);
            CREATE TABLE {s}.documents (doc_id text PRIMARY KEY, title text);
            CREATE TABLE {s}.transcripts (
                doc_id text PRIMARY KEY, source_title text, channel text, publication_date text);
        """).format(s=schema)
        )
        for doc_id, body in bodies.items():
            normalized = body.lower()  # Test bodies use ASCII case or Chinese.
            token_data = analyze(normalized)
            lexemes = [lexeme(term) for term in token_data]
            sections = []
            pos = 0
            for index, text in enumerate(body.split("\n\n")):
                sections.append(
                    {
                        "start": pos,
                        "end": pos + len(text),
                        "heading": f"话题{index}",
                        "heading_anchor": f"话题{index}",
                    }
                )
                pos += len(text) + 2
            cur.execute(
                sql.SQL(
                    "INSERT INTO {s}.search_documents VALUES (%s,%s,%s,%s,%s,array_to_tsvector(%s::text[]),%s)"
                ).format(s=schema),
                (
                    doc_id,
                    body,
                    normalized,
                    sum(map(len, token_data.values())),
                    Json(token_data),
                    lexemes,
                    Json(sections),
                ),
            )
            cur.execute(
                sql.SQL("INSERT INTO {s}.documents VALUES (%s,%s)").format(s=schema),
                (doc_id, f"标准标题 {doc_id}"),
            )
            cur.execute(
                sql.SQL(
                    "INSERT INTO {s}.transcripts VALUES (%s,%s,'频道','2026-10-18')"
                ).format(s=schema),
                (doc_id, "仅标题词不得命中"),
            )
        cur.execute(
            sql.SQL(
                "INSERT INTO {s}.search_statistics SELECT count(*), COALESCE(avg(token_count),0) FROM {s}.search_documents WHERE token_count > 0"
            ).format(s=schema)
        )


@pytest.fixture
def client(db, monkeypatch):
    with db.cursor() as cur:
        cur.execute(REGISTRY)
    seed(
        db,
        "search",
        {
            "a.md": "贵州债务",
            "b.md": "贵州\n\n" + "无关" * 150 + "\n\n债务债务",
            "c.md": "地方其他内容方债其他内容债务",  # All bigrams, no full keyword.
            "d.md": "地方债务 OpenAI 𠀀 e-mail 100% a_b | & !",
            "e.md": "贵州债务",
            "f.md": "债务问题现状成因分析",
        },
    )
    snapshot_manager.set_for_tests(selected(db))
    monkeypatch.setattr(snapshot_manager, "start", lambda: None)
    monkeypatch.setattr(search, "_cache", LRUCache())
    with TestClient(main.app) as client:
        yield client
    snapshot_manager.set_for_tests(None, "reset")


@needs_db
def test_exact_and_cross_topic_matches_are_ranked_before_pagination(client):
    response = client.get("/search", params={"q": "贵州 债务"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 6
    assert [r["doc_id"] for r in data["results"][:2]] == ["a.md", "e.md"]
    cross_topic = next(r for r in data["results"] if r["doc_id"] == "b.md")
    assert cross_topic["matched_terms"] == ["贵州", "债务"]
    snippets = cross_topic["snippets"]
    highlights = {
        p["text"] for part in snippets for p in part["parts"] if p["highlight"]
    }
    assert "贵州" in highlights and "债务债务" in highlights
    assert len({part["heading_anchor"] for part in snippets}) == 2


@needs_db
@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("OPENAI", ["d.md"]),
        ("AI", []),
        ("𠀀", ["d.md"]),
        ("100%", ["d.md"]),
        ("a_b", ["d.md"]),
        ("仅标题词", []),
    ],
)
def test_word_matching_ignores_case_without_substring_false_positives(
    client, term, expected
):
    response = client.get("/search", params={"q": term})
    assert response.status_code == 200, response.text
    assert sorted(r["doc_id"] for r in response.json()["results"]) == sorted(expected)


@needs_db
def test_natural_sentence_recalls_core_topic_without_requiring_every_word(client):
    response = client.get("/search", params={"q": "贵州的债务问题现状及其成因"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["terms"] == ["贵州", "债务", "问题", "现状", "成因"]
    assert [r["doc_id"] for r in data["results"][:2]] == ["a.md", "e.md"]
    assert data["results"][0]["matched_terms"] == ["贵州", "债务"]
    assert next(i for i, r in enumerate(data["results"]) if r["doc_id"] == "f.md") > 1
    # Out-of-vocabulary query words do not suppress useful partial matches.
    assert (
        client.get("/search", params={"q": "贵州 zznotincorpus"}).json()["total"] == 3
    )


@needs_db
def test_score_matches_word_bm25_coverage_and_proximity_formula(client):
    data = client.get("/search", params={"q": "贵州 债务"}).json()
    bodies = [
        "贵州债务",
        "贵州\n\n" + "无关" * 150 + "\n\n债务债务",
        "地方其他内容方债其他内容债务",
        "地方债务 OpenAI 𠀀 e-mail 100% a_b | & !",
        "贵州债务",
        "债务问题现状成因分析",
    ]
    tokens = [analyze(body) for body in bodies]
    average = sum(sum(map(len, t.values())) for t in tokens) / len(tokens)
    length = sum(map(len, tokens[0].values()))
    expected = 0
    for term in data["terms"]:
        df = sum(term in row for row in tokens)
        tf = len(tokens[0].get(term, []))
        expected += (
            term_weight(term)
            * math.log(1 + (len(tokens) - df + 0.5) / (df + 0.5))
            * tf
            * 2.2
            / (tf + 1.2 * (0.25 + 0.75 * length / average))
        )
    assert data["results"][0]["score"] == pytest.approx(expected * 1.15)


@needs_db
def test_cursor_stays_on_snapshot_after_publication_and_expires_after_retirement(
    client, db
):
    first = client.get("/search", params={"q": "贵州 债务", "limit": 1}).json()
    assert first["next_cursor"]
    seed(db, "new", {"new.md": "贵州债务"})
    snapshot_manager.set_for_tests(selected(db))
    assert (
        client.get("/search", params={"q": "贵州 债务"}).json()["snapshot_id"] == "new"
    )
    second = client.get(
        "/search", params={"q": "贵州 债务", "limit": 1, "cursor": first["next_cursor"]}
    ).json()
    assert second["snapshot_id"] == "search"
    assert second["results"][0]["doc_id"] == "e.md"
    third = client.get(
        "/search",
        params={"q": "贵州 债务", "limit": 1, "cursor": second["next_cursor"]},
    ).json()
    assert third["snapshot_id"] == "search"
    assert third["results"] and third["next_cursor"]
    with db.cursor() as cur:
        cur.execute(
            "UPDATE rag_meta.snapshots SET status = 'retired' WHERE snapshot_id = 'search'"
        )
    assert (
        client.get(
            "/search",
            params={"q": "贵州 债务", "limit": 1, "cursor": first["next_cursor"]},
        ).status_code
        == 410
    )


@needs_db
def test_old_snapshot_reports_search_not_ready(client, db):
    register(db, "old", fmt=2)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE rag_meta.snapshots SET status = 'retired' WHERE format_version = 4"
        )
    snapshot_manager.set_for_tests(selected(db))
    response = client.get("/search", params={"q": "债务"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Search index is not ready"


@needs_db
def test_gin_is_available_for_two_character_candidate_lookup(db, client):
    with db.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute(
            "EXPLAIN SELECT doc_id FROM rag_search.search_documents WHERE search_terms @@ %s::tsquery",
            (search.keyword_query("债务"),),
        )
        assert "search_gin" in "\n".join(row[0] for row in cur.fetchall())
        cur.execute("RESET enable_seqscan")


@needs_db
def test_corpus_with_no_searchable_words_returns_empty_results(client, db):
    seed(db, "empty_tokens", {"stop.md": "的和及其"})
    snapshot_manager.set_for_tests(selected(db))
    response = client.get("/search", params={"q": "贵州的债务问题"})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert response.json()["results"] == []


@needs_db
def test_cursor_with_nul_snapshot_id_is_rejected_as_input_error(client):
    valid = search._encode_cursor("search", ["债务"], 0, 20)
    payload = json.loads(base64.urlsafe_b64decode(valid + "=" * (-len(valid) % 4)))
    payload["snapshot_id"] = "bad\x00id"
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    response = client.get("/search", params={"q": "债务", "cursor": cursor})
    assert response.status_code == 422, response.text


@needs_db
def test_proximity_uses_indexed_words_not_substrings_of_other_words(client, db):
    seed(
        db,
        "proximity",
        {
            "a.md": "ai " + "x" * 20 + " data openai data",
            "b.md": "ai data " + "x" * 20 + " openai data",
        },
    )
    snapshot_manager.set_for_tests(selected(db))
    response = client.get("/search", params={"q": "ai data"})
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    # Both documents have identical term frequencies and token counts. Only b's
    # actual indexed 'ai' is near 'data'; the 'ai' inside 'openai' does not count.
    assert [row["doc_id"] for row in results] == ["b.md", "a.md"]
    assert results[0]["score"] == pytest.approx(results[1]["score"] * 1.15)
