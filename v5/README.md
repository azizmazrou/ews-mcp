# ews-mcp 4.5 — Exchange (EWS) as a safe, fast MCP tool surface

An MCP server that turns an on-prem Exchange mailbox into a lean,
safety-gated tool surface for an LLM assistant: **28 tools**, alias-only
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
- Health: `GET /livez`, `/readyz`, `/health` — public. `/metrics` and
  `/openapi.json` carry no mailbox data and are reachable with the operator
  key alone (a Prometheus scraper has no user identity, and must not need
  one); everything else requires a caller.

Docker (containerized HTTP mode — note a container only sees the
network of its host, not your workstation's VPN):

```bash
docker build -t ews-mcp:dev .
docker run --rm -p 8000:8000 --env-file .env -v ewsmcp-data:/data ews-mcp:dev
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
  the connection manager owns recovery. In per-caller mode there is no
  shared connection to warm, so `/readyz` instead probes what needs no
  identity — including whether Exchange still offers a `Bearer` challenge,
  which is what would silently break every caller if OAuth were turned off
  on the EWS virtual directory.

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
| `DATA_DIR` | `~/.ewsmcp` | Aliases, audit chain, cache mirror. Absolute; cloud-synced paths are refused (`DATA_DIR_ALLOW_SYNCED=true` to override) |
| `EWS_CACHE_ENABLED` | `true` | The mirror; `false` = pure live EWS reads |
| `EWS_CACHE_FOLDERS` | `inbox,sent` | Delta-synced folders |
| `EWS_CACHE_SYNC_SECONDS` | `45` | Delta cadence (folder tree/calendar/tasks every 10 min) |
| `EWS_CACHE_WINDOW_DAYS` | `365` | Mirror backfill window |
| `EWS_CACHE_PURGE_ON_BOOT` | `false` | Admin path: wipe the mirror and resync |
| `EWS_SEMANTIC_INDEX` | `none` | `pgvector` enables the optional vector tier (+`find_similar`) |
| `EWS_SEMANTIC_PG_DSN` / `EWS_SEMANTIC_OLLAMA_URL` / `EWS_SEMANTIC_MODEL` | — | Vector tier wiring (requires `psycopg`, not a core dependency) |
| `EWS_TZ` | `Asia/Riyadh` | Server timezone for date grammar + display |

### Per-caller auth (`AUTH_MODE=oidc`)

`AUTH_MODE` defaults to `static`: one shared `MCP_API_KEY`, one mailbox,
exactly the behaviour documented above.

In `oidc` the server is an OAuth2 **resource server**. The caller presents the
**end user's** access token; the server verifies it against the IdP's JWKS
(`iss`, `aud` = this server's own resource id, `exp`/`nbf`, an algorithm
allowlist that excludes `HS*`) and derives the mailbox from a claim. HTTP
transport only — stdio cannot carry a bearer token, so that combination
refuses to boot.

Upstream, the verified token is exchanged **on behalf of** the caller for one
whose audience is Exchange, and that token opens **their own** mailbox with
`access_type=DELEGATE`. There is no service account, no `ApplicationImpersonation`
and no `target_mailbox` argument anywhere — one caller is one mailbox, enforced
by the absence of any way to name another.

Each caller gets their own pooled Exchange session (LRU + idle TTL) and their
own alias namespace under `DATA_DIR/users/<hash>/`. The EWS thread pool is
shared — it is a politeness budget against Exchange's throttling — with
`EWS_MAX_CONCURRENCY_PER_USER` as each caller's share of it.

Endpoints: `/.well-known/oauth-protected-resource` is public and advertises the
issuer; rejections carry `WWW-Authenticate: Bearer` with `invalid_token`
(re-acquire and retry) or `insufficient_scope` (stop).

| var | default | meaning |
|---|---|---|
| `AUTH_MODE` | `static` | `static` = today's single-mailbox server; `oidc` = per-caller identity |
| `AUTH_ISSUER` / `AUTH_AUDIENCE` / `AUTH_JWKS_URL` | — | Token issuer, **this** server's resource id, and the IdP's key set |
| `AUTH_ALLOWED_ALGS` | `RS256,ES256` | Signature allowlist; any `HS*` entry is refused at boot |
| `AUTH_MAILBOX_CLAIM` | `upn,email,preferred_username` | Ordered claim list; first non-empty wins |
| `AUTH_EMAIL_DOMAIN_ALLOWLIST` | — | Glob list; the blast-radius control (a valid guest identity must not reach Exchange) |
| `AUTH_CLOCK_SKEW_SECONDS` / `AUTH_JWKS_TTL_SECONDS` / `AUTH_JWKS_MIN_REFETCH_SECONDS` | `60` / `3600` / `60` | Validation leeway and JWKS caching (the refetch cooldown stops `kid` spraying from becoming a DoS relay onto the IdP) |
| `AUTH_UPSTREAM_MODE` | `obo` | How the EWS token is obtained: `obo` (recommended), `dual_header`, or `passthrough` (needs `AUTH_ALLOW_TOKEN_PASSTHROUGH=true`) |
| `AUTH_OBO_STYLE` | `aad` | `aad` or `rfc8693` — AD FS and Keycloak differ here |
| `AUTH_OBO_TOKEN_URL` / `AUTH_OBO_CLIENT_ID` / `AUTH_OBO_CLIENT_SECRET` / `AUTH_EWS_SCOPE` | — | This server's confidential client, used ONLY to exchange the caller's own token. Required in `oidc` |
| `AUTH_TOKEN_EXPIRY_MARGIN_SECONDS` / `AUTH_TOKEN_CACHE_MAX` | `120` / `500` | Exchanged-token cache (memory only — tokens are never written to `DATA_DIR`) |
| `AUTH_GATEWAY_POOL_MAX` / `AUTH_GATEWAY_IDLE_TTL_SECONDS` | `50` / `1800` | Per-caller Exchange connection pool |
| `EWS_MAX_CONCURRENCY_PER_USER` | `4` | Per-caller share of the server-wide `EWS_MAX_CONCURRENCY` budget |
| `DATA_DIR_NAMESPACE_SALT` | — | Required in `oidc`: salts the per-caller `DATA_DIR` namespace |
| `AUTH_DATA_DIR_NAMING` / `AUDIT_IDENTITY` | `hash` / `hash` | `smtp` writes real addresses into directory names / audit records — debugging only |

The cache mirror works in `oidc` too, but **per caller and warmed
differently**: no background loop could run, because delegated OAuth
deliberately leaves no long-lived credential. Instead each caller's mirror is
refreshed from inside their own requests, after the answer has been sent, with
the token already in hand — so FTS5 (Arabic) search, `find_similar` and
`waiting_on` work for people actively using the server, warming up over their
first few calls. A sync failure never fails a tool call; reads fall back to
live EWS.

`EWS_SEMANTIC_INDEX` must still be `none` in `oidc` (refused at boot): the
embeddings table has no tenant column yet.

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
