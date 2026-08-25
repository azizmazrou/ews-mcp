# ews-mcp 4.5 — Exchange (EWS) as a safe, fast MCP tool surface

An MCP server that turns an on-prem Exchange mailbox into a lean,
safety-gated tool surface for an LLM assistant: **32 tools**, alias-only
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
whatever network your machine has, **including a corporate VPN**. If
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
(23 read + draft tools; nothing can send), `SEND_ENABLED=false` until
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
  404 — this server implements no OAuth. That fixes the "no metadata,
  dead-end UI" case, but **don't set `MCP_API_KEY` on a connection a
  client drives without an explicit OAuth config** (LibreChat's plain
  `headers:`/`customUserVars` MCP connections, no `oauth:` block): such
  clients treat *any* 401 from the very first `/mcp` touch — which
  happens before your configured headers are attached — as "this needs
  OAuth" and don't reliably fall back afterward, even once discovery
  correctly reports "not supported" (confirmed live against LibreChat).
  `x-api-key` on the REST shim (`/api/tools/<name>`) is unaffected —
  that's a plain script/curl client, not doing OAuth auto-detection.
  For an MCP connection, rely on the network boundary instead (no
  `ports:` published — see multi-user mode below and Docker).

Docker (containerized HTTP mode — note a container only sees the
network of its host, not your workstation's VPN):

```bash
docker build -t ews-mcp:dev .
docker run --rm -p 8000:8000 --env-file .env -v ewsmcp-data:/data ews-mcp:dev
```

### Multi-user mode (one server, per-person Exchange identity)

For a shared deployment (e.g. an MCP gateway behind a chat platform like
LibreChat) where each person should act as *themselves* against
Exchange, not a shared service mailbox, set `EWS_MULTI_USER=true`.
`EWS_EMAIL`/`EWS_USERNAME`/`EWS_PASSWORD` are then ignored (and
`EWS_EMAIL` is no longer required at boot) — instead, **every** MCP and
REST call must carry:

```
X-EWS-Email: person@corp.example
X-EWS-Password: their-own-exchange-password
```

A request without both headers gets a clean `auth_failed` (401), not a
tool error. Credentials are never persisted: each (email, password) pair
resolves to a short-lived per-user Exchange session (`EWSGateway`),
cached in memory only for reuse across that user's own calls (bounded to
64 users, idle-evicted after 15 minutes; a changed password rebuilds
rather than reusing the old session) and torn down on eviction. Because
identity is now per-request, this mode also turns off everything that
would otherwise mix data between users: no cache mirror, no audit log,
no alias DB, no semantic index — every read is a live EWS call. HTTP
transport only (stdio is one already-authenticated local process, with
no per-request header to attach credentials to).

For LibreChat specifically, wire this up with
[`customUserVars`](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_servers)
in `librechat.yaml` so each signed-in user enters their own Exchange
email + password once (via LibreChat's own settings UI), which
LibreChat then substitutes into the headers on every call:

```yaml
mcpServers:
  exchange:
    type: streamable-http
    url: http://ews-mcp:8000/mcp
    headers:
      X-EWS-Email: "{{EWS_EMAIL}}"
      X-EWS-Password: "{{EWS_PASSWORD}}"
    customUserVars:
      EWS_EMAIL:
        title: "Exchange email"
        description: "Your Exchange mailbox address"
      EWS_PASSWORD:
        title: "Exchange password"
        description: "Your Exchange password (stored by LibreChat, sent only to this server)"
```

## Why it looks like this

- **Token economy.** One legacy detail call shipped 115 kB of duplicated
  raw HTML for a 150-char message. Here a search result is a ~60-token
  card, bodies are cleaned once at sync time (bilingual quoted-history +
  signature stripping), and raw HTML requires an explicit flag.
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
- **Never-exit boot.** Transports bind before any Exchange contact;
  `/livez` is up immediately, `/readyz` reports the warmup honestly, and
  the connection manager owns recovery.

## Configuration (env)

| Variable | Default | Meaning |
|---|---|---|
| `EWS_SERVER_URL` / `EWS_EMAIL` / `EWS_USERNAME` / `EWS_PASSWORD` | — | Exchange endpoint + credentials (auth auto-negotiation; never pinned) |
| `EWS_CAPABILITY_TIER` | `draft` | `read` ⊂ `draft` ⊂ `full` — above-tier tools are unregistered AND refused |
| `SEND_ENABLED` | `false` | Global send kill-switch (blocks every send-class tool) |
| `EWS_RECIPIENT_ALLOWLIST` / `EWS_RECIPIENT_DENYLIST` | — | Glob lists enforced on argument-borne AND draft-resolved recipients |
| `EWS_MAX_SENDS_PER_HOUR` | `10` | Send rate cap |
| `SEND_CONFIRM_SECRET` | per-process | HMAC secret for confirm tokens (set it to survive restarts) |
| `CONFIRM_TTL_SECONDS` | `600` | Confirm token lifetime |
| `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` / `MCP_API_KEY` | stdio | HTTP serving + bearer auth (all unused in stdio mode) |
| `EWS_MULTI_USER` | `false` | Per-request `X-EWS-Email`/`X-EWS-Password` credentials instead of a shared mailbox (HTTP transport only) — see "Multi-user mode" above |
| `DATA_DIR` | `~/.ewsmcp` | Aliases, audit chain, cache mirror. Absolute; cloud-synced paths are refused (`DATA_DIR_ALLOW_SYNCED=true` to override) |
| `EWS_CACHE_ENABLED` | `true` | The mirror; `false` = pure live EWS reads |
| `EWS_CACHE_FOLDERS` | `inbox,sent` | Delta-synced folders |
| `EWS_CACHE_SYNC_SECONDS` | `45` | Delta cadence (folder tree/calendar/tasks every 10 min) |
| `EWS_CACHE_WINDOW_DAYS` | `365` | Mirror backfill window |
| `EWS_CACHE_PURGE_ON_BOOT` | `false` | Admin path: wipe the mirror and resync |
| `EWS_SEMANTIC_INDEX` | `none` | `pgvector` enables the optional vector tier (+`find_similar`) |
| `EWS_SEMANTIC_PG_DSN` / `EWS_SEMANTIC_OLLAMA_URL` / `EWS_SEMANTIC_MODEL` | — | Vector tier wiring (requires `psycopg`, not a core dependency) |
| `EWS_TZ` | `Asia/Riyadh` | Server timezone for date grammar + display |

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
while warming), `GET /health` (tool count), `GET /metrics` (Prometheus,
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
