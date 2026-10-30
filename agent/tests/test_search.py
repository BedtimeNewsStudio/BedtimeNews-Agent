"""Keyword syntax, cursor validation, snippets, and API failures."""

import base64
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from src import main, search, search_semantic
from src.search_query import is_question, query_units, split_phrases
from src.snapshots import Snapshot, snapshot_manager


def test_queries_are_segmented_normalized_and_deduplicated():
    assert search.parse_terms("  贵州\u3000债务\t贵州 OpenAI OPENAI ") == [
        "贵州",
        "债务",
        "openai",
    ]
    assert search.parse_terms("贵州的债务问题现状及其成因") == [
        "贵州",
        "债务",
        "问题",
        "现状",
        "成因",
    ]
    # A word split off a single character is matched as one phrase.
    assert search.parse_terms("Cafe\u0301") == ["café"]


def test_name_fragments_join_into_one_phrase():
    # Discovered words are whole; names still missing are rejoined phrases.
    assert [(u.term, u.parts) for u in query_units("阿尔忒弥斯")] == [
        ("阿尔忒弥斯", (("阿尔忒弥斯", 0),))
    ]
    units = query_units("斯凯迪奥")
    assert [(u.term, u.parts, u.core) for u in units] == [
        ("斯凯迪奥", (("斯", 0), ("凯", 1), ("迪奥", 2)), True)
    ]
    assert units[0].anchor == ("迪奥", 2)
    assert [u.term for u in query_units("钱德拉·鲍斯")] == ["钱德拉", "鲍斯"]
    # Separated by a space, two names stay two units.
    assert [u.term for u in query_units("星舰 阿尔忒弥斯")] == ["星舰", "阿尔忒弥斯"]


def test_aliases_are_variants_of_one_unit():
    units = query_units("川普 演讲")
    assert [u.term for u in units] == ["川普", "演讲"]
    assert [v.text for v in units[0].variants] == ["川普", "特朗普", "trump"]
    # Touching words that together spell an alias become that one unit.
    gdp = query_units("中国国内生产总值")[1]
    assert gdp.term == "国内生产总值"
    assert [v.text for v in gdp.variants] == ["国内生产总值", "gdp"]
    assert gdp.variants[0].parts == (("国内", 0), ("生产总值", 2))


def test_questions_get_semantic_recall_names_do_not():
    for query in (
        "为什么年轻人不愿意生孩子",
        "贵州的债务问题现状及其成因",
        "房价还会跌吗",
    ):
        assert is_question(query, query_units(query))
    for query in ("阿尔忒弥斯", "正威 王文银", "新能源汽车 价格战"):
        assert not is_question(query, query_units(query))


def test_common_single_characters_stay_separate_weak_words():
    units = query_units("为什么年轻人不愿意生孩子")
    assert [u.term for u in units] == ["年轻人", "不", "愿意", "生", "孩子"]
    assert [u.core for u in units] == [True, False, True, False, True]
    # Question words rank but never decide relevance.
    assert [u.core for u in query_units("贵州的债务问题")] == [True, True, False]


def test_phrase_fallback_drops_fragments():
    assert [u.term for u in split_phrases(query_units("武汉弘芯厂"))] == ["武汉弘芯"]
    assert [u.term for u in split_phrases(query_units("斯凯迪奥"))] == ["迪奥"]
    # Only fragments left: search nothing rather than every 阿 and 尔.
    assert [u.term for u in split_phrases(query_units("阿尔特弥斯"))] == ["特弥斯"]
    assert split_phrases(query_units("斯凯")) == []


@pytest.mark.parametrize(
    "query",
    [
        "",
        " \t\n",
        "x" * 65,
        " ".join(map(str, range(65))),
        "a\x00b",
        " " * 513,
        "的和及其",
        "!!!",
    ],
)
def test_invalid_queries_are_422(query):
    with pytest.raises(HTTPException) as error:
        search.parse_terms(query)
    assert error.value.status_code == 422


def test_cursor_binds_snapshot_terms_and_page_size():
    cursor = search._encode_cursor("s123", ["贵州"], 20, 20)
    decoded = search._decode_cursor(cursor, ["贵州"], 20)
    assert decoded.snapshot_id == "s123"
    assert decoded.offset == 20
    for terms, limit in [(["债务"], 20), (["贵州"], 10)]:
        with pytest.raises(HTTPException):
            search._decode_cursor(cursor, terms, limit)


def test_cursor_binds_order_and_filters():
    options = search.SearchOptions(sort="date", channel="GaoJian", year=2025)
    cursor = search._encode_cursor("s123", ["星舰"], 20, 20, options)
    assert search._decode_cursor(cursor, ["星舰"], 20, options).offset == 20
    for changed in (
        search.SearchOptions(),
        search.SearchOptions(sort="date", channel="GaoJian"),
        search.SearchOptions(sort="date", channel="CanKaoXinXi", year=2025),
    ):
        with pytest.raises(HTTPException):
            search._decode_cursor(cursor, ["星舰"], 20, changed)


def test_suggestion_edit_distance():
    assert search._edit_distance("阿尔特弥斯", "阿尔忒弥斯") == 1
    assert search._edit_distance("许家银", "许家") == 1
    assert search._edit_distance("", "ab") == 2


def _entry(doc_id, date, channel="ShuiQianXiaoXi", score=1.0):
    return search.Entry(doc_id, score, [], "keyword", channel, date)


def test_date_order_and_filters_slice_the_ranked_list():
    entries = [
        _entry("a", "2023-01-01"),
        _entry("b", None),
        _entry("c", "2025-06-01", "GaoJian"),
        _entry("d", "2025-01-01"),
    ]
    by_date = search.select(entries, search.SearchOptions(sort="date"))
    assert [e.doc_id for e in by_date] == ["c", "d", "a", "b"]
    year = search.select(entries, search.SearchOptions(year=2025))
    assert [e.doc_id for e in year] == ["c", "d"]
    channel = search.select(entries, search.SearchOptions(channel="GaoJian"))
    assert [e.doc_id for e in channel] == ["c"]
    facets = search.facets(entries)
    assert [(f.value, f.count) for f in facets.channels] == [
        ("ShuiQianXiaoXi", 3),
        ("GaoJian", 1),
    ]
    assert [(f.value, f.count) for f in facets.years] == [("2025", 2), ("2023", 1)]


def test_reciprocal_rank_fusion_rewards_agreement():
    scores = search_semantic.fuse(["a", "b", "c"], ["c", "d"])
    assert max(scores, key=scores.get) == "c"
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["d"] == pytest.approx(1 / 62)


def test_rerank_is_off_without_a_model(monkeypatch):
    monkeypatch.setattr(search_semantic.settings.rerank, "model", "")
    assert not search_semantic.rerank_enabled()
    assert search_semantic.rerank("q", ["a"]) is None


def test_cursor_from_previous_ranking_version_is_rejected():
    cursor = search._encode_cursor("s123", ["贵州"], 20, 20)
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    payload["version"] = search.CURSOR_VERSION - 1
    old_cursor = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    with pytest.raises(HTTPException) as error:
        search._decode_cursor(old_cursor, ["贵州"], 20)
    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {
            "version": search.CURSOR_VERSION,
            "snapshot_id": "x",
            "terms": ["a"],
            "offset": True,
            "limit": 20,
        },
    ],
)
def test_malformed_cursor_is_422(payload):
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    with pytest.raises(HTTPException) as error:
        search._decode_cursor(encoded, ["a"], 20)
    assert error.value.status_code == 422


def test_snippets_preserve_case_and_cover_distant_topics_with_safe_text_parts():
    body = "贵州 <script> & OpenAI\n\n" + "中" * 250 + "\n\n债务𠀀"
    boundary = body.index("债务")
    sections = [
        {"start": 0, "end": boundary, "heading": None, "heading_anchor": ""},
        {
            "start": boundary,
            "end": len(body),
            "heading": "财政",
            "heading_anchor": "财政",
        },
    ]
    snippets = search.make_snippets(
        body, body.replace("OpenAI", "openai"), sections, ["贵州", "债务", "openai"]
    )
    highlighted = [
        part.text for snippet in snippets for part in snippet.parts if part.highlight
    ]
    assert set(highlighted) >= {"贵州", "债务", "OpenAI"}
    assert any("<script>" in p.text for s in snippets for p in s.parts)
    assert {s.heading_anchor for s in snippets} == {"", "财政"}
    assert len(snippets) <= 3


def test_overlapping_keyword_highlights_are_merged():
    body = "地方债务"
    snippets = search.make_snippets(
        body,
        body,
        [{"start": 0, "end": len(body), "heading": None, "heading_anchor": ""}],
        ["地方", "债务"],
    )
    assert [(p.text, p.highlight) for p in snippets[0].parts] == [(body, True)]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(snapshot_manager, "start", lambda: None)
    snapshot_manager.set_for_tests(
        Snapshot("s1", "rag_s1", 5, "test-embedding-model@2560")
    )
    with TestClient(main.app) as client:
        yield client
    snapshot_manager.set_for_tests(None, "reset")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": " "},
        {"q": "x", "limit": 0},
        {"q": "x", "limit": 51},
        {"q": "x" * 65},
        {"q": "x", "cursor": "!"},
        {"q": "x", "sort": "random"},
        {"q": "x", "channel": "../etc"},
        {"q": "x", "year": 99},
    ],
)
def test_endpoint_input_validation(client, params):
    assert client.get("/search", params=params).status_code == 422


def test_database_error_does_not_leak_details(client, monkeypatch):
    def broken(*args):
        raise RuntimeError("secret connection details")

    monkeypatch.setattr(main, "search", broken)
    response = client.get("/search", params={"q": "债务"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Search service unavailable"}
