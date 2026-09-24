"""
LangGraph-based Agentic RAG agent for BedtimeNews knowledge base.

Public API:
    agent_query(question: str, history: list[dict] | None = None) -> dict
        Process a user question and return complete result.

    agent_stream_query(question: str, history: list[dict] | None = None)
            -> AsyncIterator[dict]
        Process a user question and yield streaming events.

Usage:
    from .agent import agent_query, agent_stream_query

    # Synchronous query
    result = agent_query("How much debt in Du Shan's county?")
    print(result["answer"])

    # Streaming query
    async for event in agent_stream_query("What is the Hengshui model?"):
        if event["type"] == "answer_chunk":
            print(event["content"], end="")
"""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages.ai import AIMessageChunk

from .graph import AgentState, build_citation_urls, create_initial_state, graph

# Graph nodes -> step types emitted to streaming clients.
_NODE_STEP_TYPES = {
    "condense": "condense",
    "route": "route",
    "query_rewrite": "rewrite",
    "retrieve": "retrieve",
    "grade": "grade",
    "generate": "generate",
    "direct": "generate",
}

# ============================================================================
# Public API
# ============================================================================


def agent_query(question: str, history: list[dict] | None = None) -> dict:
    """
    Process a user question through the agentic workflow.

    Args:
        question: User's question
        history: Prior turns, oldest first, each {question, answer, grounded}

    Returns:
        Dict with the answer and any suggested follow-up questions
    """
    initial_state: AgentState = create_initial_state(question, history)
    final_state = graph.invoke(input=initial_state)
    return {
        "answer": final_state.get("final_answer", ""),
        "followups": final_state.get("followups", []),
        "grounded": bool(final_state.get("relevant_documents")),
    }


async def agent_stream_query(
    question: str, history: list[dict] | None = None
) -> AsyncIterator[dict[str, Any]]:
    """
    Stream answer chunks and intermediate reasoning steps from the agentic RAG workflow.

    This function executes the LangGraph workflow and streams both intermediate steps
    and final answer content, providing visibility into the RAG pipeline progress.

    Args:
        question: The user's input question or query string
        history: Prior turns, oldest first, each {question, answer, grounded}

    Yields:
        dict: Events with structure:
            {
                "type": "step" | "citations" | "answer_chunk" |
                        "answer_final" | "answer_meta" | "followups",
                "step": "condense" | "route" | "rewrite" |
                        "retrieve" | "grade" | "generate",
                "content": "step description or text content"
            }

    Event Types:
        - "step": Intermediate pipeline steps with descriptions
        - "answer_chunk": LLM-generated answer content
        - "citations": {"urls": {name: {"title", "url"}}}, keyed by both the
          document URI (what the model writes) and its 标准化标题; emitted after
          grading and before the first chunk, so clients can linkify citations
          while the answer streams.
        - "followups": {"items": [question, ...]}, suggested next questions
          parsed out of the completion after the answer. The raw token stream
          still contains the delimiter and this block, so clients must hide
          everything from FOLLOWUPS_DELIMITER onward while streaming.
        - "answer_final": {"content", "grounded"} — the completed,
          post-processed answer. Clients should render it in place of the text
          they accumulated. Sent only when the client could not arrive at the
          same text itself: a citation repair changed something, or nothing
          streamed at all.
        - "answer_meta": {"grounded"} — sent instead of answer_final when the
          streamed text is already correct. Carries whether the turn was
          answered from retrieved documents, which the client sends back as
          ChatTurn.grounded on the next request.

    Steps Emitted:
        1. "condense": Follow-up reference resolution, when needed
        2. "route": Initial routing decision (direct/RAG)
        3. "rewrite": Query optimization
        4. "retrieve": Document retrieval results
        5. "grade": Document grading results
        6. "generate": Final answer generation (streamed as chunks)

    Example:
        async for event in agent_stream_query("独山县的债务有多严重？"):
            if event["type"] == "step":
                print(f"[{event['step']}] {event['content']}")
            else:
                print(event)

    Note:
        This function provides full pipeline visibility for debugging and user feedback.
    """
    initial_state = create_initial_state(question, history)

    # Track which steps we've emitted to avoid duplicates
    emitted_steps = set()
    # Whether any token reached the client. If nothing streamed, the final answer
    # has to be sent whole or there is nothing on screen.
    streamed_any = False

    async for event in graph.astream_events(initial_state, version="v2"):
        event_name = event.get("event", "")
        langgraph_node = event.get("metadata", {}).get("langgraph_node", "")
        step_type = _NODE_STEP_TYPES.get(langgraph_node)

        # Emit reasoning steps from each node completion
        if event_name == "on_chain_end" and step_type:
            output = event.get("data", {}).get("output")

            # Handle different output formats from LangGraph
            if isinstance(output, dict):
                state = output
            else:
                continue

            # Emit reasoning steps from each node
            reasoning_steps = state.get("reasoning_steps", [])
            for step in reasoning_steps:
                # Get step content safely
                step_content = step.content if hasattr(step, "content") else str(step)

                # Create a unique key for this step
                step_key = f"{langgraph_node}_{step_content}"

                if step_key not in emitted_steps:
                    emitted_steps.add(step_key)
                    yield {
                        "type": "step",
                        "step": step_type,
                        "content": step_content,
                    }

            # Grading settles which documents the answer may cite, and it runs
            # before a single token is generated. Ship the episode -> URL map now
            # so the client can linkify citations as they stream in; waiting for
            # the server-side repair would leave them as dead text for the whole
            # length of the answer.
            if langgraph_node == "grade":
                citation_urls = build_citation_urls(
                    state.get("relevant_documents") or []
                )
                if citation_urls:
                    yield {"type": "citations", "urls": citation_urls}

            # The answer nodes post-process the completed text — notably
            # _repair_citations, which rewrites bare [[名称]] citations into full
            # markdown links. That happens after the model has already streamed
            # its raw tokens, so a client that renders what it accumulated shows
            # the unrepaired version.
            #
            # Re-sending the whole answer is only worth the bytes when the client
            # cannot arrive at the same text itself. It can strip the follow-up
            # block on its own, so the two cases that matter are a repair having
            # changed something, and nothing having streamed at all — without
            # which the client would have nothing to render.
            if langgraph_node in ("generate", "direct"):
                final_answer = state.get("final_answer")
                grounded = bool(state.get("relevant_documents"))
                if final_answer and (state.get("answer_repaired") or not streamed_any):
                    yield {
                        "type": "answer_final",
                        "content": final_answer,
                        "grounded": grounded,
                    }
                else:
                    # Still tell the client how this turn was answered: it feeds
                    # the grounded flag back on the next request, and inferring it
                    # from whether a citations event arrived couples the two
                    # silently.
                    yield {"type": "answer_meta", "grounded": grounded}
                followups = state.get("followups")
                if followups:
                    yield {"type": "followups", "items": followups}

        # Stream LLM tokens from answer generation nodes
        if event_name == "on_chat_model_stream":
            if langgraph_node != "direct" and langgraph_node != "generate":
                continue

            chunk = event.get("data", {}).get("chunk")
            if not isinstance(chunk, AIMessageChunk):
                continue

            streamed_any = True
            yield {"type": "answer_chunk", "content": chunk.content}
