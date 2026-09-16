# Agent Service

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

Agentic RAG service implementing routing, query optimization, semantic
retrieval, document-conditioned answer generation, and episode citations.

See the [main README](../README.en.md) for setup instructions.

## Architecture

### Agentic RAG Workflow

![Agentic RAG workflow](../docs/diagrams/agent-workflow.svg)

**Components:**

- `agent.py`: Public API (`agent_query()`, `agent_stream_query()`)
- `graph.py`: LangGraph workflow with intelligent routing
- `retriever.py`: Semantic search (embeddings + pgvector)
- `cache.py`: LRU cache for query results
- `chat.py`: FastAPI endpoint handlers
- `main.py`: FastAPI server
- `vector_db.py`: PostgreSQL + pgvector operations

### Routing Behavior

The router classifies user input into three categories, which feed two execution
paths:

**Direct Path - Greeting** (no retrieval):

- Simple greetings: "hi", "hello", "你好"
- Meta-questions: "who are you", "what can you do"

**Direct Path - Out of Scope** (no retrieval):

- General knowledge: "1+1等于几", "法国首都是哪里"
- Unrelated topics: "今天天气怎么样", "怎么煮面"
- Real-time data: Weather, stock prices, current events
- The assistant does not answer these from model knowledge; it briefly explains
  that they fall outside the transcript archive and redirects to archive topics

**RAG Path** (retrieval-augmented):

- BedtimeNews-related questions (default)
- Chinese domestic affairs, policy, economy
- International relations, geopolitics, conflicts
- Technology, science, AI, infrastructure
- Social issues, education, healthcare, demographics

Before routing, a condense step resolves follow-up references against up to
eight client-supplied history turns. A RAG query with no relevant documents is
rewritten and retried once before the generator returns a no-results response.

### Grounding Boundary

- The generation prompt instructs the model to support factual claims with
  documents retrieved for the current turn. Earlier conversation is reference
  context, not evidence.
- The no-retrieval path instructs the model to refuse factual answers outside
  the archive.
- Citation post-processing repairs known episode links and appends a source list
  if the model emits no citation.
- This is prompt- and citation-based grounding, not claim-level verification.
  The service does not test whether every generated claim is entailed by its
  citation. The API's `grounded` flag means relevant documents were supplied to
  generation; it is not an entailment score.

## API Reference

### POST /chat

**Request:**

```json
{
  "question": "独山县的债务问题有多严重？",
  "history": [],
  "stream": false
}
```

**Parameters:**

- `question` (required): User query
- `history` (optional, default: `[]`): Up to eight prior
  `{question, answer, grounded}` turns
- `stream` (optional, default: false): Enable SSE streaming

**Response (Non-streaming):**

```json
{
  "answer": "根据[[睡前消息588]](https://bedtimenewsstudio.github.io/BedtimeNews-Transcripts/contents/ShuiQianXiaoXi/0501-0600/0588.html)...",
  "followups": ["独山县后来如何化解债务？"],
  "grounded": true
}
```

**Response (Streaming):**

```plaintext
data: {"type": "step", "step": "route", "content": "..."}
data: {"type": "citations", "urls": {"ShuiQianXiaoXi/0501-0600/0588.md": {"title": "睡前消息588", "url": "https://bedtimenewsstudio.github.io/BedtimeNews-Transcripts/contents/ShuiQianXiaoXi/0501-0600/0588.html"}}}
data: {"type": "answer_chunk", "content": "根据"}
data: {"type": "answer_chunk", "content": "睡前"}
data: {"type": "answer_meta", "grounded": true}
data: {"type": "followups", "items": ["独山县后来如何化解债务？"]}
...
data: [DONE]
```

The stream can also contain `answer_final` when post-processing changed the
streamed answer, `error` on failure, and `: ping` heartbeat comments during
silent pipeline stages.

## Evaluation

These are manual evaluation harnesses (they hit a live DB/LLM), not automated
unit tests. For unit tests see this component's `tests/` directory
(run with `cd agent && uv run pytest`).

### Evaluate Agent (Full Agentic RAG Flow)

```bash
# Test a single custom query
docker compose exec agent python -m src.eval_agent -q "独山县的债务问题"
docker compose exec agent python -m src.eval_agent --query "王文银的创业故事有哪些可疑之处"

# List query categories
docker compose exec agent python -m src.eval_agent --list-categories

# Test specific category
docker compose exec agent python -m src.eval_agent --category education

# Random sample
docker compose exec agent python -m src.eval_agent --random 10

# Limit to first N queries
docker compose exec agent python -m src.eval_agent --limit 3
```

### Evaluate Retriever (Retrieval Only)

```bash
# Score the fixed 20-query labelled set. This runs against the live embedding
# API and database, then appends recall@k and per-query ranks to the tracked
# agent/eval_results/retriever.json history on the host.
docker compose run --rm --build \
  --volume ./agent/eval_results:/app/eval_results \
  agent python -m src.eval_retriever --labelled

# Optionally identify a run in the history.
docker compose run --rm --build \
  --volume ./agent/eval_results:/app/eval_results \
  agent python -m src.eval_retriever --labelled --run-label grader-change

# Test a single custom query
docker compose exec agent python -m src.eval_retriever -q "独山县"
docker compose exec agent python -m src.eval_retriever --query "你的问题"

# Test retrieval with custom parameters
docker compose exec agent python -m src.eval_retriever \
  --category education \
  --match-count 10 \
  --threshold 0.3

# Random sample
docker compose exec agent python -m src.eval_retriever --random 20
```

## Configuration

### Model Selection

Chat and embeddings are configured independently via `LLM_PROVIDER` and
`EMBEDDING_PROVIDER`. Model names are read from provider-prefixed env vars, so
the keys depend on the providers you pick. With the defaults
(`LLM_PROVIDER=deepseek`, `EMBEDDING_PROVIDER=siliconflow`), configure in `.env`:

```bash
# Fast model (routing, query rewrite, grading)
DEEPSEEK_FAST_MODEL=deepseek-v4-flash

# Generation model (final answer)
DEEPSEEK_GENERATION_MODEL=deepseek-v4-flash

# Embedding model
SILICONFLOW_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B
```

**Notes:**

- To use OpenAI instead, set `LLM_PROVIDER=openai` / `EMBEDDING_PROVIDER=openai`
  and provide `OPENAI_FAST_MODEL`, `OPENAI_GENERATION_MODEL`,
  `OPENAI_EMBEDDING_MODEL` (plus `OPENAI_API_KEY`).
- **Embedding dimensions must match the database column.** The `embedding
  halfvec(N)` column is sized from `EMBEDDING_DIM` (`.env`, default `2560` for
  `Qwen/Qwen3-Embedding-4B`). Switching to a model with a different dimension
  requires a schema change and a full re-embed — see the "Changing the Embedding
  Model" runbook in `indexer/README.en.md`.

**Retrieval Settings**:

- `match_count`: Default 30 (`RETRIEVAL_MATCH_COUNT`), increase for better recall
- `match_threshold`: Default 0.4 (`MATCH_THRESHOLD`), increase for higher precision (but fewer results)
- `top_k`: Default 15 (`RETRIEVAL_TOP_K`), maximum unique chunks sent to grading
- Query refinement is currently fixed to one retry in `create_initial_state()`;
  it is not configured through an environment variable

## Development

### Project Structure

```plaintext
agent/src/
├── main.py            # FastAPI server
├── chat.py            # Endpoint handlers
├── agent.py           # Agentic RAG API
├── graph.py           # LangGraph workflow
├── retriever.py       # Semantic search with caching
├── cache.py           # LRU cache implementation
├── vector_db.py       # Database operations
├── models.py          # Pydantic models
├── settings.py        # Configuration
├── eval_agent.py      # Manual pipeline evaluation harness
├── eval_retriever.py  # Manual retrieval evaluation harness
└── eval_queries.py    # Evaluation query categories and examples
```

### Network Access

The agent service runs on **internal Docker network only** (not exposed to host):

```bash
# Access from host (via docker exec)
docker compose exec agent curl http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "test"}'

# Access from another container (via service name)
curl http://agent:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "test"}'
```

The web frontend is the only service published to the host — plain HTTP on port
8080, no TLS (public exposure and TLS termination are handled outside this
repo). It proxies `/chat` to the agent over the internal Docker network; the
agent itself is never exposed to the host.

### Debugging

```bash
# View logs
docker compose logs -f agent

# Access container
docker compose exec agent sh

# Test database connection (helper lives in the indexer service)
docker compose exec indexer python -m src.debugger test

# Test single query
docker compose exec agent python -m src.eval_agent --limit 1
```

## Where citation titles come from

In its raw output the model writes a transcript **URI** — e.g.
`[[ShuiQianXiaoXi/0501-0600/0588.md]]` — and neither a URL nor a Chinese episode
name. The URI is a string already present in its context, so there is no episode
number or link for it to invent. After generation, `_repair_citations` rewrites
every citation into `[[standardised title]](url)`.

Titles come from `rag.documents.title`, LEFT JOINed at retrieval time (written by
the indexer from the upstream `URI映射.md`). When that row is missing, a general
rule is used instead:

| Directory          | Title prefix |
| ------------------ | ------------ |
| `ShuiQianXiaoXi/`  | 睡前消息     |
| `CanKaoXinXi/`     | 参考信息     |
| `GaoJian/`         | 高见         |
| `JiangDianHeiHua/` | 讲点黑话     |
| `ChanJingPoBiJi/`  | 产经破壁机   |

The 29 documented exceptions (the `misc/` specials and friends) cannot be derived
at all; those fall back to showing the URI itself — an ugly label on a link that
still works.

Citation links point at the transcript site:
`https://bedtimenewsstudio.github.io/BedtimeNews-Transcripts/contents/<URI without .md>.html`
