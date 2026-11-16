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
    "\n"
    "引用出处[\\[1\\]](#ref-2)。\n"
    "\n"
    '2. [&#8202;](#ref-2)**[^](#cite_ref-2 "跳转")**引文条目。\n'
    "\n"
    "| 参数 | 数值 |\n"
    "| --- | --- |\n"
    "| 起源地 | 四川 |\n"
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
    # GFM tables render as tables, not flattened text.
    assert "<table>" in response.text
    assert "<th>数值</th>" in response.text
    # Footnote marker links become jump targets; citations link to them.
    assert '<span id="ref-2"></span>' in response.text
    assert '<a href="#ref-2">' in response.text
    # Relative capture links must point at the original site, not bedtime.blog.
    assert 'href="https://example.com/section/x.html"' in response.text
    # External links open in a new tab like the reader does.
    assert (
        'target="_blank" rel="noopener noreferrer" href="https://else.example.org/y"'
        in response.text
    )


def test_snapshot_etag_roundtrip(tmp_path, monkeypatch):
    (tmp_path / "ab").mkdir()
    (tmp_path / "ab" / "cd0123456789.md").write_text(SNAPSHOT_MD, encoding="utf-8")
    monkeypatch.setattr(server, "SNAPSHOTS_DIR", tmp_path)
    client = TestClient(server.app)

    first = client.get("/snapshots/ab/cd0123456789.md")
    etag = first.headers["etag"]
    cached = client.get(
        "/snapshots/ab/cd0123456789.md", headers={"if-none-match": etag}
    )
    assert cached.status_code == 304
    assert cached.headers["x-robots-tag"] == "noindex, nofollow"


def test_snapshot_missing_and_invalid_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SNAPSHOTS_DIR", tmp_path)
    client = TestClient(server.app)

    assert client.get("/snapshots/ab/ffffffffffff.md").status_code == 404
    # Snapshot names must retain their shard and 12-hex identifier.
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


def test_original_pdf_bytes_range_and_cache(tmp_path, monkeypatch):
    (tmp_path / "ab").mkdir()
    original = b"%PDF-1.7\r\n" + bytes(range(256)) * 8
    (tmp_path / "ab" / "cd0123456789.pdf").write_bytes(original)
    monkeypatch.setattr(server, "SNAPSHOTS_DIR", tmp_path)
    client = TestClient(server.app)
    url = "/snapshots/ab/cd0123456789.pdf"
    response = client.get(url, headers={"accept-encoding": "identity"})
    assert response.content == original
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["x-content-type-options"] == "nosniff"
    partial = client.get(
        url, headers={"range": "bytes=9-31", "accept-encoding": "identity"}
    )
    assert partial.status_code == 206
    assert partial.content == original[9:32]
    assert partial.headers["content-range"] == f"bytes 9-31/{len(original)}"
    cached = client.get(url, headers={"if-none-match": response.headers["etag"]})
    assert cached.status_code == 304


def test_original_documents_download_without_html_rendering(tmp_path, monkeypatch):
    (tmp_path / "ab").mkdir()
    original = b"PK\x03\x04\x00\xff\r\n"
    (tmp_path / "ab" / "cd0123456789.docx").write_bytes(original)
    monkeypatch.setattr(server, "SNAPSHOTS_DIR", tmp_path)
    response = TestClient(server.app).get("/snapshots/ab/cd0123456789.docx")
    assert response.status_code == 200
    assert response.content == original
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
