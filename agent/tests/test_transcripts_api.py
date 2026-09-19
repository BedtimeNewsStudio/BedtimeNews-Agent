"""Transcript reader API contracts."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from src import main


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def _metadata():
    return {
        "doc_id": "ShuiQianXiaoXi/0501-0600/0588.md",
        "canonical_title": "睡前消息588",
        "source_title": "【睡前消息588】城投债四大网红",
        "channel": "ShuiQianXiaoXi",
        "publication_date": "2023-05-12",
        "source_hash": "a" * 64,
        "updated_at": datetime(2026, 9, 18, tzinfo=UTC),
    }


def test_transcript_index_is_ordered_json_with_revalidation(client, monkeypatch):
    monkeypatch.setattr(main, "list_transcripts", lambda: [_metadata()])

    response = client.get("/transcripts")
    assert response.status_code == 200
    assert response.json()["items"][0]["canonical_title"] == "睡前消息588"
    assert response.headers["cache-control"] == "no-cache"
    etag = response.headers["etag"]

    revalidated = client.get("/transcripts", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.headers["etag"] == etag


def test_transcript_detail_returns_sanitized_projection(client, monkeypatch):
    article = _metadata() | {"body_html": "<h2>正文</h2><p>内容</p>"}
    monkeypatch.setattr(main, "get_transcript", lambda doc_id: article)

    response = client.get("/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")

    assert response.status_code == 200
    assert response.json()["body_html"] == article["body_html"]
    assert response.headers["etag"] == main._etag_for(article)


def test_detail_etag_changes_when_only_canonical_title_changes(client, monkeypatch):
    article = _metadata() | {"body_html": "<p>内容</p>"}
    monkeypatch.setattr(main, "get_transcript", lambda _doc_id: article)
    first = client.get("/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")

    article["canonical_title"] = "睡前消息588（更正）"
    second = client.get("/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")

    assert first.headers["etag"] != second.headers["etag"]


def test_unknown_transcript_is_404(client, monkeypatch):
    monkeypatch.setattr(main, "get_transcript", lambda _doc_id: None)
    assert client.get("/transcripts/unknown/missing.md").status_code == 404


@pytest.mark.parametrize(
    "uri",
    ["", "/absolute.md", "../secret.md", "folder\\secret.md", "file.txt", "a/./b.md"],
)
def test_uri_validation_rejects_noncanonical_paths(uri):
    with pytest.raises(HTTPException) as exc:
        main._validate_transcript_uri(uri)
    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/transcripts", "list_transcripts"),
        ("/transcripts/channel/document.md", "get_transcript"),
    ],
)
def test_database_failure_is_controlled_503(client, monkeypatch, path, name):
    def unavailable(*_args):
        raise RuntimeError("database offline")

    monkeypatch.setattr(main, name, unavailable)
    response = client.get(path)

    assert response.status_code == 503
    assert response.json() == {"detail": "Transcript service unavailable"}
