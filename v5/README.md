# ews-mcp 4.5 — Exchange (EWS) as a safe, fast MCP tool surface

An MCP server that turns an on-prem Exchange mailbox into a lean,
safety-gated tool surface for an LLM assistant: **33 tools**, alias-only
ids, token-lean DTOs, a local cache mirror with Arabic-correct full-text
search, and a two-phase confirm flow that makes autonomous sending
tamper-evident.

> The `v5/` directory name is an internal path; the release line is
> **4.5.x** (`ghcr.io/…:v4.5*`). Architecture: [DESIGN.md](DESIGN.md).
> Full API reference: [docs/API.md](docs/API.md).

## Quick start — run it locally over stdio (no Docker)

stdio is the default transport and the simplest way to use this server:
your MCP client (Claude Code, Claude Desktop, or any other) starts
`ewsmcp` as a child process and talks to it over stdin/stdout. There is
no port, no API key, and nothing listening on the network — and because
it runs as a normal process on your machine, it reaches Exchange through
whatever network your machine has, **including a workstation VPN**. If
your Exchange endpoint is only reachable from your workstation, this is
the mode you want; a container or a remote host would not have that
route.

**1. Install** (Python 3.11+):

```bash
git clone https://github.com/azizmazrou/ews-mcp && cd ews-mcp
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install ./v5
```

You now have an `ewsmcp` command inside the venv:
`.venv/bin/ewsmcp` — on Windows `.venv\Scripts\ewsmcp.exe`. Use that
**absolute path** in the client configs below.

**2. Connect Claude Code** (one command, then restart the session):

```bash
claude mcp add exchange \
  -e EWS_SERVER_URL="https://mail.example.com/EWS/Exchange.asmx" \
  -e EWS_EMAIL="user@example.com" \
  -e EWS_USERNAME="user" \
  -e EWS_PASSWORD="…" \
  -- /absolute/path/to/.venv/bin/ewsmcp
```

On Windows, write it on one line and point at `ewsmcp.exe`. Check with
`claude mcp list` — the server should show as connected.

**3. Or Claude Desktop** — add to `claude_desktop_config.json`
(Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "exchange": {
      "command": "C:\\path\\to\\.venv\\Scripts\\ewsmcp.exe",
      "env": {
        "EWS_SERVER_URL": "https://mail.example.com/EWS/Exchange.asmx",
        "EWS_EMAIL": "user@example.com",
        "EWS_USERNAME": "user",
        "EWS_PASSWORD": "…"
      }
    }
  }
}
```

Any other MCP client works the same way: command = the `ewsmcp` path,
credentials in `env`. Prefer a file? Copy [`.env.example`](.env.example)
to `.env` in the directory you launch `ewsmcp` from — it auto-loads.

That is the whole setup. The defaults are safe: capability tier `draft`
(28 read + draft tools; nothing can send), `SEND_ENABLED=false` until
you flip it, and mail-at-rest (aliases, audit chain, cache) goes to
`~/.ewsmcp`. The server **refuses cloud-synced folders** for that data —
if your home directory lives in OneDrive/Dropbox/iCloud, set `DATA_DIR`
to a plain local path.

**First things to try in a chat:**

- "What's in my inbox this morning?" → `get_mailbox_overview`
- "Find the last message from the finance team" → `search_messages`
- "Show me that whole conversation" → `get_thread`
- "Draft a short reply to m3 saying I'll confirm tomorrow" →
  `create_draft` (saved as a draft, never sent)
- "What's on my calendar this week?" → `list_events`

Ids like `m3` / `e1` are the server's short aliases — the assistant uses
them exactly as returned; raw Exchange ids never appear.

## What the tools cover

`EWS_CAPABILITY_TIER` exposes **17** tools at `read`, **28** at `draft`
(the default), and **33** at `full`, plus `find_similar` at `read` when
`EWS_SEMANTIC_INDEX=pgvector`. Full parameter tables live in
[`docs/API.md`](docs/API.md).

- **Mail read** (`read`, 7 tools): `list_folders` (includes raw EWS
  `folder_class`), `search_messages` (local FTS or live EWS, with
  `folder="f:all"`, `recursive`, `categories`, and `flag_complete`),
  `check_messages` (checks a list of subjects or senders against one
  folder in one call), `get_message` and `get_attachment` (both detect
  PKCS#7 S/MIME attachments and report `smime_type`), `get_thread`, and
  `get_mailbox_overview` — plus `find_similar` when the semantic index
  is enabled.
- **Calendar and people** (`read`, 7 tools): `list_events`, `get_event`,
  `check_availability`, `find_people`, `get_contact`,
  `get_oof_settings`, and `get_server_status`.
- **Tasks** (`list_tasks` and `waiting_on` at `read`; `update_task` at
  `draft`): task inspection, sent threads awaiting a reply, and task
  updates or completion.
- **Writes** (8 tools added at `draft`, 5 more at `full`): at `draft`,
  `create_draft` (with `attachments`), `update_draft`,
  `add_draft_attachment` (bytes via `content_base64` or a relative
  `path` inside `EWS_ATTACHMENT_DIR`), `delete_draft`,
  `update_messages`, `move_messages`, and `create_event` /
  `update_event` (including `recurrence` and `reminder_minutes`;
  notifying attendees also requires `SEND_ENABLED=true`); at `full`,
  `send_draft`, `respond_to_event`, `cancel_event`, `delete_messages`,
  and `set_oof`.
- **Inbox rules** (`list_rules` at `read`; `save_rule` and `delete_rule`
  at `draft`, always two-phase confirmed): server-side filing, category,
  read-state, and importance rules (`r`-alias ids, strict mailbox-internal
  action allow-list that never sends or deletes mail, `editable: false`
  protection for complex rules, and `outlook_only_rules` warnings).

## HTTP transport (only when the server runs on another machine)

If the server runs where the client isn't (a home server, for example),
serve HTTP instead:

```bash
MCP_TRANSPORT=http MCP_PORT=8000 MCP_API_KEY=<long-random-string> ewsmcp
```

- MCP endpoint: `http://host:8000/mcp` (Streamable HTTP, the modern
  replacement for SSE). Clients that only speak stdio can bridge:
  `npx mcp-remote http://host:8000/mcp --header "Authorization: Bearer <key>"`.
- Plain REST for scripts: `POST /api/tools/<name>` with an `x-api-key`
  header; OpenAPI at `/openapi.json`.
- Health: `GET /livez`, `/readyz`, `/health`.
- OAuth discovery paths (`/.well-known/oauth-*`, `/register`) always
  return 404 — this server implements no OAuth, so clients fall back to
  configured headers instead of treating a 401 as missing OAuth metadata.
  MCP clients that probe `/mcp` unauthenticated before attaching
  configured headers may still interpret an initial 401 as an OAuth
  challenge; for those clients, omit `MCP_API_KEY` and restrict network
  access instead (loopback binding or an internal container network).
  `x-api-key` on `/api/tools/<name>` is unaffected.

Docker (containerized HTTP mode — note a container only sees the
network of its host, not your workstation's VPN; see
[`docker-compose.example.yml`](docker-compose.example.yml) for a
loopback-bound, read-tier Compose setup):

```bash
docker build -t ews-mcp:dev .
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env -v ewsmcp-data:/data ews-mcp:dev
```

## Why it looks like this

- **Token economy.** Raw HTML bodies often exceed 100 kB for a short
  message. Here a search result is a ~60-token card, bodies are cleaned
  once at sync time (bilingual quoted-history + signature stripping), and
  raw HTML requires an explicit flag.
- **Ids the model can actually copy.** Raw EWS ids are ~150 chars of
  case-sensitive base64 that change when items move. Tools emit short
  aliases (`m12`, `e3`) that survive moves and restarts.
- **Safety by declaration.** Handlers contain zero policy; ONE dispatcher
  chain enforces kill-switch → tier → recipient guard → content-bound
  two-phase confirm → rate cap. Defaults are safe: sends disabled,
  draft tier.
- **Cache-first reads.** A background delta-sync (native EWS
  `SyncFolderItems`) keeps a per-mailbox SQLite mirror; warm reads answer
  in milliseconds with `{"source": "cache", "as_of": …}` provenance and
  fall back to live EWS transparently. Arabic searches match across
  orthographic variants (alef/hamza forms, teh marbuta, diacritics,
  Arabic-Indic digits).
- **Never-exit boot, lockout-safe auth.** Transports bind before any
  Exchange contact; `/livez` is up immediately, `/readyz` reports the
  warmup honestly, and the connection manager owns recovery. If Exchange
  answers HTTP 401, `RejectingFaultTolerance` aborts on the first response
  and the gateway's authentication circuit breaker enforces a 15-minute
  cooldown and a 3-rejection process cap (`state="auth_failed"`) so a
  wrong password never loops into a directory account lockout.

## Configuration (env)

| Variable | Default | Meaning |
|---|---|---|
| `EWS_SERVER_URL` | *(required)* | Exchange EWS endpoint URL (for example `https://mail.example.com/EWS/Exchange.asmx`) |
| `EWS_EMAIL` | *(required)* | Primary SMTP address of the mailbox; also used as login name when `EWS_USERNAME` is unset |
| `EWS_USERNAME` | `None` | Exchange login username; falls back to `EWS_EMAIL` when unset |
| `EWS_PASSWORD` | `None` | Exchange account password (treated as `""` when unset) |
| `EWS_AUTH_TYPE_FORCE` | `None` | Pin `basic`, `ntlm`, or `digest` to skip auth probing; unset or blank auto-negotiates |
| `EWS_INSECURE_SKIP_VERIFY` | `false` | Disable TLS certificate verification on Exchange HTTPS traffic |
| `EWS_TZ` | `UTC` | IANA timezone name for date grammar, recurring series, and formatted timestamps |
| `REQUEST_TIMEOUT` | `30` | Per-request Exchange HTTP timeout in seconds |
| `EWS_WARMUP_MAX_BACKOFF_SECONDS` | `300` | Maximum exponential-backoff delay in seconds during warmup and reconnects |
| `EWS_HEARTBEAT_SECONDS` | `600` | Seconds between live Exchange liveness probes while warm (`<= 0` disables) |
| `EWS_RETRY_MAX_WAIT_SECONDS` | `300` | Max wait in seconds for exchangelib retries on non-401 transient errors (503, SOAP busy) |
| `EWS_AUTH_FAILURE_COOLDOWN_SECONDS` | `900` | Lockout-guard cooldown in seconds (min `60`) after an HTTP 401 before one recovery probe may run |
| `EWS_AUTH_MAX_REJECTIONS` | `3` | Rejected credential submissions (`1`–`10`, including in-flight concurrent calls) before staying open until restart |
| `EWS_MAX_CONCURRENCY` | `4` | Worker thread-pool size for blocking Exchange calls |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive `upstream_unavailable`, `upstream_error`, or `throttled` errors that trip the dispatcher circuit |
| `CIRCUIT_OPEN_SECONDS` | `60` | Seconds the dispatcher circuit fast-fails with `upstream_unavailable` once tripped |
| `EWS_CAPABILITY_TIER` | `draft` | `read` ⊂ `draft` ⊂ `full` — above-tier tools are unregistered and refused at dispatch |
| `SEND_ENABLED` | `false` | Global send kill-switch (blocks `send`-class tools and attendee-notifying calendar writes) |
| `SEND_CONFIRM_SECRET` | `None` | HMAC secret for confirm tokens; random per-process when unset (set to survive restarts) |
| `CONFIRM_TTL_SECONDS` | `600` | Confirm token lifetime in seconds |
| `EWS_RECIPIENT_ALLOWLIST` | `""` | Comma-separated address globs permitted on drafts, events, and `send_draft` (empty allows all) |
| `EWS_RECIPIENT_DENYLIST` | `""` | Comma-separated address globs blocked before the allowlist on drafts, events, and `send_draft` |
| `EWS_MAX_SENDS_PER_HOUR` | `10` | Rolling 1-hour send rate cap (`<= 0` disables) |
| `EWS_ATTACHMENT_DIR` | `""` | Single directory from which `create_draft` / `add_draft_attachment` may read relative `path` files (`""` = off) |
| `EWS_MAX_ATTACHMENT_MB` | `25` | Per-call attachment ceiling in MiB (`1`–`50`); HTTP request body limit derives from this at startup |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `MCP_HOST` | `127.0.0.1` | Bind address in `http` mode (unused in `stdio` mode) |
| `MCP_PORT` | `8000` | Bind port in `http` mode (unused in `stdio` mode) |
| `MCP_API_KEY` | `None` | Optional bearer / `x-api-key` token required on non-health HTTP endpoints |
| `LOG_LEVEL` | `INFO` | Python stderr and uvicorn log level |
| `DATA_DIR` | `""` | Local directory for aliases, audit chain, and mirror (`""` resolves to `~/.ewsmcp`); cloud-synced paths are refused |
| `DATA_DIR_ALLOW_SYNCED` | `false` | Explicit opt-out of the cloud-synced folder check on `DATA_DIR` |
| `EWS_CACHE_ENABLED` | `true` | Enable the SQLite mirror (`false` = live EWS reads only) |
| `EWS_CACHE_FOLDERS` | `inbox,sent` | Comma-separated mail folders kept warm via `SyncFolderItems` delta sync |
| `EWS_CACHE_SYNC_SECONDS` | `45` | Mail delta-sync cadence in seconds (min `5`) |
| `EWS_CACHE_HIERARCHY_SECONDS` | `600` | Slow-lane cadence in seconds (min `60`) for the folder tree, 14-day calendar window, and tasks |
| `EWS_CACHE_WINDOW_DAYS` | `365` | Mirror backfill window in days |
| `EWS_CACHE_PURGE_ON_BOOT` | `false` | Admin path: wipe the mirror tables on startup and resync |
| `EWS_SEMANTIC_INDEX` | `none` | `none` or `pgvector` (enables `find_similar` and `search_messages(mode="semantic")`) |
| `EWS_SEMANTIC_PG_DSN` | `None` | PostgreSQL DSN for `pgvector` semantic indexing (requires `psycopg`, not a core dependency) |
| `EWS_SEMANTIC_OLLAMA_URL` | `http://localhost:11434` | Ollama base URL for `pgvector` embeddings |
| `EWS_SEMANTIC_MODEL` | `bge-m3` | Embedding model requested from Ollama when `EWS_SEMANTIC_INDEX=pgvector` |
| `DEFAULT_PAGE_SIZE` | `20` | Validated at startup (`<= 50`); tool handlers currently set their own `limit` defaults |
| `BODY_MAX_CHARS` | `4000` | Maximum cleaned-body characters returned by `get_message` |

## The send flow (two-phase, content-bound)

```text
create_draft(mode="reply", reply_to="m12", body="…", attachments=[…])
  → {draft_id: "d1", preview, note: "saved as draft — NOT sent"}
send_draft(draft_id="d1")
  → phase 1: fetches the draft, returns its resolved to/cc/bcc, subject,
    body_snippet, attachment_count, and attachments, plus a single-use
    confirm_token bound to subject, recipients, body, and attachments
    (ids, names, sizes) — nothing sent
send_draft(draft_id="d1", confirm_token="…")
  → phase 2: verifies and consumes the token, then under the per-draft
    lock fetches the draft once, re-checks its content hash against the
    verified hash, and sends that same object (any change after the
    preview fails with confirm_invalid)
```

## Health & operations

`GET /livez` (process up), `GET /readyz` (connection state, honest 503
while warming or `auth_failed` when credentials are rejected),
`GET /health` (tool count), `GET /metrics` (Prometheus,
bearer-authenticated), `get_server_status` tool (connection, tier,
kill-switch, cache watermarks, sync status — works while cold and over
stdio too). Audit chain:
`python scripts/verify_audit_chain.py $DATA_DIR/audit`.

### When Exchange rejects the credentials (`auth_failed`)

- **What the server does.** Each rejected password submission counts
  toward directory account-lockout policies. On the first HTTP `401`
  (including `currently locked out` responses),
  `RejectingFaultTolerance` fails immediately without retrying and
  `AuthBreaker` opens for `EWS_AUTH_FAILURE_COOLDOWN_SECONDS` (default
  `900` s), blocking every Exchange path — tool calls, warmup,
  heartbeat, and background sync. After `EWS_AUTH_MAX_REJECTIONS`
  rejected submissions (default `3`, counting requests already in flight
  when the first `401` lands), the breaker stays open until the process
  restarts.
- **Where it shows.** `GET /readyz` returns HTTP `503` with
  `status: "auth_failed"` and `connection` fields
  `state: "auth_failed"`, `auth_rejections`, `auth_permanent`,
  `next_retry_in_s` (`null` when permanent), and `last_error`.
  `get_server_status` returns the same `connection` object over stdio
  and HTTP, and `GET /metrics` exports `ewsmcp_auth_failed 1` and
  `ewsmcp_auth_rejections`. Exchange-backed tools return
  `code: "auth_failed"` with `hint`: *"Retrying will not help:
  credentials are read once at startup and repeated failures risk
  locking the directory account. Ask the operator to check the
  configured address/username and password, update the configuration,
  and restart the server."*
- **What to do.** Fix `EWS_EMAIL`, `EWS_USERNAME`, or `EWS_PASSWORD` in
  the client `env` block or `.env`, then restart `ewsmcp`. Retrying tool
  calls does not reload credentials or clear a permanent trip.
- **Diagnosis.** On an HTTP `401`, the server logs a warning naming the
  authentication schemes Exchange offered next to the configured setting
  (`Exchange HTTP 401 rejection: WWW-Authenticate schemes […];
  ews_auth_type_force=…`). When auto-negotiation picks a scheme the
  server rejects, set `EWS_AUTH_TYPE_FORCE` to `basic`, `ntlm`, or
  `digest` and restart.

## Several clients on one machine

Each `ewsmcp` stdio process with caching enabled starts its own
background `SyncEngine` against `DATA_DIR`; there is no cross-process
sync lease yet. If multiple clients on the same machine use one mailbox,
either run a single HTTP server on loopback (`MCP_TRANSPORT=http`,
`MCP_HOST=127.0.0.1`) and point every client at it, or set a separate
`DATA_DIR` per stdio instance.

## Upgrading from 4.5.0a1

- `EWS_TZ` now defaults to `UTC`; set `EWS_TZ` to an IANA zone name if
  you relied on the previous default.
- The host-specific Compose file is replaced by
  [`docker-compose.example.yml`](docker-compose.example.yml).
- `mcp>=1.30` and `exchangelib==5.6.0` are now required (`pip install -U ./v5`).
- Five tools were added since 4.5.0a1 — `check_messages`,
  `add_draft_attachment`, `list_rules`, `save_rule`, and `delete_rule` —
  bringing the default surface from 28 to **33** tools.
- Multi-folder `search_messages` (`folder="f:all"`, `recursive=true`) is
  bounded to at most `200` folders per call (extras reported in
  `folders_skipped`) and `offset + limit` at most `500`.
- `search_messages` with `flag_complete=false` returns
  `total_available: null`; check `next_offset` for further pages.
- `get_message` includes `flag_complete` only when read live
  (`fresh=true`, or when uncached).
- Connection state `auth_failed` is now reported on `/readyz`,
  `/metrics`, and `get_server_status` when Exchange rejects credentials.

## Development

```bash
pip install -e .[dev]
python -m pytest tests -q          # the full suite, no Exchange needed
python -m ruff check .
python scripts/boot_smoke.py full  # end-to-end boot against a dead endpoint
python scripts/dump_tool_table.py --check   # docs vs registry drift gate
```

## Example assistant skill

`examples/skills/exchange-assistant/` shows how a Claude skill composes
this tool surface (morning overview → triage → reply-draft with the
two-phase confirm). It is deliberately generic — judgment lives in the
calling assistant, the server stays a data plane.
