"""Chat endpoint implementation, stream and non-stream."""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator

from .agent import agent_query, agent_stream_query
from .models import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

# Emit an SSE heartbeat comment if no real event has been produced for this long.
# Keeps proxy/TCP buffers flushed and the (mobile) connection warm during the
# silent gaps between pipeline stages (route -> rewrite -> retrieve -> grade can
# run for seconds with no answer chunks). Clients ignore lines not starting with
# "data: ", so the comment is invisible to the UI.
HEARTBEAT_INTERVAL_S = 1.0


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
        If an exception occurs during streaming, yields an error event:
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
        except Exception as e:  # noqa: BLE001 - forwarded to the client below
            logger.exception("Error streaming chat response")
            await queue.put(("error", str(e)))
        finally:
            await queue.put(("done", None))

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(), timeout=HEARTBEAT_INTERVAL_S
                )
            except TimeoutError:
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
