# Deployment

`ews-mcp` runs either as a local `stdio` child process started by your MCP client or as an HTTP
service on a host or container. Startup never waits for Exchange to connect: `ewsmcp` warms the
Exchange connection in the background and keeps running through transient network faults.

## Choose how to run it

Pick the deployment mode that matches where your MCP client runs and how your machine reaches
Exchange:

| mode | when to use it | network path to Exchange | network port | jump to |
|---|---|---|---|---|
| **Local over `stdio`** (recommended) | One person, one workstation, one MCP client. | Uses your workstation's network, including a workstation VPN. | None (`stdin` / `stdout`). | [Local over stdio](#local-over-stdio) |
| **Several clients on one machine** | Two or more MCP clients on the same workstation sharing one mailbox. | Uses your workstation's network, including a workstation VPN. | None (shared `DATA_DIR`) or `127.0.0.1:8000` (loopback HTTP). | [Several clients on one machine](#several-clients-on-one-machine) |
| **HTTP server** | `ewsmcp` runs on another host or as a persistent background service. | Uses the server host's network, **not** a client workstation's VPN. | `MCP_HOST:MCP_PORT` (default `127.0.0.1:8000`). | [HTTP server](#http-server) |
| **Container** (Docker / Compose) | Packaged non-root Linux service with an isolated `/data` volume. | Uses the container host's network, **not** a workstation's VPN. | Host `127.0.0.1:8000` mapped to container `8000`. | [Docker](#docker) · [Docker Compose](#docker-compose) |

## Local over stdio

In `stdio` mode, your MCP client launches `ewsmcp` as a child process and talks to it over
`stdin`/`stdout`. Nothing listens on a network port, no `MCP_API_KEY` is needed, and Exchange
traffic uses your workstation's normal network routes and VPN.

`pip install "./v5[extract]"` installs `pypdf` for PDF text extraction; `ews-mcp` is not published
on PyPI, so always install from the local `./v5` directory.

Clone the repository and install into a virtual environment from the **repository root**:

```bash
git clone https://github.com/azizmazrou/ews-mcp
cd ews-mcp
python3 -m venv .venv        # Windows: python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install "./v5[extract]"        # or `pip install ./v5` without PDF extraction
```

The executable is now at `.venv/bin/ewsmcp` on Linux and macOS, or `.venv\Scripts\ewsmcp.exe` on
Windows. Register that **absolute path** in your MCP client and pass credentials in its `env`
configuration; client snippets are in [`v5/README.md`](../README.md). With Claude Code,
`claude mcp add` registers the server for the current project only unless `--scope user` is given.
Add `--scope user` to make it available in every project, or run the command in the project where
you will use it:

```bash
claude mcp add exchange \
  -e EWS_SERVER_URL="https://mail.example.com/EWS/Exchange.asmx" \
  -e EWS_EMAIL="you@example.com" \
  -e EWS_USERNAME="you" \
  -e EWS_PASSWORD="change-me" \
  -- /path/to/ews-mcp/.venv/bin/ewsmcp
```

On Windows, run the command on one line and point to `C:\path\to\ews-mcp\.venv\Scripts\ewsmcp.exe`.
If your home directory sits inside OneDrive, Dropbox, Google Drive, or iCloud, also pass
`-e DATA_DIR="/path/to/local/ewsmcp-data"` — such as `C:\ewsmcp-data` on Windows — so `DATA_DIR`
stays off synced storage. See [`CONFIGURATION.md`](CONFIGURATION.md#storage).

**What to check afterwards:**

1. Confirm the MCP client connected to the child process. With Claude Code, run `claude mcp list`
   in the project where you registered it (or in any directory if you passed `--scope user`) and
   check that `exchange` shows as connected.
2. Ask the assistant to call `get_server_status` (which answers at any time — while still
   connecting, after credential rejection, and while the circuit breaker is open) and verify:
   - `connection.state` is `"warm"`. It shows `"connecting"` during initial startup before the first
     probe finishes; `"auth_failed"` means Exchange rejected the credentials.
   - `tier` is `"draft"` and `send_kill_switch` is `true`, unless you changed them.
   - `cache.ready` is `true` and `cache.sync.role` is `"leader"`.

## Several clients on one machine

When two or more MCP clients on the same workstation — such as Claude Desktop and Claude Code — open
the same mailbox, choose between shared-directory `stdio` and a single loopback HTTP server.

### Option 1: Separate `stdio` processes sharing one `DATA_DIR`

Configure each client to launch `ewsmcp` over `stdio` with the same `DATA_DIR`, or leave `DATA_DIR`
unset in all of them so each resolves to `~/.ewsmcp`.

- **Leader election.** The first process to start acquires a non-blocking OS lock on
  `DATA_DIR/cache/sync.lock` and runs the background `SyncEngine` with `role: "leader"`.
- **Follower reads.** Additional processes see the lock held and run with `role: "follower"`. They
  serve fast reads from the shared WAL-mode `cache/mirror.db` and `memory/aliases.db` without
  duplicating `SyncFolderItems` traffic against Exchange.
- **Automatic failover.** If the leader exits or crashes, the OS kernel releases `sync.lock`
  immediately. Followers retry acquiring the lock every `30` seconds; the next follower to acquire
  it starts `SyncEngine` once its Exchange connection is warm. `EWS_CACHE_PURGE_ON_BOOT=true` runs
  only in the leader and is skipped with a warning by followers.
- **Trade-off on password changes.** Each process submits the old password again after every
  cooldown until it reaches `EWS_AUTH_MAX_REJECTIONS`: by default up to `3` failed logins per
  process, about 15 minutes apart. Stop every process before changing the password.
- **Audit chain.** With several processes sharing one `DATA_DIR`, their interleaved records in
  `DATA_DIR/audit/` break the hash chain by themselves, so `verify_audit_chain.py` is not
  meaningful there.

### Option 2: One loopback HTTP server for all local clients

Run a single `ewsmcp` daemon on `127.0.0.1` so every client shares one warm Exchange session, one
lockout guard, one send rate-cap window, and one intact audit chain.

After creating `.venv` as shown in [Local over stdio](#local-over-stdio), create `.env` in the
**repository root**:

```bash
cp v5/.env.example .env
chmod 600 .env   # on Linux/macOS
```

Edit `.env` to set `EWS_SERVER_URL`, `EWS_EMAIL`, `EWS_USERNAME`, `EWS_PASSWORD`,
`MCP_TRANSPORT=http`, and `MCP_API_KEY`, then start the server from the **repository root**:

```bash
.venv/bin/ewsmcp
```

Point HTTP-capable MCP clients at `http://127.0.0.1:8000/mcp` with header
`Authorization: Bearer <MCP_API_KEY>`. Clients that speak only `stdio` can bridge to it:

```bash
npx -y mcp-remote http://127.0.0.1:8000/mcp --header "Authorization: Bearer <MCP_API_KEY>"
```

**What to check afterwards:**

- **Shared-`DATA_DIR` `stdio`:** call `get_server_status` from two clients and confirm one reports
  `cache.sync.role: "leader"` while the other reports `cache.sync.role: "follower"`.
- **Loopback HTTP:** from any directory, run `curl -s http://127.0.0.1:8000/livez` to verify HTTP
  `200` immediately, and `curl -s http://127.0.0.1:8000/readyz` to verify HTTP `200` with
  `"status": "ok"` once Exchange connects.

## HTTP server

Use `MCP_TRANSPORT=http` when `ewsmcp` runs on another host, a VM, or a background service. An HTTP
server reaches Exchange over **its own host's network**, not a client workstation's VPN.

From the **repository root**:

```bash
python3 -m venv .venv        # Windows: python -m venv .venv
.venv/bin/pip install "./v5[extract]"   # or "./v5" without PDF extraction
cp v5/.env.example .env
chmod 600 .env                          # on Linux/macOS
```

Edit `.env` in the repository root and set at least:

```ini
EWS_SERVER_URL=https://mail.example.com/EWS/Exchange.asmx
EWS_EMAIL=you@example.com
EWS_USERNAME=you
EWS_PASSWORD=change-me
MCP_TRANSPORT=http
MCP_HOST=127.0.0.1
MCP_PORT=8000
MCP_API_KEY=replace-with-a-long-random-secret
DATA_DIR=~/.local/share/ewsmcp
```

`DATA_DIR` must point to a local directory writable by the OS user running `ewsmcp` (leaving it
unset defaults to `~/.ewsmcp`). If you run `ewsmcp` under a dedicated system account with
`DATA_DIR=/var/lib/ewsmcp`, create that directory and grant write ownership first (`sudo mkdir -p
/var/lib/ewsmcp && sudo chown <service-user> /var/lib/ewsmcp`); if `DATA_DIR` is not writable,
`ewsmcp` still starts while logging `aliaser init failed` and `cache init failed`, running with raw
EWS ids, `cache.ready: false`, and no audit log.

Start the server **from the directory that holds `.env`** — here, the **repository root**:

```bash
.venv/bin/ewsmcp
```

With `MCP_HOST=127.0.0.1` (the default), this listens on `127.0.0.1` only. To serve other machines,
put a TLS reverse proxy in front of it, or set `MCP_HOST=0.0.0.0` behind a firewall.

Key HTTP behaviours in `ewsmcp/http.py`:

- **Endpoints.** Streamable HTTP MCP is served at `/mcp` (the replacement for legacy SSE). Plain
  REST for scripts is served at `GET /api/tools`, `POST /api/tools/{name}`, and `GET /openapi.json`.
  On `POST /api/tools/{name}`, pass the tool's arguments as the JSON request body (`{}` when a tool
  takes none); every registered tool's JSON Schema is published at `GET /openapi.json`.
- **Authentication.** When `MCP_API_KEY` is set, every non-public route requires either
  `Authorization: Bearer <key>` or `x-api-key: <key>`. Leave `MCP_API_KEY` unset only on loopback
  or an isolated network; see [`CONFIGURATION.md`](CONFIGURATION.md#serving) and
  [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#http-mode-and-clients).
- **Request size limit.** Both `/mcp` and `POST /api/tools/{name}` cap request bodies at
  `(EWS_MAX_ATTACHMENT_MB * 1024 * 1024) * 4 // 3 + 1_048_576` bytes — about `34.3` MiB at the
  default `25` MiB — to fit base64-encoded attachments.
- **TLS.** `ewsmcp` speaks plain HTTP (`h11`). Keep `MCP_HOST=127.0.0.1` and place a
  TLS-terminating reverse proxy in front before exposing the service across a network.

**What to check afterwards:**

Run from any directory, with `MCP_API_KEY` exported in your shell for the REST check:

```bash
curl -i http://127.0.0.1:8000/livez
curl -i http://127.0.0.1:8000/readyz
curl -i -H "Authorization: Bearer $MCP_API_KEY" http://127.0.0.1:8000/api/tools
curl -s -X POST http://127.0.0.1:8000/api/tools/get_server_status \
  -H "x-api-key: $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{}"
```

Confirm `/livez` returns `200`, `/readyz` returns `200` with `"status": "ok"` once warmup finishes,
`GET /api/tools` returns `200` with the tool list (`401` without the header), and
`POST /api/tools/get_server_status` returns `"ok": true` with `"cache": {"enabled": true, "ready":
true, ...}` (`cache.ready: false` means `DATA_DIR` was not writable or SQLite failed to open).

## Docker

[`v5/Dockerfile`](../Dockerfile) builds a multi-stage `python:3.11-slim` image running as
unprivileged user `mcp` with `DATA_DIR=/data`, `MCP_TRANSPORT=http`, `MCP_HOST=0.0.0.0`, and
`MCP_PORT=8000`.

A container sees **its container host's network, not a workstation's VPN**. If Exchange is only
reachable through a workstation VPN tunnel that containers do not share, run
[Local over stdio](#local-over-stdio) on the workstation instead.

Create `.env` in the **repository root** from `v5/.env.example` with unquoted `EWS_SERVER_URL`,
`EWS_EMAIL`, `EWS_USERNAME`, `EWS_PASSWORD`, and `MCP_API_KEY` values; omit `DATA_DIR` and
`MCP_HOST` so the container keeps `DATA_DIR=/data` and `MCP_HOST=0.0.0.0`. Then run from the
**repository root**:

```bash
docker build -t ews-mcp:dev v5
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env -v ewsmcp-data:/data ews-mcp:dev
```

Pass `v5` as the build context as shown above; running `docker build .` at the repository root
builds the legacy 4.0 image instead.

To run a published image without building locally, pull and run a pinned `v4.5.*` tag from any
directory that contains your `.env` file:

```bash
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
  -v ewsmcp-data:/data ghcr.io/azizmazrou/ews-mcp:v4.5.0a4
```

**What to check afterwards:**

From any directory on the Docker host:

```bash
docker ps
curl -i http://127.0.0.1:8000/readyz
```

Confirm the container `STATUS` moves from `(health: starting)` to `(healthy)` on `/livez`, and
`/readyz` answers HTTP `200` with `"status": "ok"` once Exchange connects. If `/readyz` stays `503`,
inspect `connection` in its JSON body and run `docker logs <container_id>`.

## Docker Compose

[`v5/docker-compose.example.yml`](../docker-compose.example.yml) defines a single-service HTTP
deployment with loopback port binding `127.0.0.1:8000:8000`, `EWS_CAPABILITY_TIER` defaulting to
`read`, `SEND_ENABLED` defaulting to `false`, a named `ewsmcp_data:/data` volume, a `512m` memory
limit, rotated `json-file` logs (`10m`, `3` files), and a `/livez` healthcheck. Like plain Docker,
the container uses **its host's network, not a workstation's VPN**.

Create `.env` in the **repository root** with the variables required by
`v5/docker-compose.example.yml`:

```ini
EWS_MCP_IMAGE=ghcr.io/azizmazrou/ews-mcp:v4.5.0a4
EWS_SERVER_URL=https://mail.example.com/EWS/Exchange.asmx
EWS_EMAIL=you@example.com
EWS_USERNAME=you
EWS_PASSWORD=change-me
MCP_API_KEY=replace-with-a-long-random-secret
```

Start the service from the **repository root**:

```bash
chmod 600 .env   # on Linux/macOS
docker compose -f v5/docker-compose.example.yml --env-file .env up -d
```

**What to check afterwards:**

From the **repository root**:

```bash
docker compose -f v5/docker-compose.example.yml --env-file .env ps
curl -i http://127.0.0.1:8000/readyz
```

Confirm the `ews-mcp` service reaches `healthy` and `/readyz` answers HTTP `200` once Exchange
connects, with `"tools": 18` at the Compose file's default `read` tier (or `"tools": 29` if `.env`
sets `EWS_CAPABILITY_TIER=draft`).

## Health, readiness and metrics

In `http` mode (`ewsmcp/http.py`), health and readiness routes stay public so orchestrators can
probe the process without carrying `MCP_API_KEY`. Operational and tool routes require `MCP_API_KEY`
via `Authorization: Bearer <key>` or `x-api-key: <key>` whenever `MCP_API_KEY` is set.

| route | method | auth | 200 response | when it is not 200 |
|---|---|---|---|---|
| `/livez` | `GET` | None | `{"status": "ok"}` as soon as the process is up. | Only if the process is unreachable, or on non-`GET` methods (`401` if `MCP_API_KEY` is set, else `404`). Never fails on Exchange outages; use this for container `HEALTHCHECK`. |
| `/readyz` | `GET` | None | `{"status": "ok", "connection": {...}, "tools": <n>}` when `connection.state` is `"warm"`. | **`503`** while `connection.state` is `"connecting"` or `"degraded"` (`"status": "unavailable"`), or when credentials were rejected (`"status": "auth_failed"`). Non-`GET` returns `401` or `404`. |
| `/health` | `GET` | None | `{"status": "ok", "tools": <n>}` with the registered tool count. | Process unreachable, or non-`GET` method (`401` or `404`). Does not probe Exchange. |
| `/version` | `GET` | None | `{"version": "<pkg-version>"}`. | Non-`GET` method (`401` or `404`). |
| `/metrics` | `GET` | `MCP_API_KEY` | Prometheus text (`0.0.4`) gauges and counters. | **`401`** (`auth_failed`) when `MCP_API_KEY` is set and missing or wrong; **`404`** on non-`GET` methods. |
| `/mcp` | `POST` / `GET` / `DELETE` | `MCP_API_KEY` | Streamable HTTP MCP responses (`StreamableHTTPSessionManager`). | **`401`** when `MCP_API_KEY` is missing or wrong; **`503`** if no MCP transport is mounted; **`413`** / MCP protocol errors on oversized or invalid requests. |
| `/openapi.json` | `GET` | `MCP_API_KEY` | OpenAPI `3.0.3` specification for all registered `/api/tools/{name}` endpoints. | **`401`** when `MCP_API_KEY` is missing or wrong; **`404`** on non-`GET` methods. |
| `/api/tools` | `GET` | `MCP_API_KEY` | `{"tools": [{"name", "class", "description"}, ...]}` for the active capability tier. | **`401`** when `MCP_API_KEY` is missing or wrong; **`404`** on non-`GET` methods or trailing slash (`GET /api/tools/`). |
| `/api/tools/{name}` | `POST` | `MCP_API_KEY` | Tool result JSON envelope (`{"ok": true, ...}`, including phase-1 confirm previews). | Returns **`400`**, **`401`**, **`403`**, **`404`**, **`409`**, **`413`**, **`429`**, **`500`**, **`502`**, or **`503`** with an error envelope (status map below). |
| `/.well-known/*`, `/register` | Any | None | Never returns `200`. | Always **`404`** so MCP clients skip OAuth discovery and use configured headers. |

#### `/api/tools/{name}`

- **`400`** — invalid JSON, schema mismatch, or `validation`.
- **`401`** — bad or missing `MCP_API_KEY`, or tool `auth_failed`.
- **`403`** — `kill_switch` or `recipient_blocked` (`tier_blocked` also maps to `403` as a
  dispatcher second check that clients do not normally reach).
- **`404`** — `validation` with `"Unknown tool: <name>"` for an unknown tool or a tool above
  `EWS_CAPABILITY_TIER` (not registered), `POST /api/tools/`, or non-`POST` methods.
- **`409`** — `confirm_invalid`.
- **`413`** — request body above limit.
- **`429`** — `throttled` or `rate_capped`.
- **`500`** — `internal` (unwritable attachments folder or unavailable document worker).
- **`502`** — `upstream_error` (including unexpected local exceptions).
- **`503`** — `upstream_unavailable`.

`GET /metrics` exports:

- `ewsmcp_uptime_seconds`, `ewsmcp_connection_warm`, `ewsmcp_auth_failed`, and
  `ewsmcp_auth_rejections`
- `ewsmcp_tool_calls_total{tool="..."}` and `ewsmcp_errors_total{code="..."}`
- `ewsmcp_cache_rows{table="..."}` and `ewsmcp_cache_db_mb` when the SQLite mirror is active
- `ewsmcp_sync_cycles_total`, `ewsmcp_sync_last_cycle_age_seconds`, and `ewsmcp_sync_degraded` when
  `SyncEngine` has started
- `ewsmcp_semantic_enabled` when `EWS_SEMANTIC_INDEX='pgvector'` is active

Over both `stdio` and `http`, the `get_server_status` tool answers at any time: while the server is
still connecting, after Exchange rejected the credentials, and while the circuit breaker is open.
While the circuit breaker is open, tools that need Exchange are refused with `upstream_unavailable`
and `retry_after_s`, whereas `get_server_status`, `waiting_on`, and `find_similar` still answer.
`get_server_status` returns the `connection` snapshot (`state`, `attempts`, `last_error`,
`last_success_age_s`, `next_retry_in_s`, `auth_rejections`, `auth_permanent`, plus
`circuit_open: true` and `retry_after_s` while the circuit breaker is open; both keys are absent
otherwise), capability tier, send kill-switch, tool counters, alias counts, cache watermarks, sync
role, and document-worker memory ceiling.

## Logs and the audit chain

### Process logs

`ewsmcp` writes all logs to `stderr` (`%(asctime)s [%(levelname)s] %(name)s: %(message)s`) at
`LOG_LEVEL` (`INFO` by default) so `stdout` stays clean for `stdio` MCP frames. On HTTP `401`
responses from Exchange, the gateway logs only bare scheme tokens —
`Exchange HTTP 401 rejection: WWW-Authenticate schemes […]; ews_auth_type_force=…` — and never logs
realms or challenge payloads.

### Hash-chained audit log

The audit log records one JSON line per dispatched tool call in
`DATA_DIR/audit/audit-YYYYMMDD.jsonl` (`ewsmcp/audit.py`):

- Recorded fields on every call: `ts` (time), monotonic `seq`, `tool`, `class` (`read`, `write`,
  `send`, `destructive`), `outcome` (`ok`, `phase1`, or the error code), `latency_ms`, and
  `transport` (`mcp` or `rest`).
- Hash and target fields: `prev` holds the first 12 hex characters of the previous line's hash (or
  `GENESIS` on `seq=1`), `h` holds `sha256(prev_full_hash + "|" + canonical_json)`, and `send` and
  `destructive` calls add `detail` holding the target ids of the call (`draft_id`, `event_id`,
  `ids`).
- Recipients, subjects, and bodies are not recorded.
- Each line carries the hash of the line before it, and `DATA_DIR/audit/chain.state` persists
  `{"prev": "<full_hash>", "seq": <n>}` across restarts. Audit writes are best effort: a failed
  write does not fail the tool call, and `DATA_DIR/audit/` is created on the first dispatched tool
  call. See [`SAFETY.md`](SAFETY.md#the-audit-chain).

Where a single process writes to `DATA_DIR`, verify the chain on the host from the **repository
root** after at least one tool call has run, replacing `~/.ewsmcp/audit` with `$DATA_DIR/audit` if
you set `DATA_DIR`:

```bash
python v5/scripts/verify_audit_chain.py ~/.ewsmcp/audit
```

When running from `v5/`, use `python scripts/verify_audit_chain.py ~/.ewsmcp/audit`.

Exit codes:

- `0` — every hash link, `prev` pointer, and `chain.state` head matches (`0 break(s)`).
- `1` — a break in the chain. With one process per `DATA_DIR`, the verifier detects accidental
  damage and edits that leave `chain.state` alone; it is not protection against someone who can
  write to `DATA_DIR`, because the chain has no key and can be recomputed. With several processes
  sharing one `DATA_DIR`, their interleaved records break the chain by themselves, so verify only
  where one process writes.
- `2` — directory not found (including before the first tool call runs) or contains no
  `audit-*.jsonl` files.

## The data directory

`DATA_DIR` defaults to `~/.ewsmcp` locally and `/data` inside the container image. It holds **mail
at rest** and **must never sit inside a cloud-synced folder** such as OneDrive, Dropbox, Google
Drive, or iCloud; synced paths are refused at startup unless `DATA_DIR_ALLOW_SYNCED=true`. Keep it
on an encrypted local disk with owner-only permissions.

| path inside `DATA_DIR` | what is in it | holds mail content | back up? |
|---|---|---|---|
| `memory/aliases.db` (plus `-wal`, `-shm`) | SQLite map between short aliases (`m1`, `d1`, `e1`, `p1`, `r1`, …) and raw Exchange `ItemId`s, `ChangeKey`s, `Internet-Message-Id`s, and person e-mail addresses. | Ids only (`ItemId`, `ChangeKey`, `Internet-Message-Id`), plus the e-mail addresses of people returned by `find_people`; no subjects or bodies. | **Optional.** Back up with `sqlite3 .backup` or while `ewsmcp` is stopped if external workflows store minted aliases; otherwise aliases are re-minted on future searches. |
| `audit/audit-YYYYMMDD.jsonl`, `audit/chain.state` | Append-only SHA-256 hash-chained tool audit log and chain head. | **No** — records the target ids of the call (`draft_id`, `event_id`, `ids`); recipients, subjects, and bodies are not recorded. | **Yes.** Operational audit trail; cannot be rebuilt from Exchange. |
| `cache/mirror.db` (plus `-wal`, `-shm`) | SQLite WAL mirror: cleaned message bodies, subjects, senders, recipients, FTS5 index, folder counts, 14-day calendar window, tasks, learned sender signature hashes, and `SyncFolderItems` tokens. | **Yes** — cleaned bodies, subjects, addresses, and event/task details. | **No.** Disposable cache; `ewsmcp` rebuilds it automatically from Exchange if deleted. |
| `cache/sync.lock` | OS lock file holding the sync-leader PID. | No. | **No.** Recreated automatically at startup. |
| `attachments/` | Created on demand by `get_attachment(mode="save")` and `get_message(format="eml")`. `.eml` files exported from Sent Items may include `Bcc` headers. | **Yes** — raw attachment bytes and full `.eml` messages. | **Only if needed** by local workflows; original items remain in Exchange unless deleted there. |

When `EWS_SEMANTIC_INDEX='pgvector'` is enabled, synced message text is also sent to
`EWS_SEMANTIC_OLLAMA_URL` and item ids and embeddings are stored outside `DATA_DIR` in PostgreSQL
table `ews.message_embeddings`. Back up your `.env` file or secrets separately in a password or
secrets manager, never in git.

## Updating

Container images are published to `ghcr.io/azizmazrou/ews-mcp` on `v4.5.*` tags and manual
`workflow_dispatch` runs when the `v5-tests` suite passes:

- **Always pin an exact `v4.5.*` tag**, such as `ghcr.io/azizmazrou/ews-mcp:v4.5.0a4`.
- **Never use `:latest` for 4.5.** The 4.5 line never publishes `:latest`; `:latest` and the
  version tags of the legacy line belong to the legacy 4.0 server (now in `Maintenance only`
  status). A moving `:v4.5` tag exists on GHCR, and production deployments should pin an immutable
  `v4.5.*` release tag so updates stay deliberate and easy to roll back.

Check [`UPGRADING.md`](UPGRADING.md) before upgrading for dependency or tool-surface changes between
releases.

### Local `stdio` or `http` install

From the **repository root**:

```bash
git fetch --tags origin
git checkout v4.5.0a4                       # replace with the target v4.5.* tag
.venv/bin/pip install -U "./v5[extract]"    # or "./v5" without PDF extraction
```

On Windows PowerShell, run `.venv\Scripts\pip install -U "./v5[extract]"` (or `"./v5"` without PDF
extraction). Restart your MCP client for `stdio` or your `ewsmcp` service for `http`, then check
`get_server_status.version` or `GET /version`.

### Docker Compose

Edit `.env` in the **repository root** and update `EWS_MCP_IMAGE` to the new `v4.5.*` tag, then run
from the **repository root**:

```bash
docker compose -f v5/docker-compose.example.yml --env-file .env pull
docker compose -f v5/docker-compose.example.yml --env-file .env up -d
```

### Standalone Docker

Pull the new `v4.5.*` image tag — or check out the tag and run `docker build -t ews-mcp:dev v5` from
the **repository root** — then stop the old container and start the new one with the same
`-v ewsmcp-data:/data` volume. Existing `aliases.db`, `audit/chain.state`, and `mirror.db`
`sync_state` tokens carry across 4.5 upgrades without re-downloading the mailbox. Afterwards, check
`GET /version` and `GET /readyz`.
