import json
import re
import xml.etree.ElementTree as ET

import httpx
import pytest
import server
from fastapi.testclient import TestClient


class _UpstreamStream:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.response

    async def __aexit__(self, _exc_type, _exc, _traceback):
        if self.response:
            await self.response.aclose()


class _FakeUpstreamClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def stream(self, _method, _url, **_kwargs):
        return _UpstreamStream(self.response, self.error)


@pytest.fixture
def client():
    test_client = TestClient(server.app)
    server._SHORT_ID_CACHE = None
    server._SHORT_ID_BUILT_AT = 0.0
    try:
        yield test_client
    finally:
        test_client.close()
        server._client = None
        server._SHORT_ID_CACHE = None
        server._SHORT_ID_BUILT_AT = 0.0


def _upstream_response(status_code, content=b""):
    request = httpx.Request("POST", server.CHAT_ENDPOINT)
    return httpx.Response(status_code, content=content, request=request)


def _error_event(response):
    data_lines = [
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert data_lines[-1] == "[DONE]"
    return json.loads(data_lines[0])


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (422, "问题格式有误，请检查后重试。"),
        (500, "档案服务暂时不可用，请稍后重试。"),
        (503, "档案服务暂时不可用，请稍后重试。"),
    ],
)
def test_chat_maps_upstream_status_errors_to_sse(client, status_code, message):
    server._client = _FakeUpstreamClient(_upstream_response(status_code))

    response = client.post("/chat", json={"question": "test"})

    assert response.status_code == 200
    assert _error_event(response) == {"type": "error", "content": message}


def test_chat_maps_upstream_timeout_to_sse(client):
    server._client = _FakeUpstreamClient(error=httpx.ReadTimeout("upstream timed out"))

    response = client.post("/chat", json={"question": "test"})

    assert response.status_code == 200
    assert _error_event(response) == {
        "type": "error",
        "content": "信号超时，请稍后重试。",
    }


def test_static_assets_set_cache_headers_and_support_revalidation(client):
    app_js = client.get("/app.js")

    assert app_js.status_code == 200
    assert app_js.headers["cache-control"] == "no-cache"
    assert "etag" in app_js.headers

    revalidated = client.get(
        "/app.js", headers={"If-None-Match": app_js.headers["etag"]}
    )
    assert revalidated.status_code == 304
    assert revalidated.headers["cache-control"] == "no-cache"

    vendored_asset = client.get("/markdown-it.min.js")
    assert vendored_asset.status_code == 200
    assert vendored_asset.headers["cache-control"] == "public, max-age=604800"


def test_gzip_compresses_static_text_but_not_event_stream(client):
    static_response = client.get("/app.js", headers={"Accept-Encoding": "gzip"})
    assert static_response.headers["content-encoding"] == "gzip"

    event = b'data: {"type": "answer_chunk", "content": "' + b"x" * 2048 + b'"}\n\n'
    server._client = _FakeUpstreamClient(_upstream_response(200, event))
    stream_response = client.post(
        "/chat",
        json={"question": "test"},
        headers={"Accept-Encoding": "gzip"},
    )

    assert stream_response.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in stream_response.headers
    assert stream_response.content == event


class _FakeJsonClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def _json_upstream(status_code=200, payload=None, headers=None):
    request = httpx.Request("GET", server.TRANSCRIPTS_ENDPOINT)
    return httpx.Response(
        status_code,
        json=payload or {},
        headers=headers,
        request=request,
    )


def test_transcript_index_proxies_etag_and_revalidation(client):
    upstream = _FakeJsonClient(
        _json_upstream(200, {"items": [{"doc_id": "a.md"}]}, {"ETag": '"abc"'})
    )
    server._client = upstream

    response = client.get("/api/transcripts", headers={"If-None-Match": '"old"'})

    assert response.status_code == 200
    assert response.json()["items"][0]["doc_id"] == "a.md"
    assert response.headers["etag"] == '"abc"'
    assert upstream.calls == [
        (server.TRANSCRIPTS_ENDPOINT, {"headers": {"If-None-Match": '"old"'}})
    ]


def test_transcript_detail_percent_encodes_upstream_uri(client):
    upstream = _FakeJsonClient(_json_upstream(404, {"detail": "not found"}))
    server._client = upstream

    response = client.get("/api/transcripts/栏目/一期.md")

    assert response.status_code == 404
    assert upstream.calls[0][0].endswith("/%E6%A0%8F%E7%9B%AE/%E4%B8%80%E6%9C%9F.md")


def _article_payload(**overrides):
    payload = {
        "doc_id": "ShuiQianXiaoXi/0501-0600/0588.md",
        "canonical_title": "睡前消息588",
        "source_title": "【睡前消息588】测试文稿",
        "channel": "ShuiQianXiaoXi",
        "publication_date": "2025-02-03",
        "updated_at": "2026-09-21T04:50:37+00:00",
        "body_html": "<p>这是已经清洗的<strong>正文内容</strong>。</p>",
    }
    payload.update(overrides)
    return payload


def _index_item(doc_id, title, date, channel="ShuiQianXiaoXi"):
    return {
        "doc_id": doc_id,
        "canonical_title": title.split("】", 1)[0].removeprefix("【"),
        "source_title": title,
        "channel": channel,
        "publication_date": date,
        "source_hash": "hash",
        "updated_at": f"{date}T12:00:00+00:00",
    }


def test_root_is_server_rendered_with_crawlable_channel_links(client):
    response = client.get("/")

    assert response.status_code == 200
    assert '<link rel="canonical" href="https://bedtime.blog/"' in response.text
    assert '<meta property="og:type" content="website"' in response.text
    assert response.text.count('class="channel-chip"') == 5
    assert 'href="/transcripts?channel=ShuiQianXiaoXi"' in response.text
    assert '<script type="module" src="/app.js"></script>' in response.text
    assert "__PAGE_" not in response.text
    assert "etag" in response.headers
    assert '<meta name="twitter:card" content="summary" />' in response.text
    assert '<meta property="og:image:width" content="128" />' in response.text
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', response.text, re.S
    )
    assert match is not None
    assert json.loads(match.group(1))["@type"] == "WebSite"


def test_index_html_redirects_to_canonical_root(client):
    response = client.get("/index.html", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == "/"


def test_channel_archive_is_server_rendered_and_sorted(client):
    items = [
        _index_item(
            "ShuiQianXiaoXi/0001-0100/0001.md",
            "【睡前消息1】旧文稿",
            "2025-01-01",
        ),
        _index_item(
            "ShuiQianXiaoXi/0001-0100/0002.md",
            "【睡前消息2】新文稿",
            "2026-01-01",
        ),
        _index_item(
            "GaoJian/0001-0100/0001.md", "【高见1】其它栏目", "2026-02-01", "GaoJian"
        ),
    ]
    server._client = _FakeJsonClient(_json_upstream(200, {"items": items}))

    response = client.get("/transcripts?channel=ShuiQianXiaoXi")

    assert response.status_code == 200
    assert "睡前消息文稿 · 睡前消息知识库" in response.text
    assert (
        '<link rel="canonical" href="https://bedtime.blog/transcripts?channel=ShuiQianXiaoXi"'
        in response.text
    )
    assert response.text.index("【睡前消息2】新文稿") < response.text.index(
        "【睡前消息1】旧文稿"
    )
    assert "【高见1】其它栏目" not in response.text
    assert 'href="/transcripts/ShuiQianXiaoXi/0001-0100/0002.md"' in response.text
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', response.text, re.S
    )
    assert match is not None
    breadcrumb = json.loads(match.group(1))
    assert breadcrumb["@type"] == "BreadcrumbList"
    assert [item["name"] for item in breadcrumb["itemListElement"]] == [
        "首页",
        "睡前消息",
    ]


def test_transcript_page_contains_raw_ssr_body_and_safe_metadata(client):
    hostile_title = '题目</title><script>alert("x")</script>'
    article = _article_payload(source_title=hostile_title)
    server._client = _FakeJsonClient(_json_upstream(200, article))

    response = client.get("/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")

    assert response.status_code == 200
    assert article["body_html"] in response.text
    assert hostile_title not in response.text
    assert "题目&lt;/title&gt;&lt;script&gt;" in response.text
    assert '<meta property="og:type" content="article"' in response.text
    assert (
        '<link rel="canonical" href="https://bedtime.blog/transcripts/ShuiQianXiaoXi/0501-0600/0588.md"'
        in response.text
    )
    matches = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', response.text, re.S
    )
    assert len(matches) == 2
    for raw in matches:
        assert "</script>" not in raw
    structured_by_type = {json.loads(raw)["@type"]: json.loads(raw) for raw in matches}
    article = structured_by_type["Article"]
    assert article["headline"] == hostile_title
    assert article["datePublished"] == "2025-02-03"
    breadcrumb = structured_by_type["BreadcrumbList"]
    assert [item["name"] for item in breadcrumb["itemListElement"]] == [
        "首页",
        "睡前消息",
        hostile_title,
    ]
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-src 'none'" in response.headers["content-security-policy"]


def test_transcript_html_supports_etag_revalidation(client):
    server._client = _FakeJsonClient(_json_upstream(200, _article_payload()))
    first = client.get("/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")

    second = client.get(
        "/transcripts/ShuiQianXiaoXi/0501-0600/0588.md",
        headers={"If-None-Match": first.headers["etag"]},
    )

    assert second.status_code == 304
    assert second.headers["etag"] == first.headers["etag"]


@pytest.mark.parametrize(
    "path",
    [
        "/transcripts/not-markdown.txt",
        "/transcripts/a/%2E%2E/b.md",
        "/transcripts/a/%5Cb.md",
        "/transcripts/a//b.md",
    ],
)
def test_malformed_transcript_routes_are_real_404s(client, path):
    response = client.get(path)

    assert response.status_code == 404
    assert '<meta name="robots" content="noindex"' in response.text


def test_missing_transcript_is_a_real_404(client):
    server._client = _FakeJsonClient(_json_upstream(404, {"detail": "not found"}))

    response = client.get("/transcripts/ShuiQianXiaoXi/0001-0100/0001.md")

    assert response.status_code == 404
    assert '<meta name="robots" content="noindex"' in response.text
    assert "这篇文稿不存在" in response.text


def test_invalid_or_missing_channel_does_not_create_soft_200(client):
    redirect = client.get("/transcripts", follow_redirects=False)
    unknown = client.get("/transcripts?channel=Unknown")
    duplicate = client.get("/transcripts?channel=ShuiQianXiaoXi&channel=GaoJian")

    assert redirect.status_code == 308
    assert redirect.headers["location"] == "/"
    assert unknown.status_code == duplicate.status_code == 404
    assert "noindex" in unknown.text


def test_upstream_timeout_returns_controlled_503_page(client):
    server._client = _FakeJsonClient(error=httpx.ReadTimeout("timed out"))

    response = client.get("/transcripts/ShuiQianXiaoXi/0001-0100/0001.md")

    assert response.status_code == 503
    assert '<meta name="robots" content="noindex"' in response.text
    assert "文稿服务暂时不可用" in response.text


def test_robots_txt_advertises_sitemap_and_revalidates(client):
    first = client.get("/robots.txt")

    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/plain")
    assert first.text == (
        "User-agent: *\nAllow: /\nSitemap: https://bedtime.blog/sitemap.xml\n"
    )
    second = client.get("/robots.txt", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304


def test_sitemap_contains_root_channels_articles_and_lastmods(client):
    items = [
        _index_item(
            "ShuiQianXiaoXi/0001-0100/0001.md",
            "【睡前消息1】测试",
            "2025-01-01",
        ),
        _index_item(
            "产经/一期.md",
            "【产经破壁机2026-06-02】测试",
            "2026-06-02",
            "ChanJingPoBiJi",
        ),
    ]
    fake = _FakeJsonClient(_json_upstream(200, {"items": items}))
    server._client = fake

    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(response.content)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = [element.text for element in root.findall("sm:url/sm:loc", namespace)]
    assert locations == [
        "https://bedtime.blog/",
        "https://bedtime.blog/transcripts?channel=ShuiQianXiaoXi",
        "https://bedtime.blog/transcripts?channel=ChanJingPoBiJi",
        "https://bedtime.blog/transcripts/ShuiQianXiaoXi/0001-0100/0001.md",
        "https://bedtime.blog/transcripts/%E4%BA%A7%E7%BB%8F/%E4%B8%80%E6%9C%9F.md",
    ]
    assert len(root.findall("sm:url/sm:lastmod", namespace)) == len(locations)
    assert len(fake.calls) == 1


def test_sitemap_does_not_return_partial_success_on_bad_upstream(client):
    server._client = _FakeJsonClient(error=httpx.ConnectError("offline"))

    response = client.get("/sitemap.xml")

    assert response.status_code == 503
    assert "temporarily unavailable" in response.text


def test_short_link_redirects_to_the_matching_transcript(client):
    doc_id = "ShuiQianXiaoXi/0001-0100/0001.md"
    items = [_index_item(doc_id, "【睡前消息1】测试", "2025-01-01")]
    server._client = _FakeJsonClient(_json_upstream(200, {"items": items}))

    response = client.get(f"/s/{server._short_id_for(doc_id)}", follow_redirects=False)

    assert response.status_code == 302
    assert (
        response.headers["location"] == "/transcripts/ShuiQianXiaoXi/0001-0100/0001.md"
    )


def test_short_link_is_deterministic_and_uses_the_restricted_alphabet(client):
    short_id = server._short_id_for("ShuiQianXiaoXi/0001-0100/0001.md")

    assert short_id == server._short_id_for("ShuiQianXiaoXi/0001-0100/0001.md")
    assert len(short_id) == server.SHORT_ID_LENGTH
    assert set(short_id) <= server._BASE58_SET


def test_short_link_rejects_malformed_ids_without_querying_upstream(client):
    fake = _FakeJsonClient(_json_upstream(200, {"items": []}))
    server._client = fake

    too_short = client.get("/s/abc", follow_redirects=False)
    bad_char = client.get("/s/0OIl1234", follow_redirects=False)

    assert too_short.status_code == 404
    assert bad_char.status_code == 404
    assert fake.calls == []


def test_short_link_rebuilds_on_a_cache_miss_once_the_interval_passed(
    client, monkeypatch
):
    fake = _FakeJsonClient(_json_upstream(200, {"items": []}))
    server._client = fake
    now = [1000.0]
    monkeypatch.setattr(server, "_now", lambda: now[0])

    client.get("/s/11111111", follow_redirects=False)
    now[0] += server.SHORT_ID_MIN_REFRESH_SECONDS
    response = client.get("/s/11111111", follow_redirects=False)

    assert response.status_code == 404
    assert len(fake.calls) == 2


def test_short_link_misses_do_not_refetch_within_the_interval(client, monkeypatch):
    fake = _FakeJsonClient(_json_upstream(200, {"items": []}))
    server._client = fake
    monkeypatch.setattr(server, "_now", lambda: 1000.0)

    for suffix in "12345":
        response = client.get(f"/s/1111111{suffix}", follow_redirects=False)
        assert response.status_code == 404

    assert len(fake.calls) == 1


def test_short_link_returns_503_when_upstream_is_down(client):
    server._client = _FakeJsonClient(error=httpx.ConnectError("offline"))

    response = client.get(
        f"/s/{server._short_id_for('ShuiQianXiaoXi/0001-0100/0001.md')}",
        follow_redirects=False,
    )

    assert response.status_code == 503
