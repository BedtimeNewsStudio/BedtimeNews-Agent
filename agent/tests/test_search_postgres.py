"""Keyword API against real PostgreSQL, with adversarial document fixtures."""

import base64
import json
import math

import pytest
from fastapi.testclient import TestClient
from psycopg2 import sql
from psycopg2.extras import Json
from src import main, search, search_db
from src.cache import LRUCache
from src.search_text import analyze, lexeme, term_weight
from src.snapshots import snapshot_manager
from test_snapshots import REGISTRY, needs_db, register, selected
from test_snapshots import db as db  # Reuse isolated PostgreSQL database fixture.


def seed(conn, sid, bodies):
    register(conn, sid, fmt=5)
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
            CREATE TABLE {s}.search_postings (term text PRIMARY KEY, document_frequency integer, postings jsonb);
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
        cur.execute(
            sql.SQL(
                "INSERT INTO {s}.search_postings SELECT e.key, count(*), jsonb_object_agg(d.doc_id, e.value) "
                "FROM {s}.search_documents d CROSS JOIN LATERAL jsonb_each(d.token_data) e GROUP BY e.key"
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
    monkeypatch.setattr(search, "_result_sets", LRUCache())
    monkeypatch.setattr(search, "_titles", {})
    # No model calls in tests: questions get keyword ranking only.
    monkeypatch.setattr(search, "semantic_hits", lambda *args: [])
    with TestClient(main.app) as client:
        yield client
    snapshot_manager.set_for_tests(None, "reset")


@needs_db
def test_exact_and_cross_topic_matches_are_ranked_before_pagination(
    client, monkeypatch
):
    # b.md's far-apart words score under the relative cutoff; keep it here to
    # check cross-topic snippets (the cutoff has its own test).
    monkeypatch.setattr(search, "RELATIVE_CUTOFF", 0)
    response = client.get("/search", params={"q": "贵州 债务"})
    assert response.status_code == 200, response.text
    data = response.json()
    # 债务 is in every fixture body, so 贵州 decides relevance; c/d/f lack it.
    assert data["total"] == 3
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
    # f.md matches 债务 and question words only: never enough to be relevant.
    assert "f.md" not in {r["doc_id"] for r in data["results"]}
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
    # Full coverage, adjacent in query order, no title match.
    assert data["results"][0]["score"] == pytest.approx(
        expected
        * (search_db.COVERAGE_FLOOR + 1 - search_db.COVERAGE_FLOOR)
        * search_db.PROXIMITY_BONUS
    )


@needs_db
def test_cursor_stays_on_snapshot_after_publication_and_expires_after_retirement(
    client, db, monkeypatch
):
    monkeypatch.setattr(search, "RELATIVE_CUTOFF", 0)
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
    assert third["results"] and third["next_cursor"] is None
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
    register(db, "old", fmt=4)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE rag_meta.snapshots SET status = 'retired' WHERE format_version = 5"
        )
    snapshot_manager.set_for_tests(selected(db))
    response = client.get("/search", params={"q": "债务"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Search index is not ready"


@needs_db
def test_postings_are_read_by_primary_key(db, client):
    with db.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute(
            "EXPLAIN SELECT postings FROM rag_search.search_postings WHERE term = ANY(%s)",
            (["债务", "贵州"],),
        )
        assert "search_postings_pkey" in "\n".join(row[0] for row in cur.fetchall())
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
    assert results[0]["score"] == pytest.approx(
        results[1]["score"] * search_db.PROXIMITY_BONUS
    )


@needs_db
def test_name_matches_only_as_a_whole_phrase(client, db):
    seed(
        db,
        "phrase",
        {
            "artemis.md": "阿尔忒弥斯登月计划推迟",
            "skydio.md": "斯凯迪奥公司的电池被切断",
            "bose.md": "钱德拉·鲍斯去了柏林",
            "split.md": "阿尔忒弥神话，斯里兰卡",
        },
    )
    snapshot_manager.set_for_tests(selected(db))
    data = client.get("/search", params={"q": "阿尔忒弥斯"}).json()
    assert data["terms"] == ["阿尔忒弥斯"]
    assert [r["doc_id"] for r in data["results"]] == ["artemis.md"]
    highlights = [
        p["text"]
        for s in data["results"][0]["snippets"]
        for p in s["parts"]
        if p["highlight"]
    ]
    assert highlights == ["阿尔忒弥斯"]


@needs_db
def test_phrase_found_nowhere_falls_back_to_its_words(client, db):
    seed(db, "fallback", {"dior.md": "迪奥的新品发布", "other.md": "斯里兰卡"})
    snapshot_manager.set_for_tests(selected(db))
    data = client.get("/search", params={"q": "斯凯迪奥"}).json()
    assert data["terms"] == ["斯凯迪奥"]
    assert [r["doc_id"] for r in data["results"]] == ["dior.md"]
    # A misspelled name leaves only fragments: no results rather than noise.
    assert client.get("/search", params={"q": "阿尔特弥斯"}).json()["total"] == 0


@needs_db
def test_results_far_below_the_best_body_score_are_dropped(client, db):
    bodies = {"top.md": "贵州债务", "tail.md": "贵州 " + "无关" * 3000 + " 债务"}
    bodies.update({f"filler{i}.md": "无关" for i in range(5)})
    seed(db, "cutoff", bodies)
    snapshot_manager.set_for_tests(selected(db))
    data = client.get("/search", params={"q": "贵州 债务"}).json()
    assert [r["doc_id"] for r in data["results"]] == ["top.md"]
    assert data["total"] == 1


@needs_db
def test_title_match_reorders_but_never_recalls(client, db):
    seed(db, "titled", {"x.md": "贵州债务", "y.md": "贵州债务", "z.md": "其他"})
    with db.cursor() as cur:
        cur.execute(
            "UPDATE rag_titled.transcripts SET source_title = '【睡前消息1】贵州债务' "
            "WHERE doc_id IN ('y.md', 'z.md')"
        )
    snapshot_manager.set_for_tests(selected(db))
    results = client.get("/search", params={"q": "贵州 债务"}).json()["results"]
    assert [r["doc_id"] for r in results] == ["y.md", "x.md"]
    assert results[0]["score"] == pytest.approx(results[1]["score"] * 2)


@needs_db
def test_alias_matches_any_spelling(client, db):
    seed(db, "alias", {"t.md": "特朗普访华", "c.md": "川普发推", "x.md": "其他新闻"})
    snapshot_manager.set_for_tests(selected(db))
    data = client.get("/search", params={"q": "川普"}).json()
    assert data["terms"] == ["川普"]
    assert sorted(r["doc_id"] for r in data["results"]) == ["c.md", "t.md"]
    hit = next(r for r in data["results"] if r["doc_id"] == "t.md")
    assert hit["matched_terms"] == ["川普"]
    assert [
        p["text"] for s in hit["snippets"] for p in s["parts"] if p["highlight"]
    ] == ["特朗普"]


@needs_db
def test_unknown_word_suggests_the_closest_indexed_word(client, db):
    seed(
        db, "typo", {"a.md": "阿尔忒弥斯计划", "b.md": "阿尔忒弥斯登月", "c.md": "其他"}
    )
    snapshot_manager.set_for_tests(selected(db))
    data = client.get("/search", params={"q": "阿尔特弥斯"}).json()
    assert data["total"] == 0
    assert data["suggestion"] == "阿尔忒弥斯"
    assert (
        client.get("/search", params={"q": "阿尔忒弥斯"}).json()["suggestion"] is None
    )


@needs_db
def test_order_filters_and_facets(client, db):
    seed(db, "facets", {"a.md": "星舰试飞", "b.md": "星舰发射", "c.md": "星舰回收"})
    with db.cursor() as cur:
        for doc_id, channel, date in [
            ("a.md", "GaoJian", "2025-03-01"),
            ("b.md", "GaoJian", "2026-01-01"),
            ("c.md", "CanKaoXinXi", "2024-05-01"),
        ]:
            cur.execute(
                "UPDATE rag_facets.transcripts SET channel = %s, publication_date = %s "
                "WHERE doc_id = %s",
                (channel, date, doc_id),
            )
    snapshot_manager.set_for_tests(selected(db))
    data = client.get("/search", params={"q": "星舰", "sort": "date"}).json()
    assert [r["doc_id"] for r in data["results"]] == ["b.md", "a.md", "c.md"]
    assert data["facets"]["channels"] == [
        {"value": "GaoJian", "count": 2},
        {"value": "CanKaoXinXi", "count": 1},
    ]
    assert [y["value"] for y in data["facets"]["years"]] == ["2026", "2025", "2024"]
    filtered = client.get(
        "/search", params={"q": "星舰", "channel": "GaoJian", "year": 2025, "limit": 1}
    ).json()
    assert [r["doc_id"] for r in filtered["results"]] == ["a.md"]
    assert filtered["total"] == 1 and filtered["next_cursor"] is None
    # Facets still count every result, so the menus do not shrink.
    assert filtered["facets"] == data["facets"]
    paged = client.get(
        "/search", params={"q": "星舰", "sort": "date", "limit": 1}
    ).json()
    second = client.get(
        "/search",
        params={
            "q": "星舰",
            "sort": "date",
            "limit": 1,
            "cursor": paged["next_cursor"],
        },
    )
    assert [r["doc_id"] for r in second.json()["results"]] == ["a.md"]
    # A cursor is bound to its order and filters.
    assert (
        client.get(
            "/search", params={"q": "星舰", "limit": 1, "cursor": paged["next_cursor"]}
        ).status_code
        == 422
    )


@needs_db
def test_question_fuses_semantic_recall_and_reranks(client, db, monkeypatch):
    from src.search_semantic import SemanticHit

    seed(
        db,
        "hybrid",
        {"k.md": "贵州债务很高", "s.md": "西南省份的财政困境", "x.md": "无关"},
    )
    snapshot_manager.set_for_tests(selected(db))
    hit = SemanticHit("s.md", 0.7, "财政", "财政", "西南省份的财政困境，借了很多钱")
    monkeypatch.setattr(search, "semantic_hits", lambda *args: [hit])
    data = client.get("/search", params={"q": "贵州为什么债务高"}).json()
    assert data["mode"] == "hybrid" and data["reranked"] is False
    by_id = {r["doc_id"]: r for r in data["results"]}
    assert set(by_id) == {"k.md", "s.md"}
    assert by_id["k.md"]["match"] == "keyword"
    assert by_id["s.md"]["match"] == "semantic"
    assert by_id["s.md"]["matched_terms"] == []
    assert by_id["s.md"]["snippets"][0]["heading_anchor"] == "财政"
    # A keyword search of a name never asks for semantic recall.
    assert client.get("/search", params={"q": "贵州 债务"}).json()["mode"] == "keyword"

    monkeypatch.setattr(search, "_result_sets", LRUCache())
    monkeypatch.setattr(search, "_cache", LRUCache())
    monkeypatch.setattr(search, "rerank_enabled", lambda: True)
    monkeypatch.setattr(
        search,
        "rerank",
        lambda query, texts: [0.9 if "西南" in t else 0.1 for t in texts],
    )
    reranked = client.get("/search", params={"q": "贵州为什么债务高"}).json()
    assert reranked["reranked"] is True
    assert [r["doc_id"] for r in reranked["results"]] == ["s.md", "k.md"]
