# BedtimeNews Knowledge Base

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

The BedtimeNews knowledge base website: ask the agent questions and browse or
read the full transcript archive in the same place. Q&A is powered by an
agentic RAG (Retrieval-Augmented Generation) system — automatic routing,
semantic search, retrieved-transcript context, and episode citations.

> **Try it out:** [bedtime.blog](https://bedtime.blog)

## Overview

The transcript texts and the intelligent system live in two separate repos: the
source transcripts are maintained in
[BedtimeNews-Transcripts](https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts),
while this repo (BedtimeNews-Agent) indexes them and runs a website that serves
both LLM-powered Q&A and in-app transcript reading — the indexed transcripts are
served as in-app content, and citations in answers jump straight to the in-app
reader. Built with LangGraph, any OpenAI-compatible chat/embedding endpoints (the default template uses DeepSeek for chat and SiliconFlow's Qwen3 embeddings), and PostgreSQL + pgvector.

**Key Features:**

- Automatic query routing (archive retrieval vs constrained direct handling)
- Query optimization and semantic search
- LLM-based document grading
- Retrieved transcripts supplied as answer context, with markdown citations and
  citation repair
- Automated document indexing with incremental updates
- Web interface: chat Q&A plus in-app transcript browsing/reading (citations
  jump straight to the in-app reader)

## Architecture

![BedtimeNews system architecture](docs/diagrams/system-architecture.svg)

**Components:**

- **[Frontend](frontend/README.en.md)**: Custom chat + transcript-reading UI
  (static HTML/CSS/JS served by a small FastAPI app; hosts the in-app
  transcript list and reader, with content served from the index database)
- **[Agent](agent/README.en.md)**: LangGraph-based agentic RAG service
- **[Indexer](indexer/README.en.md)**: Automated document embedding pipeline
- **Database**: PostgreSQL with pgvector extension as vector database

Indexing scope: only each transcript's `## 正文` (body) section is indexed; the
`## 附录` (appendix — fact corrections and verification notes) and the
`**发布日期**` metadata line are excluded from retrieval.

The stack serves plain HTTP on port 8080 — no TLS. Public exposure and TLS
termination are handled outside this repo.

## Quick Start

### Prerequisites

- Docker
- API keys for the generation and embedding endpoints — filled into
  `config.yml` (any OpenAI-compatible vendor; the default template uses
  DeepSeek for chat and SiliconFlow Qwen3 embeddings as the example)

### Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/BedtimeNewsStudio/BedtimeNews-Agent.git
   cd BedtimeNews-Agent
   ```

2. **Configure environment**

   Copy [`config.example.yml`](config.example.yml) to `config.yml` and configure:

   ```bash
   cp config.example.yml config.yml
   # Edit config.yml — fill in the generation / embedding endpoint groups
   # (api_key + base_url + model; any OpenAI-compatible vendor)
   ```

   Also copy `.env.example` to `.env` (deployment wiring: ports, image tag,
   postgres credentials, `EMBEDDING_DIM` — only these still ride on
   environment variables):

   ```bash
   cp .env.example .env
   ```

   > **Precedence: real environment variables > `config.yml`** (nested keys use
   > double underscores, e.g. `GENERATION__API_KEY`). Keys and app config live
   > in `config.yml`; there is no need to export keys anymore.

3. **Start services**

   ```bash
   docker compose up -d
   ```

4. **Access the UI**

   Open `http://localhost:8080` (plain HTTP; change the host port with
   `FRONTEND_PORT` in `.env`).

   This runs the published images. If you have edited the code, add `--build` —
   see [Published image vs. your checkout](#published-image-vs-your-checkout).

### Verify Installation

```bash
# Check service status
docker compose ps

# View logs
docker compose logs -f
```

### Tests and Coverage

The root test command runs agent, indexer, and frontend in isolated processes:

```bash
uv run pytest
uv run pytest --cov
```

Options are forwarded to every component. To run only one component, invoke it
from that directory:

```bash
cd agent  # or indexer / frontend
uv run pytest --cov
```

## Releases

Tagged releases publish prebuilt multi-arch (amd64 + arm64) images to GHCR via
[`release.yml`](.github/workflows/release.yml):

- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-agent`
- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-indexer`
- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-frontend`

To deploy a published release, pin a version with `IMAGE_TAG` in `.env` (default
`latest`) and pull:

```bash
# in .env: IMAGE_TAG=0.1.0
docker compose pull
docker compose up -d
```

### Published image vs. your checkout

`docker compose up` **never builds on its own**, even from a source checkout with
local edits. The `image:` key decides what runs:

| Situation                            | What `docker compose up` does           |
| ------------------------------------ | --------------------------------------- |
| Tagged image already present locally | Reuses it — no pull, no build           |
| Tagged image not present locally     | **Pulls** the published image from GHCR |
| `docker compose up --build`          | Builds from the checkout                |

So after editing code, rebuild explicitly or you will keep running the old image:

```bash
docker compose up -d --build agent web
```

Note that a locally built image and a published release share the same tag, so
whichever was created last wins. `docker compose pull` overwrites a local build,
and `--build` overwrites a pulled release.

To cut a release, push a `v*` tag (image tags drop the leading `v`):

```bash
git tag v0.1.0 && git push origin v0.1.0
```

> Release notes should call out operational changes: new/renamed env vars,
> schema changes (e.g. `EMBEDDING_DIM` — see the runbook in
> [indexer/README.en.md](indexer/README.en.md)), and whether re-indexing is required.
> `storage/postgres/init.sh` only runs on a fresh data volume, so schema changes
> never apply automatically to existing deployments.

### Body-hash schema upgrade

The indexer now invalidates vectors from the SHA-256 of the exact normalized `## 正文` text, while retaining a separate whole-source hash. Existing volumes must apply `storage/postgres/migrations/001_body_hashes.sql` before running the new indexer; the production runbook is in [indexer/README.en.md](indexer/README.en.md). The deterministic local subset is available through `docker-compose.sample.yml` and requires isolated `POSTGRES_DATA_DIR` / `INDEXER_DATA_DIR` paths.

## Service-Specific Documentation

- **[Frontend](frontend/README.en.md)**: UI customization
- **[Agent](agent/README.en.md)**: API endpoints, Agentic RAG implementation
- **[Indexer](indexer/README.en.md)**: Document processing

## Data Persistence

Data is persisted across restarts:

- **PostgreSQL data** (chunks + embeddings): bind-mounted to `./storage/postgres/volume`
- **Service logs**: Docker named volumes `bedtimenews_indexer_logs` and `bedtimenews_agent_logs`

## Project Structure

```plaintext
BedtimeNews-Agent/
├── agent/              # LangGraph agentic RAG service
│   ├── src/
│   ├── Dockerfile
│   ├── README.md
│   ├── README.en.md
│   └── README.es-ES.md
├── frontend/           # Custom web UI (static + FastAPI)
│   ├── server.py       # FastAPI: serves static UI + proxies /chat SSE and transcript APIs
│   ├── starters.py     # Sample questions data
│   ├── static/         # index.html, styles.css, app.js, logo
│   ├── Dockerfile
│   ├── README.md
│   ├── README.en.md
│   └── README.es-ES.md
├── indexer/            # Document embedding pipeline
│   ├── src/
│   ├── Dockerfile
│   ├── README.md
│   ├── README.en.md
│   └── README.es-ES.md
├── docs/diagrams/      # SVG architecture and workflow diagrams
├── storage/            # Database initialization scripts
│   └── postgres/
├── docker-compose.yml  # Service orchestration
├── config.yml          # App + secrets config (not in git, copied from the example)
├── config.example.yml  # App config template
├── .env                # Deployment wiring (not in git, copied from .env.example)
├── .env.example        # Deployment wiring template
├── THIRD_PARTY_NOTICES.md  # Third-party component licenses
├── README.md           # Project README (中文, default)
├── README.en.md        # English README (this file)
└── README.es-ES.md     # Spanish README
```

## License

MIT License — see [LICENSE](LICENSE) file.

This project bundles third-party components under their own licenses — see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details.
