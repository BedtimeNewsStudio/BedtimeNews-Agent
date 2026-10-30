import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

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
def client(monkeypatch):
    # monkeypatch restores all four module globals after every test, so no
    # test can leak a fake upstream or a warm cache into the next.
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_SHORT_ID_CACHE", None)
    monkeypatch.setattr(server, "_SHORT_ID_BUILT_AT", 0.0)
    monkeypatch.setattr(server, "_latest_desk_html_cache", {})
    test_client = TestClient(server.app)
    try:
        yield test_client
    finally:
        test_client.close()


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
def test_chat_maps_upstream_status_errors_to_sse(
    client, status_code, message, monkeypatch
):
    monkeypatch.setattr(
        server, "_client", _FakeUpstreamClient(_upstream_response(status_code))
    )

    response = client.post("/chat", json={"question": "test"})

    assert response.status_code == 200
    assert _error_event(response) == {"type": "error", "content": message}


def test_chat_maps_upstream_timeout_to_sse(client, monkeypatch):
    monkeypatch.setattr(
        server,
        "_client",
        _FakeUpstreamClient(error=httpx.ReadTimeout("upstream timed out")),
    )

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


def test_gzip_compresses_static_text_but_not_event_stream(client, monkeypatch):
    static_response = client.get("/app.js", headers={"Accept-Encoding": "gzip"})
    assert static_response.headers["content-encoding"] == "gzip"

    event = b'data: {"type": "answer_chunk", "content": "' + b"x" * 2048 + b'"}\n\n'
    monkeypatch.setattr(
        server, "_client", _FakeUpstreamClient(_upstream_response(200, event))
    )
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


def test_search_proxy_passes_query_params_and_body(client, monkeypatch):
    payload = {
        "query": "贵州 债务",
        "terms": ["贵州", "债务"],
        "snapshot_id": "s1",
        "total": 0,
        "results": [],
        "next_cursor": None,
    }
    upstream = _FakeJsonClient(_json_upstream(200, payload))
    monkeypatch.setattr(server, "_client", upstream)

    response = client.get("/api/search", params={"q": "贵州 债务", "limit": "10"})

    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert upstream.calls[0][0] == server.SEARCH_ENDPOINT
    assert upstream.calls[0][1]["params"] == {
        "q": "贵州 债务",
        "limit": "10",
        "sort": "relevance",
    }


def test_search_proxy_forwards_cursor(client, monkeypatch):
    upstream = _FakeJsonClient(_json_upstream(200, {"next_cursor": None}))
    monkeypatch.setattr(server, "_client", upstream)

    response = client.get("/api/search", params={"q": "贵州", "cursor": "abc_123"})

    assert response.status_code == 200
    assert upstream.calls[0][1]["params"] == {
        "q": "贵州",
        "limit": "20",
        "sort": "relevance",
        "cursor": "abc_123",
    }


def test_search_proxy_forwards_order_and_filters(client, monkeypatch):
    upstream = _FakeJsonClient(_json_upstream(200, {"results": [], "total": 0}))
    monkeypatch.setattr(server, "_client", upstream)
    params = {"q": "星舰", "sort": "date", "channel": "GaoJian", "year": "2025"}
    assert client.get("/api/search", params=params).status_code == 200
    assert upstream.calls[0][1]["params"] == {**params, "limit": "20"}
    for bad in ({"sort": "random"}, {"channel": "../x"}, {"year": "25"}):
        assert client.get("/api/search", params={"q": "星舰", **bad}).status_code == 422


class _Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_search_click_is_logged_anonymously(client):
    click = {
        "q": "阿尔忒弥斯",
        "doc_id": "GaoJian/0063.md",
        "rank": 1,
        "match": "keyword",
        "mode": "keyword",
        "snapshot_id": "s1",
    }
    records = _Records()
    logging.getLogger("search.clicks").addHandler(records)
    try:
        assert client.post("/api/search/click", json=click).status_code == 204
    finally:
        logging.getLogger("search.clicks").removeHandler(records)
    logged = records.messages[-1]
    assert logged.startswith("search_click ")
    assert json.loads(logged.removeprefix("search_click ")) == click
    # Unknown fields (an IP, a user id) are refused, not logged.
    assert (
        client.post("/api/search/click", json={**click, "ip": "1.2.3.4"}).status_code
        == 422
    )
    assert client.post("/api/search/click", content="x" * 4096).status_code == 413


def test_search_proxy_passes_agent_error_status_through(client, monkeypatch):
    upstream = _FakeJsonClient(
        _json_upstream(422, {"detail": "Provide 1-64 searchable words"})
    )
    monkeypatch.setattr(server, "_client", upstream)

    response = client.get("/api/search", params={"q": "。。。"})

    assert response.status_code == 422
    assert "searchable" in response.json()["detail"]


def test_search_proxy_maps_transport_error_to_controlled_503(client, monkeypatch):
    upstream = _FakeJsonClient(error=httpx.ConnectError("boom"))
    monkeypatch.setattr(server, "_client", upstream)

    response = client.get("/api/search", params={"q": "贵州"})

    assert response.status_code == 503
    assert "搜索服务暂时不可用" in response.json()["detail"]


def test_transcript_index_proxies_etag_and_revalidation(client, monkeypatch):
    upstream = _FakeJsonClient(
        _json_upstream(200, {"items": [{"doc_id": "a.md"}]}, {"ETag": '"abc"'})
    )
    monkeypatch.setattr(server, "_client", upstream)

    response = client.get("/api/transcripts", headers={"If-None-Match": '"old"'})

    assert response.status_code == 200
    assert response.json()["items"][0]["doc_id"] == "a.md"
    assert response.headers["etag"] == '"abc"'
    assert upstream.calls == [
        (
            server.TRANSCRIPTS_ENDPOINT,
            {"headers": {"If-None-Match": '"old"'}, "params": None},
        )
    ]


def test_transcript_detail_percent_encodes_upstream_uri(client, monkeypatch):
    upstream = _FakeJsonClient(_json_upstream(404, {"detail": "not found"}))
    monkeypatch.setattr(server, "_client", upstream)

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
    assert 'id="masthead-latest" href="/latest" data-route' in response.text
    assert response.text.index('id="channel-bar"') < response.text.index(
        'id="masthead-latest"'
    )
    assert 'href="/transcripts?channel=ShuiQianXiaoXi"' in response.text
    assert '<script type="module" src="/app.js"></script>' in response.text
    assert "__PAGE_" not in response.text
    assert "etag" in response.headers
    assert '<meta name="twitter:card" content="summary" />' in response.text
    assert '<meta property="og:image:width" content="128" />' in response.text
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', response.text, re.DOTALL
    )
    assert match is not None
    assert json.loads(match.group(1))["@type"] == "WebSite"


def test_index_html_redirects_to_canonical_root(client):
    response = client.get("/index.html", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == "/"


def test_channel_archive_is_server_rendered_and_sorted(client, monkeypatch):
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
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(_json_upstream(200, {"items": items}))
    )

    response = client.get("/transcripts?channel=ShuiQianXiaoXi")

    assert response.status_code == 200
    assert "睡前消息文稿 · 睡前消息知识库" in response.text
    assert (
        '<link rel="canonical" href="https://bedtime.blog/transcripts?channel=ShuiQianXiaoXi"'
        in response.text
    )
    # Order/filtering assertions are scoped to the archive section: the page
    # now also server-renders the landing desk, which draws from the same
    # faked upstream and lists other channels.
    archive_html = response.text[
        response.text.index('id="archive-view"') : response.text.index(
            'id="reader-view"'
        )
    ]
    assert archive_html.index("【睡前消息2】新文稿") < archive_html.index(
        "【睡前消息1】旧文稿"
    )
    assert "【高见1】其它栏目" not in archive_html
    assert 'href="/transcripts/ShuiQianXiaoXi/0001-0100/0002.md"' in archive_html
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', response.text, re.DOTALL
    )
    assert match is not None
    breadcrumb = json.loads(match.group(1))
    assert breadcrumb["@type"] == "BreadcrumbList"
    assert [item["name"] for item in breadcrumb["itemListElement"]] == [
        "首页",
        "睡前消息",
    ]


def test_transcript_page_contains_raw_ssr_body_and_safe_metadata(client, monkeypatch):
    hostile_title = '题目</title><script>alert("x")</script>'
    article = _article_payload(
        source_title=hostile_title,
        body_html='<h2 id="话题">话题</h2><p>这是已经清洗的<strong>正文内容</strong>。</p>',
    )
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(_json_upstream(200, article))
    )

    response = client.get("/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")

    assert response.status_code == 200
    assert article["body_html"] in response.text
    assert '<h2 id="话题">' in response.text
    assert hostile_title not in response.text
    assert "题目&lt;/title&gt;&lt;script&gt;" in response.text
    assert '<meta property="og:type" content="article"' in response.text
    assert (
        '<link rel="canonical" href="https://bedtime.blog/transcripts/ShuiQianXiaoXi/0501-0600/0588.md"'
        in response.text
    )
    matches = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', response.text, re.DOTALL
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


def test_transcript_html_supports_etag_revalidation(client, monkeypatch):
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(_json_upstream(200, _article_payload()))
    )
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


def test_missing_transcript_is_a_real_404(client, monkeypatch):
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(_json_upstream(404, {"detail": "not found"}))
    )

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


def test_upstream_timeout_returns_controlled_503_page(client, monkeypatch):
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(error=httpx.ReadTimeout("timed out"))
    )

    response = client.get("/transcripts/ShuiQianXiaoXi/0001-0100/0001.md")

    assert response.status_code == 503
    assert '<meta name="robots" content="noindex"' in response.text
    assert "文稿服务暂时不可用" in response.text


class _FlakyJsonClient(_FakeJsonClient):
    """Raises `error` for the first `failures` GETs, then returns `response`."""

    def __init__(self, response, error, failures):
        super().__init__(response=response)
        self.flaky_error = error
        self.failures = failures

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) <= self.failures:
            raise self.flaky_error
        return self.response


def _stale_connection():
    return httpx.RemoteProtocolError("Server disconnected without sending a response.")


def test_transcript_page_retries_once_on_stale_upstream_connection(client, monkeypatch):
    # Cache the landing desk out of the way so the counted calls are the
    # article fetch's alone.
    monkeypatch.setattr(
        server,
        "_latest_desk_html_cache",
        {"at": time.monotonic(), "html": "<!-- cached desk -->"},
    )
    upstream = _FlakyJsonClient(
        _json_upstream(200, _article_payload()), _stale_connection(), failures=1
    )
    monkeypatch.setattr(server, "_client", upstream)

    response = client.get("/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")

    assert response.status_code == 200
    assert len(upstream.calls) == 2


def test_persistent_upstream_disconnect_is_a_controlled_503_not_500(
    client, monkeypatch
):
    monkeypatch.setattr(
        server,
        "_client",
        _FlakyJsonClient(
            _json_upstream(200, _article_payload()), _stale_connection(), failures=99
        ),
    )

    page = client.get("/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")
    api = client.get("/api/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")

    assert page.status_code == 503
    assert "文稿服务暂时不可用" in page.text
    assert api.status_code == 503
    assert api.json() == {"detail": "文稿服务暂时不可用"}


def test_api_transcript_proxy_retries_once_on_stale_upstream_connection(
    client, monkeypatch
):
    upstream = _FlakyJsonClient(
        _json_upstream(200, {"items": []}), _stale_connection(), failures=1
    )
    monkeypatch.setattr(server, "_client", upstream)

    response = client.get("/api/transcripts")

    assert response.status_code == 200
    assert len(upstream.calls) == 2


def test_upstream_client_expires_idle_connections_before_the_agent_does():
    # uvicorn's default timeout_keep_alive is 5s; ours must be shorter.
    assert server.UPSTREAM_KEEPALIVE_EXPIRY < 5


def test_robots_txt_advertises_sitemap_and_revalidates(client):
    first = client.get("/robots.txt")

    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/plain")
    assert first.text == (
        "User-agent: *\nAllow: /\nSitemap: https://bedtime.blog/sitemap.xml\n"
    )
    second = client.get("/robots.txt", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304


def _assert_head_matches_get(client, path):
    get = client.get(path, headers={"Accept-Encoding": "identity"})
    head = client.head(path, headers={"Accept-Encoding": "identity"})

    assert head.status_code == get.status_code
    assert head.content == b""
    for name in ("content-type", "content-length", "etag", "cache-control"):
        assert head.headers.get(name) == get.headers.get(name), name


@pytest.mark.parametrize("path", ["/", "/robots.txt", "/app.js"])
def test_head_matches_get_for_local_pages(client, path):
    _assert_head_matches_get(client, path)


def test_head_matches_get_for_ssr_transcript(client, monkeypatch):
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(_json_upstream(200, _article_payload()))
    )

    _assert_head_matches_get(client, "/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")


def test_head_on_missing_transcript_is_still_a_404(client, monkeypatch):
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(_json_upstream(404, {"detail": "not found"}))
    )

    response = client.head("/transcripts/ShuiQianXiaoXi/0001-0100/0001.md")

    assert response.status_code == 404
    assert response.content == b""


def test_search_page_opens_the_search_tab(client):
    response = client.get("/search")

    assert response.status_code == 200
    assert '<link rel="canonical" href="https://bedtime.blog/search"' in response.text
    assert 'data-mode="search"' in response.text
    assert 'class="search-pane is-idle"' in response.text
    assert "noindex" not in response.text


def test_search_page_with_query_prefills_and_is_noindex(client):
    response = client.get("/search", params={"q": " 独山县<b> "})

    assert response.status_code == 200
    assert "<title>独山县&lt;b&gt; - 搜索 · 睡前消息知识库</title>" in response.text
    assert 'value="独山县&lt;b&gt;"' in response.text
    assert 'class="search-pane"' in response.text
    assert '<meta name="robots" content="noindex"' in response.text
    # A noindex page must not also point search engines at a canonical.
    assert 'rel="canonical"' not in response.text


def test_other_pages_open_the_chat_tab(client):
    response = client.get("/")

    assert 'data-mode="chat"' in response.text
    assert 'value=""' in response.text
    assert "__SEARCH" not in response.text


def test_latest_page_renders_the_desk_at_its_own_address(client):
    response = client.get("/latest")

    assert response.status_code == 200
    assert '<link rel="canonical" href="https://bedtime.blog/latest"' in response.text
    assert "<title>最新文稿 · 睡前消息知识库</title>" in response.text
    assert 'data-panel="latest"' in response.text
    assert 'data-view="browse"' in response.text


def test_latest_page_lists_the_desk_as_structured_data(client, monkeypatch):
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(_json_upstream(200, _latest_desk_payload()))
    )

    response = client.get("/latest")

    blocks = [
        json.loads(block)
        for block in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            response.text,
            re.DOTALL,
        )
    ]
    item_list = next(block for block in blocks if block.get("@type") == "ItemList")
    first = item_list["itemListElement"][0]
    assert first["position"] == 1
    assert first["url"].startswith("https://bedtime.blog/transcripts/")
    assert item_list["numberOfItems"] == len(item_list["itemListElement"])


def test_opensearch_description_points_at_the_search_page(client):
    response = client.get("/opensearch.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/opensearchdescription+xml"
    )
    root = ET.fromstring(response.content)
    namespace = {"os": "http://a9.com/-/spec/opensearch/1.1/"}
    url = root.find("os:Url", namespace)
    assert url is not None
    assert url.get("template") == "https://bedtime.blog/search?q={searchTerms}"
    assert root.findtext("os:ShortName", namespaces=namespace) == "睡前消息知识库"
    home = client.get("/")
    assert 'rel="search" type="application/opensearchdescription+xml"' in home.text
    assert 'href="/opensearch.xml"' in home.text


def test_api_responses_are_noindex_but_pages_are_not(client):
    assert client.get("/api/starters").headers["x-robots-tag"] == "noindex"
    assert "x-robots-tag" not in client.get("/").headers


def test_sitemap_contains_root_channels_articles_and_lastmods(client, monkeypatch):
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
    monkeypatch.setattr(server, "_client", fake)

    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(response.content)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = [element.text for element in root.findall("sm:url/sm:loc", namespace)]
    assert locations == [
        "https://bedtime.blog/",
        "https://bedtime.blog/latest",
        "https://bedtime.blog/transcripts?channel=ShuiQianXiaoXi",
        "https://bedtime.blog/transcripts?channel=ChanJingPoBiJi",
        "https://bedtime.blog/transcripts/ShuiQianXiaoXi/0001-0100/0001.md",
        "https://bedtime.blog/transcripts/%E4%BA%A7%E7%BB%8F/%E4%B8%80%E6%9C%9F.md",
    ]
    assert len(root.findall("sm:url/sm:lastmod", namespace)) == len(locations)
    assert len(fake.calls) == 1


def test_sitemap_does_not_return_partial_success_on_bad_upstream(client, monkeypatch):
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(error=httpx.ConnectError("offline"))
    )

    response = client.get("/sitemap.xml")

    assert response.status_code == 503
    assert "temporarily unavailable" in response.text


def test_short_link_redirects_to_the_matching_transcript(client, monkeypatch):
    doc_id = "ShuiQianXiaoXi/0001-0100/0001.md"
    items = [_index_item(doc_id, "【睡前消息1】测试", "2025-01-01")]
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(_json_upstream(200, {"items": items}))
    )

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


def test_short_link_rejects_malformed_ids_without_querying_upstream(
    client, monkeypatch
):
    fake = _FakeJsonClient(_json_upstream(200, {"items": []}))
    monkeypatch.setattr(server, "_client", fake)

    too_short = client.get("/s/abc", follow_redirects=False)
    bad_char = client.get("/s/0OIl1234", follow_redirects=False)

    assert too_short.status_code == 404
    assert bad_char.status_code == 404
    assert fake.calls == []


def test_short_link_rebuilds_on_a_cache_miss_once_the_interval_passed(
    client, monkeypatch
):
    fake = _FakeJsonClient(_json_upstream(200, {"items": []}))
    monkeypatch.setattr(server, "_client", fake)
    now = [1000.0]
    monkeypatch.setattr(server, "_now", lambda: now[0])

    client.get("/s/11111111", follow_redirects=False)
    now[0] += server.SHORT_ID_MIN_REFRESH_SECONDS
    response = client.get("/s/11111111", follow_redirects=False)

    assert response.status_code == 404
    assert len(fake.calls) == 2


def test_short_link_misses_do_not_refetch_within_the_interval(client, monkeypatch):
    fake = _FakeJsonClient(_json_upstream(200, {"items": []}))
    monkeypatch.setattr(server, "_client", fake)
    monkeypatch.setattr(server, "_now", lambda: 1000.0)

    for suffix in "12345":
        response = client.get(f"/s/1111111{suffix}", follow_redirects=False)
        assert response.status_code == 404

    assert len(fake.calls) == 1


def test_short_link_returns_503_when_upstream_is_down(client, monkeypatch):
    monkeypatch.setattr(
        server, "_client", _FakeJsonClient(error=httpx.ConnectError("offline"))
    )

    response = client.get(
        f"/s/{server._short_id_for('ShuiQianXiaoXi/0001-0100/0001.md')}",
        follow_redirects=False,
    )

    assert response.status_code == 503


# --- /healthz and /readyz (blue-green design 2.3) ----------------------------


class _FakeHealthClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def _health_response(status_code, payload):
    request = httpx.Request("GET", server.HEALTH_ENDPOINT)
    return httpx.Response(status_code, json=payload, request=request)


AGENT_HEALTH = {
    "ready": True,
    "snapshot": {
        "id": "s20261007t091512z_a1b2c3d",
        "schema": "rag_s20261007t091512z_a1b2c3d",
        "format_version": 1,
        "embedding_space": "Qwen/Qwen3-Embedding-4B@2560",
        "published_at": "2026-10-07T09:15:40Z",
        "data_age_seconds": 60,
        "source_commit": "a1b2c3d",
    },
    "embedding_space": "Qwen/Qwen3-Embedding-4B@2560",
    "supported_formats": [1],
    "indexer_status": {
        "last_result": "failed",
        "last_error": "OperationalError: password authentication failed for postgres_user",
        "consecutive_failures": 3,
    },
}


def test_healthz_reports_instance_and_never_calls_the_agent(client, monkeypatch):
    monkeypatch.setattr(server, "APP_INSTANCE", "bedtimenews-app-green")
    fake = _FakeHealthClient(error=AssertionError("healthz must not call the agent"))
    monkeypatch.setattr(server, "_client", fake)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": server.APP_VERSION,
        "instance": "bedtimenews-app-green",
    }
    assert fake.calls == []


def test_readyz_forwards_readiness_with_a_trimmed_body(client, monkeypatch):
    fake = _FakeHealthClient(_health_response(200, AGENT_HEALTH))
    monkeypatch.setattr(server, "_client", fake)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "snapshot": {
            "id": "s20261007t091512z_a1b2c3d",
            "format_version": 1,
            "embedding_space": "Qwen/Qwen3-Embedding-4B@2560",
        },
    }
    assert "password" not in response.text
    assert "indexer_status" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert fake.calls[0][0] == server.HEALTH_ENDPOINT
    assert fake.calls[0][1]["timeout"] == server.READY_TIMEOUT_S


def test_readyz_is_503_when_the_agent_is_not_ready(client, monkeypatch):
    body = AGENT_HEALTH | {
        "ready": False,
        "snapshot": None,
        "reason": "no published snapshot",
    }
    monkeypatch.setattr(
        server, "_client", _FakeHealthClient(_health_response(503, body))
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "snapshot": None,
        "reason": "no published snapshot",
    }
    assert "indexer_status" not in response.text


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")]
)
def test_readyz_is_503_when_the_agent_is_unreachable(client, error, monkeypatch):
    monkeypatch.setattr(server, "_client", _FakeHealthClient(error=error))

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "snapshot": None,
        "reason": "agent unreachable",
    }


def test_readyz_never_trusts_a_200_without_ready(client, monkeypatch):
    monkeypatch.setattr(
        server,
        "_client",
        _FakeHealthClient(_health_response(200, {"unexpected": True})),
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["ready"] is False


# --- hardening -----------------------------------------------------------------


class _RecordingGetClient:
    def __init__(self):
        self.urls = []

    async def get(self, url, **_kwargs):
        self.urls.append(str(url))
        return httpx.Response(
            200, json={"leak": True}, request=httpx.Request("GET", str(url))
        )


@pytest.mark.parametrize(
    "path",
    [
        "/api/transcripts/..%2Fhealth",
        "/api/transcripts/%2E%2E/health",
        "/api/transcripts/a/..%2F..%2Fhealth",
        "/api/transcripts/channel/not-markdown.txt",
    ],
)
def test_transcript_api_never_proxies_a_non_canonical_uri(client, path, monkeypatch):
    # httpx normalizes dot segments, so an unvalidated "../health" would reach
    # the agent's internal /health (with indexer error text) from the internet.
    fake = _RecordingGetClient()
    monkeypatch.setattr(server, "_client", fake)

    response = client.get(path)

    assert response.status_code == 404
    assert fake.urls == []


def test_transcript_api_proxies_a_canonical_uri(client, monkeypatch):
    fake = _RecordingGetClient()
    monkeypatch.setattr(server, "_client", fake)

    response = client.get("/api/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")

    assert response.status_code == 200
    assert fake.urls == [
        f"{server.TRANSCRIPTS_ENDPOINT}/ShuiQianXiaoXi/0501-0600/0588.md"
    ]


class _MarkdownGetClient:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    async def get(self, url, **_kwargs):
        self.urls.append(str(url))
        return httpx.Response(
            200, json=self.payload, request=httpx.Request("GET", str(url))
        )


def test_transcript_markdown_download_uses_title_filename(client, monkeypatch):
    fake = _MarkdownGetClient(
        {
            "doc_id": "ShuiQianXiaoXi/0501-0600/0588.md",
            "source_title": "【睡前消息588】城投债四大网红，柳州第一",
            "canonical_title": "睡前消息588",
            "body_markdown": "# 【睡前消息588】城投债四大网红，柳州第一\n\n## 正文\n",
        }
    )
    monkeypatch.setattr(server, "_client", fake)

    response = client.get(
        "/api/transcripts/ShuiQianXiaoXi/0501-0600/0588.md?download=markdown"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"] == server._content_disposition(
        "【睡前消息588】城投债四大网红，柳州第一.md"
    )
    assert response.text.startswith("# 【睡前消息588】城投债四大网红，柳州第一")
    assert fake.urls == [
        f"{server.TRANSCRIPTS_ENDPOINT}/ShuiQianXiaoXi/0501-0600/0588.md"
    ]


def test_transcript_markdown_download_rejects_non_canonical_uri(client, monkeypatch):
    fake = _RecordingGetClient()
    monkeypatch.setattr(server, "_client", fake)

    response = client.get("/api/transcripts/../health?download=markdown")

    assert response.status_code == 404
    assert fake.urls == []


def test_transcript_markdown_download_is_404_without_source(client, monkeypatch):
    fake = _MarkdownGetClient(
        {
            "doc_id": "ShuiQianXiaoXi/0501-0600/0588.md",
            "source_title": "标题",
            "body_html": "<p>x</p>",
        }
    )
    monkeypatch.setattr(server, "_client", fake)

    response = client.get(
        "/api/transcripts/ShuiQianXiaoXi/0501-0600/0588.md?download=markdown"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "文稿源暂不可用"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_no_generated_api_docs_are_public(client, path):
    assert client.get(path).status_code == 404


def test_chat_refuses_an_oversized_body_without_calling_the_agent(client, monkeypatch):
    monkeypatch.setattr(
        server,
        "_client",
        _FakeUpstreamClient(error=AssertionError("the agent must not be called")),
    )
    question = "问" * (server.MAX_CHAT_BODY_BYTES // 3 + 10)

    response = client.post("/chat", json={"question": question})

    assert response.status_code == 413


def test_chat_does_not_leak_unexpected_error_text(client, monkeypatch):
    monkeypatch.setattr(
        server,
        "_client",
        _FakeUpstreamClient(
            error=RuntimeError("connect to http://agent:8000 failed: secret detail")
        ),
    )

    response = client.post("/chat", json={"question": "test"})

    assert _error_event(response) == {
        "type": "error",
        "content": "信号中断，请稍后重试。",
    }
    assert "agent:8000" not in response.text


def test_pages_cannot_be_framed(client):
    headers = client.get("/healthz").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"


def _latest_desk_payload():
    return {
        "snapshot_id": "s_test",
        "items": [
            {
                "doc_id": "CanKaoXinXi/0601-0700/0654.md",
                "canonical_title": "参考信息654",
                "source_title": "【参考信息654】再查“不想生不敢生”",
                "channel": "CanKaoXinXi",
                "publication_date": "2026-09-23",
                "headings": [
                    {"text": "富豪排队补税", "anchor": "富豪排队补税"},
                    {"text": "青年跨国抗议", "anchor": ""},
                ],
            }
        ],
    }


def test_home_renders_latest_desk_from_agent_cache(client, monkeypatch):
    upstream = _FakeJsonClient(_json_upstream(200, _latest_desk_payload()))
    monkeypatch.setattr(server, "_client", upstream)

    response = client.get("/")

    assert response.status_code == 200
    assert (
        '<span class="latest-item-title">【参考信息654】再查“不想生不敢生”</span>'
        in response.text
    )
    assert '<span class="latest-item-meta">2026-09-23</span>' in response.text
    anchored = "/transcripts/CanKaoXinXi/0601-0700/0654.md#" + quote(
        "富豪排队补税", safe=""
    )
    assert f'href="{anchored}"' in response.text
    # A heading without an anchor links to the document top.
    assert (
        'href="/transcripts/CanKaoXinXi/0601-0700/0654.md" data-route' in response.text
    )
    assert 'class="latest-end-chip"' in response.text
    assert upstream.calls[0][0] == server.LATEST_ENDPOINT
    # Inside the micro-cache TTL a second render must not hit the agent again.
    client.get("/")
    assert len(upstream.calls) == 1


def test_home_latest_desk_serves_stale_on_upstream_failure(client, monkeypatch):
    good = _FakeJsonClient(_json_upstream(200, _latest_desk_payload()))
    monkeypatch.setattr(server, "_client", good)
    assert client.get("/").status_code == 200

    broken = _FakeJsonClient(error=httpx.ConnectError("boom"))
    monkeypatch.setattr(server, "_client", broken)
    response = client.get("/")

    assert response.status_code == 200
    assert 'class="latest-item-title"' in response.text


def test_home_latest_desk_shows_note_without_any_cache(client, monkeypatch):
    broken = _FakeJsonClient(error=httpx.ConnectError("boom"))
    monkeypatch.setattr(server, "_client", broken)

    response = client.get("/")

    assert response.status_code == 200
    assert "文稿目录暂时不可用" in response.text
    assert "latest-item-title" not in response.text


def test_latest_desk_has_no_public_api(client):
    # The desk is rendered server-side only; the internal agent endpoint must
    # never gain a proxied twin under /api.
    assert client.get("/api/latest").status_code == 404


def test_page_carries_the_public_base_url_for_the_client(client, monkeypatch):
    # One source for the domain: the page tells the script which origin to use
    # for the links in a copied answer, and a changed PUBLIC_BASE_URL follows.
    response = client.get("/")
    assert 'name="public-base-url" content="https://bedtime.blog"' in response.text
    assert "__PUBLIC_BASE_URL__" not in response.text

    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://example.org")
    response = client.get("/")
    assert 'name="public-base-url" content="https://example.org"' in response.text
