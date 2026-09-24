"""Pydantic models for retrieval API."""

from pydantic import BaseModel, Field

from .settings import settings

# ============================================================================
# Chat API Models
# ============================================================================


class ChatTurn(BaseModel):
    """One completed exchange, replayed by the client to give a follow-up context.

    The answer is expected to arrive truncated — resolving "那它呢" needs the
    subject of the previous turn, not its full text — and the client is the only
    owner of conversation state, so the whole stack stays stateless.
    """

    question: str = Field(..., max_length=2000)
    answer: str = Field(default="", max_length=1000)
    grounded: bool = Field(
        default=False,
        description=(
            "Whether that turn was answered from retrieved documents. Records the "
            "difference between 'we discussed this and cited episodes' and 'the "
            "archive had nothing', so a later turn cannot act as if it had sources."
        ),
    )


class ChatRequest(BaseModel):
    """Request model for chat endpoint."""

    question: str = Field(
        ...,
        description="User's question or message",
        min_length=1,
        max_length=2000,
    )
    history: list[ChatTurn] = Field(
        default_factory=list,
        description=(
            "Prior turns, oldest first. Capped because every turn is replayed on "
            "each request; the client sends only the most recent few."
        ),
        max_length=8,
    )
    stream: bool = Field(
        default=False,
        description="Whether to stream the response",
    )


class ChatResponse(BaseModel):
    """Response model for chat endpoint."""

    answer: str = Field(..., description="Generated answer")
    followups: list[str] = Field(
        default_factory=list,
        description="Suggested next questions, answerable from the archive",
    )
    grounded: bool = Field(
        default=False,
        description=(
            "Whether the answer was built from retrieved documents. Send it back "
            "as ChatTurn.grounded on the next request."
        ),
    )


# ============================================================================
# Retrieve API Models
# ============================================================================


class RetrieveRequest(BaseModel):
    """Request model for semantic search retrieval."""

    query: str = Field(
        ...,
        description="Semantic search query string",
        min_length=1,
    )
    match_threshold: float = Field(
        default_factory=lambda: settings.match_threshold,
        description="Cosine similarity threshold (0.0-1.0)",
        ge=0.0,
        le=1.0,
    )
    match_count: int = Field(
        default=10,
        description="Maximum number of results to return",
        ge=1,
        le=100,
    )
    doc_id_filter: list[str] | None = Field(
        default=None,
        description="Optional list of doc_ids to restrict search to",
    )
    include_text: bool = Field(
        default=True,
        description="Whether to include full text in results",
    )
    include_heading: bool = Field(
        default=True,
        description="Whether to include heading in results",
    )


class ChunkResult(BaseModel):
    """Individual chunk result from retrieval."""

    chunk_id: str = Field(..., description="Unique chunk identifier")
    doc_id: str = Field(
        ..., description="Document URI, e.g. ShuiQianXiaoXi/0501-0600/0588.md"
    )
    title: str | None = Field(
        None, description="标准化标题 for the document, e.g. 睡前消息588"
    )
    chunk_index: int | None = Field(None, description="Chunk index within document")
    heading: str | None = Field(None, description="Section heading")
    text: str | None = Field(None, description="Chunk content")
    word_count: int = Field(..., description="Word count of chunk")
    similarity: float = Field(..., description="Cosine similarity score (0.0-1.0)")
    rank: int = Field(..., description="1-based rank in result list")


class RetrieveResponse(BaseModel):
    """Response model for semantic search retrieval."""

    query: str = Field(..., description="Query string used for search")
    match_threshold: float = Field(..., description="Similarity threshold used")
    match_count: int = Field(..., description="Maximum results requested")
    results: list[ChunkResult] = Field(
        default_factory=list,
        description="List of matching chunks",
    )
