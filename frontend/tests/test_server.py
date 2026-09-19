import json

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
    try:
        yield test_client
    finally:
        test_client.close()
        server._client = None


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
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
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


def test_transcript_deep_links_return_spa_and_security_headers(client):
    response = client.get("/transcripts/ShuiQianXiaoXi/0501-0600/0588.md")

    assert response.status_code == 200
    assert "睡前消息知识库" in response.text
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-src 'none'" in response.headers["content-security-policy"]
