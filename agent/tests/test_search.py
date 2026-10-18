"""Keyword syntax, cursor validation, snippets, and API failures."""

import base64
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from src import main, search
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
    assert search.parse_terms("Cafe\u0301") == ["caf", "é"]
    assert search.keyword_query("债").startswith("w")
    assert all(c.isalnum() or c in " &" for c in search.keyword_query("'|x"))


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
        Snapshot("s1", "rag_s1", 2, "test-embedding-model@2560")
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
