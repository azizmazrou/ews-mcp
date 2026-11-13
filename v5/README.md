# ews-mcp 4.5 — Exchange (EWS) as a safe, fast MCP tool surface

An MCP server that turns an on-prem Exchange mailbox into a lean,
safety-gated tool surface for an LLM assistant: **30 tools**, alias-only
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
(25 read + draft tools; nothing can send), `SEND_ENABLED=false` until
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
| `EWS_SERVER_URL` / `EWS_EMAIL` / `EWS_USERNAME` / `EWS_PASSWORD` | — | Exchange endpoint + credentials (auth auto-negotiation; blank `EWS_AUTH_TYPE_FORCE` is unset) |
| `EWS_AUTH_FAILURE_COOLDOWN_SECONDS` / `EWS_AUTH_MAX_REJECTIONS` | `900` / `3` | Lockout guard on HTTP 401 rejections: cooldown between probes (min `60`) and permanent per-process cap (`1`–`10`); values outside fail startup validation. `EWS_AUTH_MAX_REJECTIONS` counts rejected submissions (matching directory lockout counters), so concurrent requests already in flight when the first 401 arrives are each counted and a burst can reach the cap at once |
| `EWS_CAPABILITY_TIER` | `draft` | `read` ⊂ `draft` ⊂ `full` — above-tier tools are unregistered AND refused |
| `SEND_ENABLED` | `false` | Global send kill-switch (blocks every send-class tool) |
| `EWS_RECIPIENT_ALLOWLIST` / `EWS_RECIPIENT_DENYLIST` | — | Glob lists enforced on argument-borne AND draft-resolved recipients |
| `EWS_MAX_SENDS_PER_HOUR` | `10` | Send rate cap |
| `EWS_ATTACHMENT_DIR` | `""` (off) | Single directory from which `create_draft` / `add_draft_attachment` may read relative `path` attachments |
| `EWS_MAX_ATTACHMENT_MB` | `25` | Per-call attachment ceiling in MiB (`1`–`50`); HTTP request body limit is derived from this at start-up |
| `SEND_CONFIRM_SECRET` | per-process | HMAC secret for confirm tokens (set it to survive restarts) |
| `CONFIRM_TTL_SECONDS` | `600` | Confirm token lifetime |
| `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` / `MCP_API_KEY` | stdio | HTTP serving + bearer auth (all unused in stdio mode) |
| `DATA_DIR` | `~/.ewsmcp` | Aliases, audit chain, cache mirror. Absolute; cloud-synced paths are refused (`DATA_DIR_ALLOW_SYNCED=true` to override) |
| `EWS_CACHE_ENABLED` | `true` | The mirror; `false` = pure live EWS reads |
| `EWS_CACHE_FOLDERS` | `inbox,sent` | Delta-synced folders |
| `EWS_CACHE_SYNC_SECONDS` | `45` | Delta cadence (folder tree/calendar/tasks every 10 min) |
| `EWS_CACHE_WINDOW_DAYS` | `365` | Mirror backfill window |
| `EWS_CACHE_PURGE_ON_BOOT` | `false` | Admin path: wipe the mirror and resync |
| `EWS_SEMANTIC_INDEX` | `none` | `pgvector` enables the optional vector tier (+`find_similar`) |
| `EWS_SEMANTIC_PG_DSN` / `EWS_SEMANTIC_OLLAMA_URL` / `EWS_SEMANTIC_MODEL` | — | Vector tier wiring (requires `psycopg`, not a core dependency) |
| `EWS_TZ` | `UTC` | Server timezone for date grammar + display |

## The send flow (two-phase, content-bound)

```text
create_draft(mode="reply", reply_to="m12", body="…")
  → {draft_id: "d1", preview, note: "saved as draft — NOT sent"}
send_draft(draft_id="d1")
  → phase 1: fetches the draft, returns its REAL recipients/subject/body
    snippet + confirm_token bound to that content (nothing sent)
send_draft(draft_id="d1", confirm_token="…")
  → phase 2: REFETCHES the draft, verifies the content still matches,
    sends once (tokens are single-use; editing the draft in between
    invalidates the token)
```

## Health & operations

`GET /livez` (process up), `GET /readyz` (connection state, honest 503
while warming or `auth_failed` when credentials are rejected), `GET /health` (tool count), `GET /metrics` (Prometheus,
bearer-authenticated), `get_server_status` tool (connection, tier,
kill-switch, cache watermarks, sync status — works while cold and over
stdio too). Audit chain:
`python scripts/verify_audit_chain.py $DATA_DIR/audit`.

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
