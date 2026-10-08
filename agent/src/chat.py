"""Chat endpoint implementation, stream and non-stream."""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator

from .agent import agent_query, agent_stream_query
from .models import ChatRequest, ChatResponse
from .snapshots import SnapshotUnavailable

logger = logging.getLogger(__name__)

# Emit an SSE heartbeat comment if no real event has been produced for this long.
# Keeps proxy/TCP buffers flushed and the (mobile) connection warm during the
# silent gaps between pipeline stages (route -> rewrite -> retrieve -> grade can
# run for seconds with no answer chunks). Clients ignore lines not starting with
# "data: ", so the comment is invisible to the UI.
HEARTBEAT_INTERVAL_S = 1.0

# Overall limit for one /chat request, streaming or not. The longest stream
# seen in production is about 2.5 minutes. Each shutdown layer outside this one
# is longer than the one inside it, so a stopping process always lets an
# in-flight answer finish (docs/designs/20261008_blue-green-deployment.md, 4.3):
#
#   240 s  this limit: the stream ends with an SSE error event
#   270 s  uvicorn --timeout-graceful-shutdown (agent and web Dockerfiles)
#   300 s  compose stop_grace_period for agent and web (SIGKILL after that)
#   5 min  the edge proxy's drain cap during a blue-green switch
#
# Snapshot garbage collection also relies on it: a superseded snapshot is kept
# for 10 minutes, longer than any request pinned to it can run
# (docs/designs/20261007_rag-snapshot-architecture.md, 6.8).
CHAT_TIME_LIMIT_S = 240.0
CHAT_TIMEOUT_MESSAGE = "回答超时，请缩小问题范围后重试。"
# Error events reach the browser verbatim, so they carry these fixed messages;
# the exception itself is logged (it can contain internal URLs, SQL or
# provider error text).
CHAT_UNAVAILABLE_MESSAGE = "知识库暂时不可用，请稍后重试。"
CHAT_FAILED_MESSAGE = "回答生成失败，请稍后重试。"


class ChatTimeout(TimeoutError):
    """A non-streaming /chat exceeded CHAT_TIME_LIMIT_S."""


async def nonstream_chat_with_limit(request: ChatRequest) -> ChatResponse:
    """Run the synchronous pipeline in a worker thread, bounded by the limit.

    A thread cannot be cancelled: on timeout the caller gets ChatTimeout at
    once, while the abandoned worker finishes its current model call and its
    result is discarded.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(nonstream_chat, request), timeout=CHAT_TIME_LIMIT_S
        )
    except TimeoutError as exc:
        logger.warning("Chat exceeded the %.0f s limit", CHAT_TIME_LIMIT_S)
        raise ChatTimeout(CHAT_TIMEOUT_MESSAGE) from exc


def nonstream_chat(request: ChatRequest) -> ChatResponse:
    history = [turn.model_dump() for turn in request.history]
    result = agent_query(request.question, history)
    answer = result["answer"]
    logger.info(f"Chat completed: {len(answer)} chars, {len(history)} prior turn(s)")
    return ChatResponse(
        answer=answer,
        followups=result.get("followups", []),
        grounded=result.get("grounded", False),
    )


async def stream_chat(request: ChatRequest) -> AsyncGenerator[str]:
    """
    Stream chat responses in Server-Sent Events (SSE) format.

    This function acts as a streaming endpoint that:
    1. Takes a chat request with a user question
    2. Streams the agent's pipeline steps and answer chunks as SSE events
    3. Handles errors gracefully within the stream

    Args:
        request: ChatRequest object containing the user's question and optional parameters

    Yields:
        str: SSE-formatted events in the format "data: {json_event}\n\n"
             (plus ": ping" heartbeat comments during silent gaps).
             Stream ends with "data: [DONE]"

    Event Format:
        The stream can contain step, citations, answer_chunk, answer_final,
        answer_meta, followups, and error events. See ``agent_stream_query`` for
        their payloads.

    Error Handling:
        If the request runs longer than CHAT_TIME_LIMIT_S, or an exception
        occurs during streaming, yields an error event and ends the stream:
        {
            "type": "error",
            "content": "error message"
        }

    Example:
        request = ChatRequest(question="独山县的债务有多严重？", stream=True)
        async for event in stream_chat(request):
            print(event)
    """
    # Drive the agent in a background task feeding a queue, so the consumer loop
    # can emit periodic heartbeats while waiting. (We can't wait_for() the
    # generator's __anext__ directly: a timeout would cancel and corrupt it.)
    queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

    async def produce() -> None:
        try:
            history = [turn.model_dump() for turn in request.history]
            async for event in agent_stream_query(request.question, history):
                await queue.put(("event", event))
        except SnapshotUnavailable as e:
            logger.warning("Chat stream without a readable snapshot: %s", e)
            await queue.put(("error", CHAT_UNAVAILABLE_MESSAGE))
        except Exception:  # noqa: BLE001 - reported to the client below
            logger.exception("Error streaming chat response")
            await queue.put(("error", CHAT_FAILED_MESSAGE))
        finally:
            await queue.put(("done", None))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + CHAT_TIME_LIMIT_S
    task = asyncio.create_task(produce())
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "Chat stream exceeded the %.0f s limit", CHAT_TIME_LIMIT_S
                )
                error_event = {"type": "error", "content": CHAT_TIMEOUT_MESSAGE}
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
                break
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(), timeout=min(HEARTBEAT_INTERVAL_S, remaining)
                )
            except TimeoutError:
                if loop.time() < deadline:
                    yield ": ping\n\n"
                continue

            # ensure_ascii=False: the stream is already UTF-8, and escaping CJK
            # to \uXXXX spends six bytes where three would do — roughly doubling
            # the payload for an archive that is almost entirely Chinese.
            if kind == "event":
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif kind == "error":
                error_event = {"type": "error", "content": payload}
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
                break
            else:  # "done"
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # SSE events must end with a blank line; always terminate the stream so
    # clients waiting for [DONE] don't hang after an error.
    yield "data: [DONE]\n\n"
