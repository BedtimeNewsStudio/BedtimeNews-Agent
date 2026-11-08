"""Tests for the read-only /snapshots appendix-snapshot route."""

import server
from fastapi.testclient import TestClient

SNAPSHOT_MD = (
    "---\n"
    'source_url: "https://example.com/a/page.html"\n'
    "captured_at: 2026-11-08T05:34:40Z\n"
    'title: "示例报道"\n'
    'tool: "firecrawl"\n'
    "---\n"
    "\n"
    "> **网页快照**：2026-11-08 05:34 UTC 抓取自 [原始页面](https://example.com/a/page.html)（工具：firecrawl）。\n"
    "\n"
    "# 示例报道\n"
    "\n"
    "正文段落，含相对链接 [内部](/section/x.html) 与外链 [外部](https://else.example.org/y)。\n"
)


def test_snapshot_page_renders(tmp_path, monkeypatch):
    (tmp_path / "ab").mkdir()
    (tmp_path / "ab" / "cd0123456789.md").write_text(SNAPSHOT_MD, encoding="utf-8")
    monkeypatch.setattr(server, "SNAPSHOTS_DIR", tmp_path)
    client = TestClient(server.app)

    response = client.get("/snapshots/ab/cd0123456789.md")
    assert response.status_code == 200
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert 'name="robots" content="noindex, nofollow"' in response.text
    assert "<title>示例报道 · 网页快照 · " in response.text
    assert "网页快照" in response.text
    assert "正文段落" in response.text
    # Relative capture links must point at the original site, not bedtime.blog.
    assert 'href="https://example.com/section/x.html"' in response.text
    # External links open in a new tab like the reader does.
    assert 'target="_blank" rel="noopener noreferrer" href="https://else.example.org/y"' in response.text


def test_snapshot_etag_roundtrip(tmp_path, monkeypatch):
    (tmp_path / "ab").mkdir()
    (tmp_path / "ab" / "cd0123456789.md").write_text(SNAPSHOT_MD, encoding="utf-8")
    monkeypatch.setattr(server, "SNAPSHOTS_DIR", tmp_path)
    client = TestClient(server.app)

    first = client.get("/snapshots/ab/cd0123456789.md")
    etag = first.headers["etag"]
    cached = client.get("/snapshots/ab/cd0123456789.md", headers={"if-none-match": etag})
    assert cached.status_code == 304
    assert cached.headers["x-robots-tag"] == "noindex, nofollow"


def test_snapshot_missing_and_invalid_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SNAPSHOTS_DIR", tmp_path)
    client = TestClient(server.app)

    assert client.get("/snapshots/ab/ffffffffffff.md").status_code == 404
    # Anything that is not shard/12-hex.md is rejected before touching disk.
    assert client.get("/snapshots/ab/escape.md").status_code == 404
    assert client.get("/snapshots/zz/../../etc/passwd").status_code == 404


def test_snapshot_route_disabled_without_dir(monkeypatch):
    monkeypatch.setattr(server, "SNAPSHOTS_DIR", None)
    client = TestClient(server.app)
    assert client.get("/snapshots/ab/cd0123456789.md").status_code == 404


def test_robots_txt_disallows_snapshots():
    client = TestClient(server.app)
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /snapshots/" in response.text
