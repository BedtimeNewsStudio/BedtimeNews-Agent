# Frontend Service

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

Custom web frontend for the BedtimeNews knowledge base: a static single-page
app (HTML/CSS/JS) serving both the chat agent and the in-app transcript
archive. A small FastAPI app hosts it and proxies the chat stream and
transcript APIs to the internal agent backend; the transcript list/reader live
same-origin under `/transcripts`.

See the [main README](../README.en.md) for full-stack setup.

## Design

- **Theme:** a flat chatbot palette (ChatGPT/Claude-like defaults) — dark
  `#212121` / light `#ffffff`, one green `--accent`, no page gradients and no
  dual decorative accents. Light/dark follow `prefers-color-scheme` by default;
  the masthead SVG sun/moon toggle writes a `sessionStorage` override (survives
  reload, resets on a new tab).
- **Color tokens** are semantic (`--bg`, `--surface`, `--line`, `--text`,
  `--text-dim`, `--muted`, `--accent`, `--user-bubble`, …), defined for dark in
  `:root` and overridden under `[data-theme="light"]`.
- **Type:** system CJK sans; monospace only for a few machine labels. No webfont
  CDN.
- **Layout (automatic by viewport; no manual desktop/mobile switch):**
  - **Desktop (>900px):** chat-centred landing; opening a transcript squeezes in
    a right-hand reading pane. Masthead shows channel chips, framed GitHub +
    theme controls. Starters: one question per category plus a「浏览文稿」
    control.
  - **Mobile (≤900px):** full-screen chat **or** archive/reader; bottom tabs
    「对话 | 文稿」split the bar in half. Starters are eight flat questions (no
    category labels, no browse button). Masthead icons are borderless; the
    GitHub Edit control is desktop-only.
- **Reading chrome:** fixed-height slots (back / edit / close) so list↔article
  borders do not jump; in-article heading/footnote rule lines are suppressed.
- **Signal-acquisition log:** RAG stages (condense → … → generate) render live,
  then lock and collapse when the answer starts.

## Features

- Anonymous chat (no authentication)
- System-aware light/dark theme with SVG toggle
- Sample questions (categorised on desktop, flat on mobile) and desktop
  「浏览文稿」
- Channel chips, newest-first archive lists (undated last), article reader
- Desktop GitHub Edit link into
  `BedtimeNews-Transcripts/edit/main/contents/…`
- Real-time SSE streaming with visible pipeline steps
- Markdown answers via vendored markdown-it (`html:false`) and in-app citation
  navigation
- Ephemeral, in-page conversation (cleared on refresh)
- Keyboard-accessible; respects `prefers-reduced-motion`

## Architecture

![Frontend request architecture](../docs/diagrams/frontend-architecture.svg)

The frontend:

- Runs in a Docker container that serves plain HTTP on port 8080 (no TLS —
  public exposure and TLS termination are handled outside this repo)
- Is the only service published to the host (`FRONTEND_PORT`, default 8080)
- Proxies `/chat` and transcript APIs to the agent over the internal Docker
  network; the agent is never exposed to the host

## Components

- **server.py** — FastAPI: `static/`, `/api/starters`, transcript API proxy,
  `/chat` SSE proxy, SPA routes for `/transcripts`
- **starters.py** — sample-question data (categories + questions)
- **static/index.html** — markup, theme boot, mobile tab bar, turn templates
- **static/styles.css** — solid tokens and desktop/mobile layout
- **static/app.js** — routing, archive/reader, starters, composer, theme, SSE,
  Markdown
- **static/markdown-it.min.js** — vendored Markdown renderer (MIT), on demand
- **static/bedtimenews.webp** — favicon / brand logo
- **pyproject.toml** — `fastapi`, `uvicorn`, `httpx`

## Endpoints

| Method | Path                                  | Purpose                              |
| ------ | ------------------------------------- | ------------------------------------ |
| GET    | `/`                                   | SPA (`static/index.html`)            |
| GET    | `/transcripts`, `/transcripts/{path}` | Same SPA (client routes)             |
| GET    | `/api/starters`                       | Sample questions JSON (`categories`) |
| GET    | `/api/transcripts`                    | Transcript index (proxied)           |
| GET    | `/api/transcripts/{doc_id}`           | One transcript (proxied)             |
| POST   | `/chat`                               | Proxies the agent SSE stream         |
| GET    | `/healthz`                            | Liveness check                       |
| GET    | `/index.html`                         | `308` redirect to `/`                |
| GET    | `/robots.txt`                         | Allows all; points at the sitemap    |
| GET    | `/sitemap.xml`                        | Home, channel lists, every transcript |
| GET    | `/s/{short_id}`                       | Short link, `302` to the transcript  |

## Development Workflow

The container runs `uvicorn server:app`. After changing Python or static files,
rebuild and restart:

```bash
# The frontend is published on the host (FRONTEND_PORT, default 8080)
docker compose build web
docker compose up -d web
open http://localhost:8080
```

> Use `--no-cache` if a rebuild appears to serve stale code.

### Run without Docker

```bash
cd frontend
pip install .
# Point at a reachable agent backend:
AGENT_BACKEND_HOST=localhost AGENT_BACKEND_PORT=8000 \
  uvicorn server:app --reload --port 8080
```

### Customization

- **Starter questions / categories:** edit `starters.py` (`CATEGORIES`).
- **Styling:** edit `static/styles.css` (design tokens live in `:root`).
- **Copy / layout:** edit `static/index.html`.
- **Logo / favicon:** replace `static/bedtimenews.webp`. It renders at about
  1.85rem, so keep it small — 128px square is enough for hi-DPI, and the file is
  cached for a week by `CachedStaticFiles`.

## Configuration

| Variable             | Default | Purpose                                  |
| -------------------- | ------- | ---------------------------------------- |
| `AGENT_BACKEND_HOST` | `agent` | Agent service name on the Docker network |
| `AGENT_BACKEND_PORT` | `8000`  | Agent port                               |
| `FRONTEND_PORT`      | `8080`  | Host port the frontend is published on   |
| `APP_VERSION`        | (empty) | Masthead version; compose sets it from `IMAGE_TAG` (`latest`/empty falls back to the package version) |
| `PUBLIC_BASE_URL`    | `https://bedtime.blog` | Origin for canonical, sitemap and robots URLs |

## Debugging

```bash
# Logs
docker compose logs -f web

# Backend connectivity from inside the container (the slim image has no
# ping/curl; use the bundled Python + httpx instead)
docker compose exec web python -c "import httpx; print(httpx.post(
    'http://agent:8000/chat', json={'question': '测试'}, timeout=120).text)"
```

## API Contract

The frontend proxies the agent's `/chat` endpoint.

### Request

```json
{
  "question": "string (required)",
  "history": [{"question": "…", "answer": "…", "grounded": true}],
  "stream": true
}
```

`history` is optional; the browser sends at most its three most recent turns.

### Streaming response (SSE)

```json
{"type": "step", "step": "condense|route|rewrite|retrieve|grade|generate", "content": "…"}
{"type": "citations", "urls": {"ShuiQianXiaoXi/0501-0600/0588.md": {"title": "睡前消息588", "url": "/transcripts/ShuiQianXiaoXi/0501-0600/0588.md"}}}
{"type": "answer_chunk", "content": "…"}
{"type": "answer_final", "content": "…", "grounded": true}
{"type": "answer_meta", "grounded": true}
{"type": "followups", "items": ["…"]}
{"type": "error", "content": "…"}
```

The server may emit `: ping` SSE comments between events and terminates every
stream with `data: [DONE]`. A successful turn sends exactly one of
`answer_final` or `answer_meta`. Citation URLs are in-app `/transcripts/…`
paths (not GitHub Pages).

## Limitations (MVP)

- **No authentication** — anonymous only
- **No persistence** — conversation is cleared on refresh
- **Per-tab session** — no cross-tab or server-side history

## Troubleshooting

**Port 8080 in use:** set `FRONTEND_PORT` in `.env` to another host port and
recreate the service (`docker compose up -d web`).

**Cannot connect to backend:**

- `docker compose ps agent` and `docker compose logs agent`
- Connectivity check from inside the container (see [Debugging](#debugging))

**Changes not appearing:** rebuild (`--no-cache`) and hard-refresh the browser
(Cmd/Ctrl+Shift+R).
