"""The overall /chat time limit (blue-green design 4.3)."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from src import chat, main
from src.models import ChatRequest
from src.snapshots import snapshot_manager


def _events(chunks):
    return [
        json.loads(c.removeprefix("data: "))
        for c in chunks
        if c.startswith("data: ") and c.strip() != "data: [DONE]"
    ]


def _collect(request):
    async def run():
        return [c async for c in chat.stream_chat(request)]

    return asyncio.run(run())


def test_stream_past_the_limit_ends_with_an_error_event(monkeypatch):
    monkeypatch.setattr(chat, "CHAT_TIME_LIMIT_S", 0.3)
    monkeypatch.setattr(chat, "HEARTBEAT_INTERVAL_S", 0.05)
    cancelled = []

    async def endless(_question, _history):
        yield {"type": "step", "step": "route", "content": "RAG"}
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        yield {"type": "answer_chunk", "content": "never"}

    monkeypatch.setattr(chat, "agent_stream_query", endless)
    started = time.monotonic()
    chunks = _collect(ChatRequest(question="q", stream=True))
    elapsed = time.monotonic() - started

    assert elapsed < 2, "the limit must end the stream, not the producer"
    events = _events(chunks)
    assert events[0]["type"] == "step"
    assert events[-1] == {"type": "error", "content": chat.CHAT_TIMEOUT_MESSAGE}
    assert chunks[-1] == "data: [DONE]\n\n"
    assert ": ping\n\n" in chunks, "heartbeats keep flowing until the limit"
    assert cancelled, "the producer task is cancelled at the limit"


def test_stream_within_the_limit_is_untouched(monkeypatch):
    async def quick(_question, _history):
        yield {"type": "answer_chunk", "content": "答"}

    monkeypatch.setattr(chat, "agent_stream_query", quick)
    events = _events(_collect(ChatRequest(question="q", stream=True)))
    assert events == [{"type": "answer_chunk", "content": "答"}]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(snapshot_manager, "start", lambda: None)
    with TestClient(main.app) as test_client:
        yield test_client


def test_nonstream_past_the_limit_is_a_504(client, monkeypatch):
    monkeypatch.setattr(chat, "CHAT_TIME_LIMIT_S", 0.2)

    def slow(_question, _history):
        time.sleep(1)
        return {"answer": "late"}

    monkeypatch.setattr(chat, "agent_query", slow)
    response = client.post("/chat", json={"question": "q"})
    assert response.status_code == 504
    assert response.json() == {"detail": chat.CHAT_TIMEOUT_MESSAGE}


def test_limit_layering_matches_the_deployment_files():
    """240 s < uvicorn 270 s < compose 300 s, as the comments in chat.py state."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    agent_docker = (root / "agent" / "Dockerfile").read_text()
    web_docker = (root / "frontend" / "Dockerfile").read_text()
    app_compose = (root / "compose.app.yml").read_text()
    assert chat.CHAT_TIME_LIMIT_S == 240
    assert "--timeout-graceful-shutdown" in agent_docker
    assert "270" in agent_docker and '"270"' in web_docker
    assert app_compose.count("stop_grace_period: 300s") == 2
