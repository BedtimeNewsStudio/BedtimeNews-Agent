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
- `retriever.py`: Semantic search (embeddings + pgvector) against one snapshot;
  result cache keys include the snapshot ID, so a switch never serves stale results
- `cache.py`: LRU cache for query results
- `chat.py`: FastAPI endpoint handlers
- `main.py`: FastAPI server (`/chat`, `/transcripts`, `/health`)
- `snapshots.py`: Which RAG snapshot to read: selection rule, 15-second
  registry poll, readiness
- `vector_db.py`: Read-only PostgreSQL + pgvector queries; every query takes the
  snapshot schema explicitly (no `search_path`)

### RAG snapshots

The indexer publishes the knowledge base as immutable snapshots (`rag_s<id>`
schemas) registered in `rag_meta.snapshots`; see the
[indexer README](../indexer/README.en.md#database-schema). The agent:

- **selects** the published snapshot whose `format_version` it supports
  (`SUPPORTED_FORMATS`, currently `{1}`) and whose vector space
  (`<embedding.model>@<EMBEDDING_DIM>`, or `embedding.space_id`) equals its
  own — highest format first, then the most recently published. `RAG_SNAPSHOT`
  pins one snapshot ID instead. Before the indexer has created `rag_meta`, the
  original `rag` schema is read as the implicit snapshot `legacy`;
- keeps the current snapshot **in memory** and re-applies the rule every 15
  seconds in a background thread, so a newly published or rolled-back snapshot
  takes effect without a restart. Request handling never queries `rag_meta`;
- **pins each request** to the snapshot current when it starts: every
  retrieval of one `/chat` answer, and each reader response, reads one
  snapshot even if a new one is published meanwhile;
- stays up when the database or a readable snapshot is unavailable: it is then
  not ready (`/health` returns `503`), `/chat` and `/transcripts` return `503`,
  and the poller keeps retrying;
- connects as the read-only role `rag_agent` when `POSTGRES_AGENT_PASSWORD` is
  set (it cannot modify any data even under prompt injection); otherwise it
  falls back to the database superuser and logs a warning.

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
  "answer": "根据[[睡前消息588]](/transcripts/ShuiQianXiaoXi/0501-0600/0588.md)...",
  "followups": ["独山县后来如何化解债务？"],
  "grounded": true
}
```

**Response (Streaming):**

```plaintext
data: {"type": "step", "step": "route", "content": "..."}
data: {"type": "citations", "urls": {"ShuiQianXiaoXi/0501-0600/0588.md": {"title": "睡前消息588", "url": "/transcripts/ShuiQianXiaoXi/0501-0600/0588.md"}}}
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

Without a readable snapshot, the non-streaming response is `503`; a streaming
response carries an `error` event (`知识库暂时不可用，请稍后重试。`). Any other
failure is a `500` (`Chat processing failed`) or an `error` event
(`回答生成失败，请稍后重试。`): the exception text is logged, never returned.

**Time limit.** One `/chat` may run for at most 240 seconds
(`CHAT_TIME_LIMIT_S` in `src/chat.py`). A stream that reaches it ends with an
`error` event (`回答超时，请缩小问题范围后重试。`) followed by `[DONE]`; a
non-streaming request gets `504`. The limit is the innermost of the shutdown
layers (240 s limit < 270 s uvicorn graceful shutdown < 300 s compose
`stop_grace_period` < 5-minute proxy drain), so stopping or replacing the
agent never cuts an answer short; see
[the blue-green design](../docs/designs/20261008_blue-green-deployment.md).

### GET /transcripts

Reader navigation index from the current snapshot's `transcripts` table:
`{"items": [{doc_id, canonical_title, source_title, channel, publication_date, source_hash, updated_at}, ...]}`,
ordered by channel, then newest `publication_date` first. Responses carry an
`ETag` (`Cache-Control: no-cache`) and answer `If-None-Match` with `304`. `503`
when the database or a readable snapshot is unavailable.

### GET /transcripts/{doc_id}

One rendered transcript by its exact URI (e.g.
`/transcripts/ShuiQianXiaoXi/0501-0600/0588.md`): the same fields plus
`body_html`. Only canonical relative `.md` URIs are accepted; anything else,
or an unknown URI, returns `404`. Same `ETag`/`304`/`503` behaviour.

### GET /health

Readiness probe. `200` when the database is reachable and a readable snapshot
is selected; otherwise `503` with a `reason` (database unreachable, no
published snapshot in this agent's vector space, pinned snapshot retired or
missing, ...). Served from the snapshot poller's last result (at most 15 s
old): it does not query the database or call a model.

```json
{
  "ready": true,
  "snapshot": {
    "id": "s20261007t091512z_a1b2c3d",
    "schema": "rag_s20261007t091512z_a1b2c3d",
    "format_version": 1,
    "embedding_space": "Qwen/Qwen3-Embedding-4B@2560",
    "published_at": "2026-10-07T09:15:40Z",
    "data_age_seconds": 3600,
    "source_commit": "a1b2c3d..."
  },
  "embedding_space": "Qwen/Qwen3-Embedding-4B@2560",
  "supported_formats": [1],
  "pinned_by_config": null,
  "checked_at": "2026-10-07T10:15:38Z",
  "indexer_status": {
    "last_run_at": "...",
    "last_result": "no_change",
    "last_error": null,
    "last_published_at": "...",
    "consecutive_failures": 0
  }
}
```

`indexer_status` does not affect readiness (an older snapshot still serves); use
it for alerting, e.g. 3 consecutive failures or no run for more than 3 hours.
The compose file uses `/health` as the agent's container healthcheck.

The full body stays internal (the agent is not published). `web` exposes a
trimmed form publicly as `GET /readyz` — the same status code, but only
`ready`, `reason` and the snapshot's `id`, `format_version` and
`embedding_space`, never `indexer_status` (its `last_error` can contain
internal error text).

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

Chat and embeddings are each one "OpenAI-compatible endpoint + model + key"
group, configured under `generation` / `embedding` in `config.yml` (vendor
agnostic — DeepSeek, SiliconFlow, OpenAI or a self-hosted gateway differ only
in three lines):

```yaml
generation:
  api_key: "..."
  base_url: "https://api.deepseek.com"
  model: "deepseek-v4-flash"        # generation model (final answer)
  fast_model: "deepseek-v4-flash"   # fast model (condense, routing, query rewrite, grading)

embedding:
  api_key: "..."
  base_url: "https://api.siliconflow.com/v1"
  model: "Qwen/Qwen3-Embedding-4B"
```

**Notes:**

- Switching vendors (OpenAI, a self-hosted gateway, ...) only changes the
  `base_url` / `model` / `api_key` values — no code changes needed.
- **The vector space must match the snapshot.** Query vectors must come from
  the model the snapshot was built with, so the agent only reads snapshots in
  its own vector space: `<embedding.model>@<EMBEDDING_DIM>` (`.env`, default
  `2560` for `Qwen/Qwen3-Embedding-4B`), or `embedding.space_id` when set.
  Changing the model means the indexer builds a new snapshot first — see the
  "Changing the Embedding Model" runbook in `indexer/README.en.md`.

**Database and snapshot settings** (environment, set by `compose.app.yml`
from `.env`):

- `POSTGRES_AGENT_PASSWORD`: connect as the read-only `rag_agent` role
  (recommended). Unset → fall back to `POSTGRES_USER` with a warning
- `EMBEDDING_DIM`: dimension of the query vectors (part of the vector space)
- `RAG_SNAPSHOT`: optional snapshot ID to pin (troubleshooting, long-term
  pinning); the agent then does not follow new snapshots, and becomes not ready
  if that snapshot is retired or deleted

**Retrieval Settings**:

- `match_count`: Default 30 (`retrieval_match_count` in `config.yml`), increase for better recall
- `match_threshold`: Default 0.4 (`match_threshold` in `config.yml`), increase for higher precision (but fewer results)
- `top_k`: Default 15 (`retrieval_top_k` in `config.yml`), maximum unique chunks sent to grading
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
├── retriever.py       # Semantic search with per-snapshot caching
├── cache.py           # LRU cache implementation
├── snapshots.py       # Snapshot selection, polling, readiness
├── vector_db.py       # Read-only, snapshot-qualified database queries
├── models.py          # Pydantic models
├── settings.py        # Configuration
├── uri_mapping.py     # Fallback citation title from a URI
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

The agent is on two networks: its application instance's private network,
shared only with that instance's `web` (where `agent` resolves to this agent
alone), and the data layer's network, to reach `postgres`. During a blue-green
deployment every instance's agent is on the data-layer network, so the alias
`agent` there resolves to all of them: **nothing on the data-layer network may
call `agent`** (postgres and the indexer never do). See `compose.app.yml`.

**Graceful shutdown.** On SIGTERM uvicorn (started with `exec`, so it receives
the signal as PID 1) stops accepting connections and lets in-flight requests,
including streams, finish for up to 270 s; compose's `stop_grace_period` is
300 s. A `docker compose stop` or in-place recreate therefore waits up to 5
minutes while a stream is open.

### Debugging

```bash
# View logs
docker compose logs -f agent

# Access container
docker compose exec agent sh

# Readiness and the snapshot being served. Container names follow the compose
# project (e.g. bedtimenews-app-green-agent-1), so address the service:
docker compose ps agent
docker compose exec agent curl -s http://localhost:8000/health

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

Titles come from the snapshot's `documents.title`, LEFT JOINed at retrieval time
(written by the indexer from the upstream `URI映射.md`). When that row is missing, a general
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
`/transcripts/<URI>` (in-app reader; source on GitHub at `blob/main/contents/<URI>`)
