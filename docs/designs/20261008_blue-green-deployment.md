# Application-Layer Blue-Green Deployment

Status: phases A and B implemented and released in v0.4.1; in production since 2026-10-08, when the one-time cutover of section 6 ran and v0.4.1 was deployed as the first blue-green upgrade. Phase C, the deployment tool, lives outside this repository and is not implemented yet; until it exists, an operator runs the steps of section 3 by hand. Section 11 records where the implementation differs, which tests of section 7 were run, and the production results.

Prerequisite: [RAG Snapshot Publishing](20261007_rag-snapshot-architecture.md) (implemented, in production since v0.4.0). In this document "snapshot", "vector space", "selection rule", "lineage" and the agent's `/health` have the meanings defined there (sections 5.2, 6.8, 7.1 and 7.4).

## 1. Purpose and Scope

Upgrade `agent` and `web` (a published release image, or a source build) without dropping requests and without cutting in-flight `/chat` streams:

1. The new version (**green**) starts on a different host port, next to the running old version (**blue**), sharing the same PostgreSQL;
2. wait until green is ready;
3. the edge proxy sends new requests to green, while requests already on blue finish on blue;
4. once blue is idle (or the drain times out), blue is stopped.

The project does not care which proxy or deployment tool performs steps 3 and 4. This document defines what the project provides (section 2) and what it requires from the proxy and the deployment tool (section 3).

**What the snapshot architecture already removed.** Served data is immutable snapshots, and every agent picks the snapshot it can read by the selection rule. Old and new versions therefore never share mutable tables, so this design does **not** need: a permanent "additive-only schema" discipline, migration tooling and version gates, maintenance labels on images, or a downtime window for changing the embedding dimension. The agent is read-only by database privilege (`rag_agent`, snapshot design 7.5), so two colors running at once cannot produce write conflicts.

Out of scope:

- `postgres` and `indexer` stay single-instance and are upgraded in place. The indexer is not user-facing: upgrading it only delays the next snapshot. A snapshot in a new format does not affect a running old agent, which keeps reading the newest snapshot of a format it supports.
- PostgreSQL major-version upgrades.
- The control tables `rag_meta.snapshots` and `rag_meta.indexer_status` are written only by the indexer but read by agents of both colors. They rarely change; any change must be additive (columns the agent reads are never dropped or given a new meaning). This is the only compatibility constraint left.

## 2. Contract: What the Project Provides to the Deployment Tool

### 2.1 Two layers, two kinds of compose project

| Layer       | Compose file         | Project name                                                                            | Services                            | Upgrade                            |
| ----------- | -------------------- | --------------------------------------------------------------------------------------- | ----------------------------------- | ---------------------------------- |
| Data        | `compose.data.yml`   | `bedtimenews-agent` (the existing production project name, unchanged)                   | `postgres`, `indexer`               | In place                           |
| Application | `compose.app.yml`    | One per instance, chosen by the deployment tool (e.g. `bedtimenews-app-blue`, `-green`) | `agent`, `web`                      | Blue-green                         |
| Umbrella    | `docker-compose.yml` | The directory name                                                                      | All four services, one app instance | Self-hosting and local development |

Colors are only labels: after a switch, green is the blue of the next upgrade. There is no fixed pair of projects.

### 2.2 Inputs (environment variables passed per instance when running `docker compose`)

| Variable                      | Meaning                                                                                                                                                                                                                                                                                   |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-p` / `COMPOSE_PROJECT_NAME` | Instance identity                                                                                                                                                                                                                                                                         |
| `APP_IMAGE_TAG`               | Image version shared by `agent` and `web`; source builds use a distinct tag such as `0.4.0-<sha>`. The two always run the same version                                                                                                                                                    |
| `APP_PORT`                    | Host port `web` publishes; allocated by the deployment tool and must be free                                                                                                                                                                                                              |
| `APP_INSTANCE`                | Instance label reported by `/healthz` (the project name is fine)                                                                                                                                                                                                                          |
| `BEDTIMENEWS_NET_NAME`        | Name of the data-layer network; in production `bedtimenews-agent_bedtimenews_net` (section 4.2)                                                                                                                                                                                           |
| `BEDTIMENEWS_NET_EXTERNAL`    | `true`: join the data-layer network created by the data-layer project instead of creating one (section 4.2)                                                                                                                                                                               |
| `RAG_SNAPSHOT` (optional)     | Pin this instance to one snapshot id; unset, it follows the newest readable snapshot (snapshot design 7.1). Pair a long-lived pin with `snapshots pin <id>`, or GC may eventually remove it                                                                                               |
| `EMBEDDING_DIM` (optional)    | The agent's query-vector dimension. Together with `embedding.model` (or `embedding.space_id`) in the config it decides which vector space this instance reads. Defaults to the shared `.env`                                                                                              |
| `APP_CONFIG_FILE` (optional)  | Host path of the `config.yml` mounted into this instance's `agent` (default `./config.yml`). For deliberately different per-instance settings, e.g. tests of two vector spaces side by side. Named so it does not clash with `CONFIG_YAML`, the in-container path the agent already reads |

The deployment tool refuses to run when `APP_IMAGE_TAG`, `APP_PORT`, `APP_INSTANCE`, `BEDTIMENEWS_NET_NAME` or `BEDTIMENEWS_NET_EXTERNAL=true` is missing, and always passes `-p` and `-f compose.app.yml` explicitly. The live project, port and version are not stored anywhere: the deployment tool derives them from the running system on every run (section 3.1). They are **never written to the shared `.env`**: written there, any later argument-less command would bring the old version back.

Self-hosters get defaults through interpolation fallbacks in `compose.app.yml`: `APP_IMAGE_TAG` falls back to `IMAGE_TAG` in `.env` (else `latest`), `APP_PORT` to `FRONTEND_PORT` (else 8080), `APP_INSTANCE` to `local`, and the data-layer network to the umbrella project's own. The README quick start (`docker compose up -d`) stays the same. In production those fallbacks never apply: the deployment tool's preflight makes the variables mandatory, and the `COMPOSE_FILE` guard (section 4.1) keeps argument-less commands away from the application layer.

### 2.3 Outputs (on `APP_PORT`)

| Endpoint       | Meaning                                                                                                                                                                                                                                                                                           |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /healthz` | Liveness, answered by `web` alone without calling the agent: `{"status":"ok","version":...,"instance":...}`                                                                                                                                                                                       |
| `GET /readyz`  | Readiness. Calls this instance's agent `/health` with a short timeout and returns its status code (200 when the database is reachable and a readable snapshot is selected, otherwise 503) with a trimmed body: `ready`, `reason`, and the snapshot's `id`, `format_version` and `embedding_space` |

The agent's `/health` already exists (snapshot design 7.4). It is served from the snapshot poller's last result (at most 15 s old) and never queries the database itself, so `/readyz` is cheap to poll. The first poll runs synchronously at agent startup, so a green instance in a vector space that has a published snapshot is ready as soon as it is up.

Both endpoints are reachable through the public proxy like every other path on `web`. That is why `/healthz` must not depend on the agent (liveness of `web` is not readiness of the stack), and why `/readyz` does not forward the agent's full body: it carries `indexer_status.last_error`, which can contain internal error text.

### 2.4 Guarantees

- Two application instances of any versions can use the same database at the same time, each reading the snapshot it can read.
- Each instance's `web` talks only to its own `agent` (section 4.2).
- On SIGTERM a process stops accepting connections and finishes in-flight requests, including streaming `/chat`, within the limits of section 4.3 (from phase A on, section 8).
- The application layer has no state to hand over to the other color. Conversation history travels with every `/chat` request (`ChatRequest.history`), so a user's next turn may land on either color. The retrieval result cache is keyed by snapshot id (snapshot design 7.3), the query-embedding cache is valid within a vector space, and transcript short links are derived deterministically from the `doc_id` (`frontend/server.py`), with no lookup table. Green starts with cold caches, which only costs its first queries some latency.
- Two colors fit the database's connection budget: each agent's pool holds at most 20 connections, so two agents plus the indexer stay well below PostgreSQL's default `max_connections` of 100.

## 3. Requirements on the Edge Proxy and Deployment Tool (Outside This Repository)

The proxy must be able to:

1. switch the public route atomically from one upstream (`host:APP_PORT`) to another without restarting;
2. let requests already on the old upstream, including long streaming responses, run to completion;
3. report in-flight requests per upstream (the preferred drain signal), or accept a time-based drain.

The proxy must also put the client's real address in `X-Forwarded-For`, and backend ports must not be reachable from the internet. The production Caddy meets all of this: `caddy reload` sends new requests to the new upstream at once, in-flight streams are not cut, and the admin endpoint `/reverse_proxy/upstreams` lists the old upstream with its `num_requests` until the last request finishes.

Deployment tool steps (after the one-time cutover in section 6):

```text
0. Preflight  take the deployment lock (one run at a time) and hold it until the end;
              derive blue (section 3.1) and refuse if it cannot be determined;
              APP_IMAGE_TAG / APP_PORT / APP_INSTANCE / BEDTIMENEWS_NET_* set; enough disk and memory for one more app instance.
              If the new version needs a snapshot in a new format or vector space: finish the indexer step of section 5 first.
1. Allocate   pick a free port P_green, and the project name blue does not have
              (bedtimenews-app-blue <-> bedtimenews-app-green)
2. Start      APP_IMAGE_TAG=<new> APP_PORT=P_green APP_INSTANCE=bedtimenews-app-green \
              BEDTIMENEWS_NET_NAME=bedtimenews-agent_bedtimenews_net BEDTIMENEWS_NET_EXTERNAL=true \
              docker compose -p bedtimenews-app-green -f compose.app.yml up -d [--build]
              (for a source build, --build must be in this step, or compose tries to pull a tag that does not exist)
3. Ready      poll http://127.0.0.1:P_green/readyz every 2 s until 3 consecutive 200s, for at most 2 minutes;
              on timeout: down -v green and stop (blue was never touched)
4. Verify     /healthz reports APP_IMAGE_TAG; /readyz reports the expected snapshot: for a release in blue's format
              and vector space, blue's snapshot or a newer one (the indexer may publish in between); otherwise the
              newest snapshot of the new format or space
5. Switch     proxy upstream -> P_green
6. Drain      wait until the proxy reports 0 in-flight requests on P_blue, or 5 minutes have passed
7. Retire     docker compose -p bedtimenews-app-blue -f compose.app.yml down -v;
              then prune images per section 3.2
8. Record     nothing extra: the proxy configuration written in step 5 is the record
              (commit it where the proxy configuration is version-controlled, as in production)
Rollback      before step 7: switch the proxy back to P_blue, drain P_green the same way, then down -v green
```

If green cannot reach the data it needs (the indexer step of section 5 was skipped, or the network variables are wrong), its agent never becomes ready and step 3 aborts without touching blue.

### 3.1 Live state is derived, not stored

The deployment tool keeps no state file. At the start of every run it derives the live instance from the system itself:

1. **Live port**: the application upstream in the proxy configuration. That is what actually carries traffic, so nothing is more authoritative.
2. **Live project**: the compose project (`com.docker.compose.project` label) of the container publishing that port. Application projects are recognized by the name prefix `bedtimenews-app-`.
3. **Live version and snapshot**: `GET /healthz` and `GET /readyz` on that port, i.e. what is actually running rather than what should be running.

The tool refuses to run when the derivation is inconsistent:

- no container publishes the proxied port, or `/healthz` does not answer;
- the port belongs to a project without the `bedtimenews-app-` prefix, e.g. the data-layer project (only possible before the cutover in section 6, which is done by hand). This is what keeps step 7 from ever running `down -v` on the data layer;
- an application project other than the live one exists. That is a leftover of an interrupted run, and possibly a blue that is still draining; an operator checks and removes it by hand.

Because the state is read from the system each time, it cannot go stale after a manual switch-back, a failed deploy or a host reboot, and the derivation doubles as a consistency check before anything is changed.

`down -v` is the complete retirement: compose stops `web` before `agent` (`web` depends on `agent`), so blue's `web` finishes its streams while its agent is still running; each gets SIGTERM and up to `stop_grace_period`, then the project is removed. `-v` removes only that instance's log volume, and `down` removes only the project's private network; the data-layer network is external to the application project and stays. **Never** run `down -v` on the data-layer project.

### 3.2 Image retention

Retirement removes containers, the private network and the log volume, but not images: every upgrade leaves the previous `agent` and `web` images on the host, every indexer upgrade the previous indexer image, and every source build its build cache (about 0.65 GB of image per application upgrade and 0.45 GB per indexer upgrade, less after shared layers). Nothing else removes them, so the deployment tool prunes at the end of step 7:

- **Images**: among the repository's own images (`ghcr.io/bedtimenewsstudio/bedtimenews-agent-*`), keep the tag each running container uses and the one deployed before it, separately for the application (`agent` + `web`, one shared tag) and the indexer, and remove every other tag. Keeping the previous tag lets a rollback after step 7 start without a pull or a rebuild.
- **Build cache**: after a source build, remove build cache unused for a week (`docker builder prune --filter until=168h`); recent cache keeps the next build fast.
- **Scope**: never a host-wide prune (`docker system prune`, `docker image prune -a`). The host may run other services whose unused images or volumes are kept on purpose.

"Deployed before" is derived like the live state (section 3.1), not stored: it is the newest remaining tag of that image other than the running one, by image creation time.

## 4. Changes in This Repository

### 4.1 Split the compose file

```text
compose.data.yml     postgres, indexer
compose.app.yml      agent, web
docker-compose.yml   include: [compose.data.yml, compose.app.yml]
```

- The layout needs Docker Compose 2.24.6 or later (originally planned as 2.20, the release that introduced `include`; see section 11); the README states the minimum.
- `postgres` and `indexer` keep their `container_name` (`bedtimenews-postgres`, `bedtimenews-indexer`); `agent` and `web` drop it, so container names follow the project (e.g. `bedtimenews-app-green-web-1`).
- `agent` drops `depends_on: postgres`: a service cannot depend on one in another project. Nothing is lost, because the agent starts without a database, reports not ready and reconnects (snapshot design 7.4). `web` keeps `depends_on: agent`, which also orders the shutdown (section 3.1).
- `web` gets a compose healthcheck on `/healthz`. Its image has no `curl`, so the check uses Python's `urllib`.
- The data-layer service definitions move over unchanged, including everything added for the snapshot architecture: postgres's `shm_size: 1g` (HNSW builds need ~265 MB of `/dev/shm`), the indexer's read-only `/pgdata` mount, `stop_grace_period: 30s`, and `POSTGRES_AGENT_PASSWORD` / `EMBEDDING_DIM`. There is one deliberate exception, below.
- **postgres replaces `env_file: .env` with an explicit `environment:`** listing only what it uses (`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST_AUTH_METHOD`), interpolated from `.env`. With `env_file`, every variable in `.env` becomes part of postgres's container configuration, so _any_ `.env` edit (an `IMAGE_TAG` bump, the `COMPOSE_FILE` guard below) recreates postgres on the next `up` that includes it, a few seconds without a database for both colors. With explicit entries, only a change to postgres's own variables does. The change itself recreates postgres once (section 6).
- The data-layer project name stays `bedtimenews-agent` and the network key stays `bedtimenews_net`, so its network is still the existing `bedtimenews-agent_bedtimenews_net` and **postgres is not recreated because of a network change** on the first deploy of the split files.
- `IMAGE_TAG` in `.env` keeps pinning the indexer image (data layer); `APP_IMAGE_TAG` pins the application. `web`'s `APP_VERSION` comes from `APP_IMAGE_TAG`.
- The agent log volume is per project, so two processes never write the same file.
- `docker-compose.sample.yml` stays an override of the umbrella file, with its own `POSTGRES_DATA_DIR` and `INDEXER_DATA_DIR`.
- **Production guard**: the VM's `.env` sets `COMPOSE_FILE=compose.data.yml`. A `docker compose ...` without `-f` on the VM then only acts on the data layer, instead of starting a default-version `agent` and `web` inside the data-layer project (the VM directory `BedtimeNews-Agent`, lower-cased, equals the data-layer project name). Commands with `-f`, and self-hosters, are unaffected.

### 4.2 Networks: each `web` reaches only its own `agent`

| Network                                                              | Created by         | Members                                         |
| -------------------------------------------------------------------- | ------------------ | ----------------------------------------------- |
| Data-layer network (production: `bedtimenews-agent_bedtimenews_net`) | Data-layer project | `postgres`, `indexer`, every instance's `agent` |
| The application project's default network                            | That app project   | That instance's `agent` and `web`               |

- `web` is only on the private network, where `AGENT_BACKEND_HOST=agent` resolves to this instance's agent alone.
- `agent` is on both networks and reaches `POSTGRES_HOST=postgres` through the data-layer network. Membership is declared in the compose service definitions, so a recreated postgres rejoins automatically, with no manual attach step.
- Compose registers the alias `agent` for every color's agent on the data-layer network. With two colors running the alias resolves to both, but nothing uses it. Rule: **no service on the data-layer network may call `agent`**; postgres and the indexer never do.
- Both files declare the data-layer network under the **same key**, `bedtimenews_net`, with an **identical definition**: `driver: bridge`, `name: ${BEDTIMENEWS_NET_NAME:-${COMPOSE_PROJECT_NAME}_bedtimenews_net}` and `external: ${BEDTIMENEWS_NET_EXTERNAL:-false}`. Compose only accepts a resource declared by two included files when the definitions are identical (a differing one fails with "conflicts with imported resource"). `driver: bridge` must stay, because Compose labels the network with a hash of its configuration and today's production network was created from a bare `driver: bridge`; `name` and `external` do not enter that hash, so the production network is untouched. In the umbrella, both declarations resolve to the same project network, created once; in two-project mode the application project joins the existing network as external, and the data layer never sets `BEDTIMENEWS_NET_*`.
- Two alternatives fail and must not be used. A second key that resolves to the same network name makes the second `docker compose up -d` of the umbrella try to recreate the network, which fails while containers are attached. Declaring the network as always external in `compose.app.yml` breaks a fresh umbrella install, because Compose checks that external networks exist before it creates the project's own.

### 4.3 Graceful shutdown and the chat time limit

The longest `/chat` stream seen in production is about 2.5 minutes. The agent emits an SSE heartbeat (`: ping`) after every second of silence (`agent/src/chat.py`), and `web`'s httpx timeout is a **read** timeout (120 s, `frontend/server.py`), which the heartbeat keeps from firing. Both stay as they are.

Add an overall limit, layered so each layer is longer than the one inside it:

| Setting                                                     | Value | Effect                                               |
| ----------------------------------------------------------- | ----- | ---------------------------------------------------- |
| Agent overall `/chat` limit                                 | 240 s | Sends an SSE error event and ends the stream cleanly |
| uvicorn `--timeout-graceful-shutdown` for `web` and `agent` | 270 s | In-flight work finishes before it would be cancelled |
| Compose `stop_grace_period` for `web` and `agent`           | 300 s | Docker sends SIGKILL only after uvicorn has finished |

- The agent image's `CMD ["sh", "-c", "python -m uvicorn ..."]` makes `sh` PID 1, which does not forward SIGTERM, so Docker's grace period never reaches uvicorn. Change it to `exec python -m uvicorn ...`. The web image already uses the exec form and only needs the timeout flag and `stop_grace_period`.
- Cleanup (stopping the snapshot poller, closing the connection pool and shared clients) stays in the lifespan shutdown phase, as today; nothing cancels `/chat` tasks in an earlier hook.
- The proxy's drain cap (5 minutes) is longer than the 240 s chat limit, so every stream in flight at the switch has ended before the cap; the grace period covers the race between "proxy reports 0" and `docker stop`.
- The 240 s limit is also what the snapshot GC's 10-minute grace period assumes (snapshot design 6.8): a superseded snapshot is never dropped while a request pinned to it could still be running.
- **In-place recreates wait too**: any in-place recreate of `agent` or `web` (a non-blue-green deploy, a manual restart) waits up to 5 minutes while a stream is open, which looks like a hang; the ops docs must say so.

### 4.4 Health endpoints

- `agent /health`: done (snapshot design 7.4), including the compose healthcheck on the agent.
- Done: `web` has `/readyz` (status code of `agent /health`, trimmed body, section 2.3); `/healthz` reports `instance` and stays independent of the agent; `web` has a compose healthcheck on `/healthz`.
- With healthchecks on both services (section 4.1), `docker compose up --wait` works as a simple local readiness gate; in production the deployment tool's consecutive-200 polling of `/readyz` is authoritative.

## 5. How Each Kind of Release Flows

| Release                                                       | Steps                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Users              |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------ |
| Application code only, same snapshot format                   | Run section 3 directly; green and blue read the same snapshot                                                                                                                                                                                                                                                                                                                                                                                                      | No interruption    |
| Snapshot format upgrade                                       | Upgrade the indexer in place first and wait until it publishes a full build in the new format (vector reuse means no embedding calls; under a minute on the current corpus). Old agents keep reading the newest old-format snapshot, which just stops updating. Then run section 3; green selects the new format                                                                                                                                                   | No interruption    |
| Embedding model or dimension change                           | Change `embedding.model` (and `space_id` if used) in the shared `config.yml` and `EMBEDDING_DIM` in `.env`, then recreate **only** the indexer (`docker compose up -d indexer`). It builds a full snapshot in the new vector space (about 25 minutes of embedding) while blue keeps serving the old space, because the running agent loaded its configuration at startup. Then run section 3; green starts with the new configuration and selects the new snapshot | No interruption    |
| Indexer only (normalization, chunking, …)                     | Upgrade the indexer in place; when the new snapshot is published the agents switch to it within 15 s. No blue-green needed                                                                                                                                                                                                                                                                                                                                         | No interruption    |
| PostgreSQL upgrade, incompatible change to the control tables | Out of scope; handled in a maintenance window                                                                                                                                                                                                                                                                                                                                                                                                                      | Brief interruption |

During a model change, blue's data is frozen from the indexer switch until green takes over (the old lineage gets no new builds); that is the build time plus one switch, about half an hour, so a transcript published in that window reaches users only after the switch.

If blue restarts inside that window (crash, host reboot), it rereads the shared `config.yml` and so gets the new model, but keeps the `EMBEDDING_DIM` it was created with, because a container's environment is fixed at creation. With an unchanged dimension it is not ready until the new snapshot is published; with a changed dimension its vector space matches no snapshot, and it stays not ready until green replaces it. That is the remaining risk of using the shared files, accepted because the window is short and restarts are rare.

**Rollback**:

- Before step 7: switch the proxy back to blue, drain green, and `down -v` green. No data needs restoring.
- After step 7: deploy the old version again as the new green. Snapshot GC keeps the latest snapshot of every non-current lineage for 7 days after it is superseded (snapshot design 6.8), so within 7 days a version on the old format or old model still has a snapshot to read. After that, it first needs a rebuild in that format or space.

## 6. One-Time Cutover to the New Layout

**Done in production on 2026-10-08** (results in section 11, "Production"). The steps below are kept as the procedure that was run.

Before the cutover, production ran one compose project, `bedtimenews-agent`, with four containers `bedtimenews-postgres`, `bedtimenews-indexer`, `bedtimenews-agent` and `bedtimenews-web` on `bedtimenews-agent_bedtimenews_net`, images pinned by `IMAGE_TAG` in `.env`; the agent connects as `rag_agent`. Section 4.1 keeps the data-layer project name, network key and service definitions, so the application switch itself has no downtime. The cutover costs exactly one planned postgres restart, from postgres's explicit environment (section 4.1):

1. At a quiet time, add `COMPOSE_FILE=compose.data.yml` to the VM's `.env` (section 4.1), then deploy the data layer with `docker compose -p bedtimenews-agent -f compose.data.yml up -d`. Run it with `--dry-run` first: it must show postgres as recreated, the indexer as `Running`, and only a warning that `bedtimenews-agent` and `bedtimenews-web` are orphans. Anything else is an unintended difference to remove first. The postgres restart takes a few seconds; the old agent reports not ready meanwhile and reconnects by itself (snapshot design 7.4), and requests that hit the database in that window fail.
2. Run steps 1–6 of section 3 by hand, since the deployment tool refuses to derive blue while the proxy still points at the data-layer project (section 3.1): start the first application instance (e.g. `bedtimenews-app-green`, with `BEDTIMENEWS_NET_EXTERNAL=true`), wait for readiness, switch the proxy and drain. The old `bedtimenews-agent` and `bedtimenews-web` containers keep serving meanwhile; the new project's services do not have those container names, so they do not take them over.
3. Stop the old containers by name, `web` first so its streams finish while the old agent still runs: `docker stop bedtimenews-web && docker stop bedtimenews-agent`, then remove them. The old agent log volume (`bedtimenews-agent_bedtimenews_agent_logs`) can be removed once its logs are no longer needed; nothing removes it automatically, because no compose project declares it any more (in production it was empty and was removed by hand).
4. Do not use `--remove-orphans` on the data-layer project before step 3: to the data-layer project those two old containers are orphans and would be removed at once, cutting live traffic.

## 7. Test Plan

1. **Isolation**: the data layer plus two application instances on different ports. Stop one `agent`; the other instance's `/readyz` stays 200. Check network membership: `web` only on the private network, `agent` on the private and data-layer networks. Recreate postgres; both agents become ready again with no action (the agent reconnects by itself, snapshot design 7.4).
2. **Chat limit**: a `/chat` longer than 240 s ends at the limit with an SSE error event.
3. **SIGTERM**: start a long `/chat` against instance A, then `docker compose stop` that instance; the stream ends normally. Repeat with a stream that has just started.
4. **Proxy switch**: through a local proxy, switch upstreams while a stream is running; the stream completes on the old instance, new requests go to the new one, and the proxy's in-flight count for the old upstream drops to 0.
5. **Formats side by side**: after the indexer publishes a snapshot in a new format, an old-version instance keeps reading the old format and stays ready, while a new-version instance reads the new format.
6. **Vector spaces side by side**: two instances with different `APP_CONFIG_FILE` / `EMBEDDING_DIM` (different embedding models) each select the snapshot of their own space and return correct results.
7. **Defaults and guard**: with no `APP_*` set, the umbrella runs on 8080 (or `FRONTEND_PORT`) with `IMAGE_TAG` (or `latest`); with `COMPOSE_FILE=compose.data.yml` set, `docker compose up -d` without `-f` starts only the data layer.
8. **Compose layout, on the production Compose version**: a fresh umbrella `up -d` and a second one both succeed and the second changes nothing; `-f docker-compose.yml -f docker-compose.sample.yml` still overrides services that come from the included files; a data-layer `up -d --dry-run` against containers created by the current single-file `docker-compose.yml` recreates only postgres (its explicit environment) and keeps the indexer `Running`; afterwards, editing a non-postgres variable in `.env` (e.g. `IMAGE_TAG`) no longer recreates postgres.
9. **Retirement**: `down -v` on an application project removes only its log volume and private network; the data layer's network, volumes and postgres data directory are untouched, and `web` stops before `agent`.
10. **Public endpoints**: `/healthz` answers 200 while the agent is stopped; `/readyz` answers 503 then, and its body never contains `indexer_status` or error text from the indexer.
11. **Deployment tool preflight**: with any required input of section 2.2 missing, it exits before running any compose command. It also exits when the live state cannot be derived consistently (section 3.1): the proxied port has no container, it belongs to the data-layer project, or a second application project exists; and a second run started while one holds the lock exits at once.

## 8. Phases

| Phase | Scope                                                                                                            | Notes                                                                                                                              |
| ----- | ---------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| A     | Agent started with `exec`, chat limit, grace periods, `web /readyz`, `instance` in `/healthz`, `web` healthcheck | **Done**, released in v0.4.1 (2026-10-08). Ordinary restarts now also let streams finish                                           |
| B     | Compose split, network layout, umbrella defaults, sample override, the VM's `COMPOSE_FILE` guard, README         | **Done**, released in v0.4.1; production cut over on 2026-10-08 (section 6), including the VM's `COMPOSE_FILE` guard               |
| C     | The deployment tool (outside this repository) implements section 3; first use follows section 6                  | Not started; section 3 is run by hand meanwhile. Needs A (drain and retirement rely on the chat limit and graceful shutdown) and B |

## 9. Decisions

- The data-layer project name stays `bedtimenews-agent` and the network key `bedtimenews_net`; the data-layer service definitions move unchanged except that postgres takes an explicit `environment:` instead of `env_file: .env`. That costs one planned postgres restart at the cutover and stops every later `.env` edit from recreating postgres.
- `docker-compose.yml` stays as the umbrella with self-hosting defaults; production calls the two files explicitly with `-p` and `-f`, and `COMPOSE_FILE` guards against accidents.
- `agent` is on both the data-layer and the private network; `web` only on the private one. Services on the data-layer network must not call `agent`.
- Both compose files declare the data-layer network under the same key `bedtimenews_net`; the application file makes it external through `BEDTIMENEWS_NET_EXTERNAL` (section 4.2). `agent` has no `depends_on` on the data layer.
- `/healthz` is liveness of `web` alone; `/readyz` carries readiness and the snapshot, with a body trimmed for public exposure.
- `APP_IMAGE_TAG` sets both `agent` and `web`; the indexer's version is managed separately through `IMAGE_TAG`.
- Data compatibility between versions comes from the snapshot selection rule; this design adds no schema-compatibility discipline, migration gates or maintenance labels.
- The deployment tool stores no state: the live port comes from the proxy configuration, the live project from the container publishing that port, the live version and snapshot from its `/healthz` and `/readyz` (section 3.1). It runs under a lock, recognizes application projects by the prefix `bedtimenews-app-`, and refuses to run while a second application project exists.
- Readiness gate: 3 consecutive 200s from `/readyz` at 2-second intervals, within 2 minutes.
- Chat limit 240 s, uvicorn grace 270 s, Docker grace 300 s; proxy drain cap 5 minutes.
- Application instances are retired with `down -v`, which removes only their log volume.
- After each retirement the deployment tool keeps only the running and the previous tag of the repository's images and prunes build cache older than a week; never a host-wide prune (section 3.2).
- The indexer builds snapshots in one vector space at a time. During a model change blue's data freezes for about half an hour (section 5) instead of the indexer building both spaces in parallel.
- An embedding model change edits the shared configuration and recreates only the indexer; the running blue keeps its loaded configuration (section 5). No per-instance configuration is required for it.

## 10. Documentation to Update When Implemented

Each README exists in three languages (`README.md`, `README.en.md`, `README.es-ES.md`); every README change below applies to all three. The Phase column refers to section 8.

### 10.1 This document

| Location                          | Change                                                                                                                                                                                                       | Phase   |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------- |
| Status line                       | "accepted design, not yet implemented" becomes "phases A and B implemented (version)", then "implemented, in production since (version)" after the first blue-green deploy                                   | A, B, C |
| Section 2.3                       | Drop "Today it returns `status` and `version`" once `/healthz` reports `instance`                                                                                                                            | A       |
| Section 4.3                       | Drop "Until this limit ships, the assumption rests only on observed stream lengths"                                                                                                                          | A       |
| Section 4.4                       | Mark `web /readyz`, `instance` and the `web` healthcheck as done, as `agent /health` already is                                                                                                              | A       |
| Section 6                         | Past tense once the cutover has run, with its date and anything that differed from the plan (the actual dry-run output, how long postgres was down)                                                          | C       |
| Section 8                         | Mark each phase done with its release                                                                                                                                                                        | A, B, C |
| New section: implementation notes | As in the snapshot design (its section 18): where each part landed in the code, any deviation from this design and why, and which test of section 7 covers it, or why one was only run by hand (3, 4, 8, 11) | A, B, C |

### 10.2 Diagrams (`docs/diagrams/`)

| Diagram                     | Used in                               | Change                                                                                                                                                                                                      | Phase |
| --------------------------- | ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `system-architecture.svg`   | Main README (all three languages)     | No change required: it shows one application instance, which is what self-hosters run. Optionally mark the boundary between the application layer (`web`, `agent`) and the data layer (PostgreSQL, indexer) | —     |
| `frontend-architecture.svg` | Frontend README (all three languages) | No change: `HTTP :8080` stays the default. If it is redrawn for another reason, `/readyz` can go next to the agent call                                                                                     | —     |
| Other diagrams              | —                                     | No change                                                                                                                                                                                                   | —     |

### 10.3 READMEs

| File              | Locations to change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Phase |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| Main `README`     | **Prerequisites**: Docker Compose 2.20 or later (`include`, section 4.1). **Quick Start** stays `docker compose up -d`; under **Verify Installation** add `curl localhost:8080/readyz`. **Releases**: `IMAGE_TAG` pins the indexer and, by fallback, the application; `APP_IMAGE_TAG` overrides the application version alone. **Published image vs. your checkout**: a source build for a blue-green instance uses a distinct tag (`0.4.0-<sha>`) and `--build` in the same `up` (section 3, step 2). **Add a section on zero-downtime upgrades**: the two layers, what the project provides (section 2) and requires from a proxy and deployment tool (section 3), with a link to this document; self-hosters who restart in place lose nothing but should expect a stop to wait up to 5 minutes while a stream is open (section 4.3). **Data Persistence**: the agent log volume is per application project. **Project Structure**: `compose.data.yml`, `compose.app.yml`, `docker-compose.yml` as the umbrella | B     |
| `frontend/README` | **Endpoints**: add `GET /readyz` (status code of the agent's `/health`, trimmed body, public by design) and the `instance` field of `/healthz`; say `/healthz` never calls the agent. **Configuration**: add `APP_INSTANCE`; `APP_VERSION` now comes from `APP_IMAGE_TAG` (falling back to `IMAGE_TAG`); `FRONTEND_PORT` becomes the fallback of `APP_PORT`. **Debugging**: the container name is no longer fixed (`docker compose ps web` instead of `bedtimenews-web`); mention the compose healthcheck. Note the 270 s graceful shutdown                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | A, B  |
| `agent/README`    | **POST /chat**: the 240 s overall limit and the SSE error event sent when it is reached. **GET /health**: `web /readyz` exposes a trimmed form of it publicly; the full body stays internal. **Network Access**: the agent is on the data-layer network and its instance's private network; nothing on the data-layer network may call `agent` (section 4.2). **Debugging**: container names follow the project. Note the graceful shutdown and that a stop can wait up to 5 minutes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | A, B  |
| `indexer/README`  | **Changing the Embedding Model**: recreate only the indexer, let blue keep serving, then switch the application with a blue-green deploy (section 5); without blue-green the old order still works. Commands that act on the data layer in production need `-f compose.data.yml` or the `COMPOSE_FILE` guard. **Data Backup and Restore** and any `docker compose down` there: in production, `down` runs only on the data-layer project and never with `-v` on an application project's behalf                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | B     |

### 10.4 Example configs, compose files and code comments

| File                        | Change                                                                                                                                                                                                                                                                                                                                                                                                                 | Phase |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `.env.example`              | Comment `IMAGE_TAG` as the indexer's version and the application's fallback; list `APP_IMAGE_TAG`, `APP_PORT`, `APP_INSTANCE`, `BEDTIMENEWS_NET_NAME`, `BEDTIMENEWS_NET_EXTERNAL` and `APP_CONFIG_FILE` as commented-out, per-instance variables that a deployment tool passes on the command line and that must **not** be set here (section 2.2); describe the production-only `COMPOSE_FILE=compose.data.yml` guard | B     |
| `compose.data.yml` (new)    | Header comment: the data layer, upgraded in place, never `down -v`; why postgres has an explicit `environment:` (section 4.1); why the network key and project name must not change                                                                                                                                                                                                                                    | B     |
| `compose.app.yml` (new)     | Header comment: one project per instance, the inputs of section 2.2 and their fallbacks; comments on the network declaration (both alternatives of section 4.2 fail), on the absent `container_name` and `depends_on: postgres`, and on `stop_grace_period: 300s`                                                                                                                                                      | A, B  |
| `docker-compose.yml`        | Becomes the umbrella (`include`); its comment says it is for self-hosting and development, and that production calls the two files with `-p` and `-f`                                                                                                                                                                                                                                                                  | B     |
| `docker-compose.sample.yml` | Check the comments still hold now that the services it overrides come from included files; decide whether its `container_name` overrides for `agent` and `web` stay (they are fine for a single sample instance)                                                                                                                                                                                                       | B     |
| `agent/Dockerfile`          | Comment why the `CMD` uses `exec` (section 4.3)                                                                                                                                                                                                                                                                                                                                                                        | A     |
| `frontend/Dockerfile`       | Comment the `--timeout-graceful-shutdown` value and how it relates to the agent's limit and the compose grace period                                                                                                                                                                                                                                                                                                   | A     |
| `agent/src/chat.py`         | Next to the 240 s limit, the layering of section 4.3 (240 < 270 < 300 s, drain cap 5 minutes) and that snapshot GC's 10-minute grace period depends on it                                                                                                                                                                                                                                                              | A     |
| `frontend/server.py`        | Docstrings of `/healthz` and `/readyz`: why one never calls the agent and why the other trims the body (section 2.3)                                                                                                                                                                                                                                                                                                   | A     |

### 10.5 Other documents

| Document                                                | Change                                                                                                                                                                                                                                                          | Phase |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `docs/designs/20261007_rag-snapshot-architecture.md`    | Section 6.8 (GC grace period) states the 240 s limit as a fact; add a pointer that it is enforced by this design (section 4.3). Section 3's non-goal "blue-green deployment builds on this design but is out of scope" links to this document                   | A     |
| Release notes of the releases that ship phases A and B  | Phase A: in-place restarts now wait for open streams (up to 5 minutes), and `/chat` has a 240 s limit. Phase B: Docker Compose 2.24.6 or later; container names of `agent` and `web` are no longer fixed; for self-hosters, `docker compose up -d` is unchanged | A, B  |
| Production deployment runbook (outside this repository) | The VM's two-layer layout, the `COMPOSE_FILE` guard, the cutover of section 6, the deployment tool and its rollback, and that `down -v` never runs on `bedtimenews-agent`. It lives with the deployment tool, not in this repository                            | B, C  |

## 11. Implementation Notes (2026-10-08)

### Where each part landed

| Part                  | Location                                                                                                                                                                                                                                                                                                                                      |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chat limit            | `agent/src/chat.py`: `CHAT_TIME_LIMIT_S = 240`. Streaming: the consumer loop tracks a deadline, keeps sending heartbeats until it, then emits `{"type":"error","content":"回答超时，请缩小问题范围后重试。"}` and `[DONE]` and cancels the producer task. Non-streaming: `asyncio.wait_for` around the worker thread; `main.py` answers `504` |
| Graceful shutdown     | `agent/Dockerfile` (`exec python -m uvicorn … --timeout-graceful-shutdown 270`), `frontend/Dockerfile` (`--timeout-graceful-shutdown 270`), `stop_grace_period: 300s` on both services in `compose.app.yml`                                                                                                                                   |
| `/healthz`, `/readyz` | `frontend/server.py`. `APP_INSTANCE` defaults to `local`. `/readyz` calls the agent's `/health` with a 3 s timeout and returns 200 only when the agent answered 200 **and** its body says `ready: true`; both endpoints send `Cache-Control: no-store`                                                                                        |
| Compose split         | `compose.data.yml`, `compose.app.yml`, `docker-compose.yml` (umbrella with `include`); `docker-compose.sample.yml` keeps its overrides and `container_name`s; CI job `compose config` validates the umbrella, the sample overlay, the data layer and an application instance in two-project mode                                              |
| Documentation         | Section 10 items: READMEs (root, frontend, agent, indexer; three languages), `.env.example`, compose and Dockerfile comments, `chat.py`/`server.py` docstrings, snapshot design 3 and 6.8. Not changed: the optional diagram edits of 10.2. Release notes are written at release time, not in this repository                                 |

### Deviations and details

- **Non-streaming `/chat` past the limit** returns `504` with the timeout message. The design only specified the streaming case. The worker thread cannot be cancelled: it finishes its current model call and its result is discarded.
- **`/readyz` body**: `reason` is present only when not ready. Besides the agent's own reasons, `web` reports `agent unreachable` (connection error or timeout) and `web is starting`. A 200 from the agent without `ready: true` is treated as not ready.
- **Readiness lags by up to one poll.** `/readyz` reflects the agent's last snapshot poll (≤ 15 s old). In the cutover rehearsal a postgres restart therefore showed ~15 s of 503 on the old agent although the database was back after a few seconds, and, the other way round, right after a forced postgres recreate `/readyz` still answered 200 from the previous poll (chats served normally once checked 16 s later; requests inside the restart window itself were not measured). A deployment tool's 3-consecutive-200s gate (6 s) is shorter than a poll interval, so it cannot detect a database outage that started after the last poll; that is acceptable for a gate whose job is "green can serve", and the proxy switch does not depend on the database.
- **Network declaration and minimum Compose version.** As first written (section 4.2's original plan: `driver: bridge` in `compose.data.yml`, `name`/`external` in `compose.app.yml`), the umbrella failed in CI on Compose 2.38.2 with `networks.bedtimenews_net conflicts with imported resource`; Compose 5.6, used for the first local tests, accepts differing declarations. Both files now carry the identical declaration of section 4.2. Checked with real Compose binaries: 2.20.3 and 2.22.0 reject any network declared by two included files (2.20 also has no `COMPOSE_PROJECT_NAME` for interpolation); 2.23.x and 2.24.6 or later accept the umbrella and the sample overlay; 2.24.0–2.24.5 accept the umbrella but cannot override included services, which breaks the sample overlay. The documented minimum is therefore 2.24.6. A variant without `driver: bridge` passed `config` but, against the production-shaped network created from the old file, made a data-layer `up --dry-run` stop postgres and the indexer to recreate the network (Compose's network config hash changed); with `driver: bridge` kept, the dry run recreates only postgres, as section 6 expects. Re-verified on Compose 2.38.2 with real containers: the cutover's data-layer `up` kept the network (same ID), an application project joined it as external and reached the database, `down -v` of that project left it in place, and a fresh umbrella `up -d` followed by a second one changed nothing (also on 2.24.6 and 5.6). CI checks the layouts with the runner's Compose.
- **`APP_VERSION`** of `web` comes from `${APP_IMAGE_TAG:-${IMAGE_TAG:-}}`; as before, `latest` or empty falls back to the package version.
- **Production guard and orphans**: with `COMPOSE_FILE=compose.data.yml`, `docker compose up -d` in the data-layer directory warns that `agent` and `web` containers of an umbrella-started stack are orphans, exactly as section 6 predicts.

### Tests (section 7)

Unit tests: `agent/tests/test_chat_limit.py` (test 2: a stream past the limit ends with the error event and cancels the producer; a non-streaming request gets 504; the 240/270/300 s layering is checked against the Dockerfiles and `compose.app.yml`), `frontend/tests/test_server.py` (test 10: `/healthz` never calls the agent; `/readyz` trims the body, never forwards `indexer_status`, and is 503 when the agent is unreachable or not ready).

End to end, with Docker Compose 5.6 (and, for the network fix above, 2.38.2) and Docker 29.8 on a local host, images built from this tree, PostgreSQL 18 + pgvector in a container, and a local OpenAI-compatible stand-in for the model APIs (the real providers are unreachable from that host). Snapshots were published by running the indexer against the containerized database:

| Test | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | Passed. With the data layer and two application projects: stopping one agent left the other `/readyz` at 200, while the stopped instance's `web` reported `agent unreachable` (it did not fall through to the other agent). `web` was only on its private network, `agent` on its private and the data-layer network. After `--force-recreate postgres`, both instances served chats and transcripts again without any action (checked 16 s later)                                                                                                    |
| 2    | Unit test only                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| 3    | Passed. `docker compose stop` on an instance with a 20-second stream open (started 6 s and 0.5 s before the stop) waited 15 s and 20 s respectively; both streams completed with every chunk and `[DONE]`, `web` stopped before `agent`, both exited 0 (no SIGKILL)                                                                                                                                                                                                                                                                                   |
| 4    | Passed with Caddy 2 (`caddy reload` of a Caddyfile): after the switch new requests reached the new instance at once; `/reverse_proxy/upstreams` kept listing the old upstream with `num_requests: 1` until its 20-second stream completed on the old instance, then dropped it                                                                                                                                                                                                                                                                        |
| 5    | Half: a snapshot registered with `format_version = 2` was ignored by the current agent, which stayed ready on its format-1 snapshot. Selecting the higher format is covered by the agent unit test `test_higher_supported_format_beats_newer`                                                                                                                                                                                                                                                                                                         |
| 6    | Passed. A second snapshot was published under `embedding.space_id` `…@2560-alt`; an instance started with `APP_CONFIG_FILE=./config-alt.yml` selected it, the other instance kept the default space, and both answered chats                                                                                                                                                                                                                                                                                                                          |
| 7    | Passed. Without `APP_*` the umbrella ran on `FRONTEND_PORT` with `IMAGE_TAG` and `instance: local`; with `COMPOSE_FILE=compose.data.yml` in `.env`, `docker compose up -d` touched only postgres and the indexer and warned about the orphaned `agent`/`web`                                                                                                                                                                                                                                                                                          |
| 8    | Passed. A fresh umbrella `up -d` succeeded and a second one left every container `Running`; the sample overlay still overrides the included services (container names, `POSTGRES_DB`, `INDEXER_SCOPE`, port); against containers started from the previous single-file `docker-compose.yml` as project `bedtimenews-agent`, the data-layer `--dry-run` showed postgres `Recreate`, the indexer `Running`, and only the orphan warning for `bedtimenews-web`/`bedtimenews-agent`; afterwards, editing other `.env` variables did not recreate postgres |
| 9    | Passed. `down -v` of an application project removed exactly its log volume and its private network; the data-layer network, the indexer log volume, postgres and its data directory were untouched                                                                                                                                                                                                                                                                                                                                                    |
| 10   | Passed (unit test and by hand)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| 11   | Not applicable yet: the deployment tool (phase C) is outside this repository                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |

**Cutover rehearsal (section 6)** on the same host, starting from the previous single-file layout as project `bedtimenews-agent`: step 1's real `up -d` replaced postgres in about 11 s; the old agent reported not ready for ~15 s (one poll interval) and recovered by itself; the indexer was not touched. Steps 2 and 3 then ran as written: the first application project became ready in about 3 s, the proxy switch drained the old `web`, and the old containers were stopped `web` first and removed.

### Production (2026-10-08)

On the production VM (Docker Compose 2.40.3, Caddy 2.11.4 as the edge proxy). There is no deployment tool yet (phase C), so section 3 was run by hand; the proxy's upstream for the site now lives in a separate file included by the Caddyfile, so a switch rewrites only that file.

**Cutover (section 6)**, with a source build of `ac2cada` (`0.4.0-ac2cada`) as the first application instance:

1. The data-layer `--dry-run` showed exactly what step 1 predicts: postgres `Recreate`, the indexer `Running`, and the orphan warning for `bedtimenews-agent`/`bedtimenews-web`. The real `up -d` replaced postgres in 10 s; the network kept its ID; the old agent's `/health` was 503 for about 10 s and recovered by itself; the indexer was not touched.
2. The first application project, `bedtimenews-app-green`, passed the readiness gate (3 consecutive 200s) within 4 s, reported the expected version and instance, served the same snapshot as the old agent, and had `web` only on its private network and `agent` on both. After the proxy reload, public requests reached it at once; the old upstream had no requests in flight.
3. The old `bedtimenews-web` stopped cleanly; the old 0.4.0 `bedtimenews-agent` still had the `sh -c` PID 1 of section 4.3, ignored SIGTERM and was killed after Docker's 10 s default, harmless since it no longer had traffic. Both were removed, and later the unused old agent log volume.

**First blue-green upgrade**, to the v0.4.1 release images, as `bedtimenews-app-blue` next to `bedtimenews-app-green`. The live state was derived as in section 3.1 (port from the proxy configuration, project from the container publishing it). A probe on the VM requested the public `/healthz` once a second for the whole upgrade:

| Step                        | Result                                                                                                                                                                                                       |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Start and gate              | Images pulled, ready after 6 s; same snapshot as the live instance                                                                                                                                           |
| Switch (`caddy reload`)     | The probe's answers flipped from `0.4.0-ac2cada` / `bedtimenews-app-green` to `0.4.1` / `bedtimenews-app-blue` within the second of the reload; green's last proxied request and blue's first were 1 s apart |
| Drain                       | Immediate: the old upstream had no requests in flight. An open stream draining across the switch was not exercised in production (test 4 covers it)                                                          |
| Retire (`down -v`)          | 1 s; removed exactly green's containers, private network and log volume (test 9)                                                                                                                             |
| Indexer to 0.4.1, in place  | After the `IMAGE_TAG` edit in `.env`, the data-layer `--dry-run` recreated only the indexer and kept postgres `Running`, as section 4.1 intends                                                              |
| Availability, whole upgrade | 158 of 158 probes answered 200; latency median 9 ms, maximum 17 ms, with no change at the switch                                                                                                             |
