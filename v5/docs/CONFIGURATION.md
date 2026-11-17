# Configuration

Every setting in `ews-mcp` comes from an environment variable and defaults to a safe posture:
`draft` tier, sending off, local `stdio` transport, and local-only storage. Only `EWS_SERVER_URL`
and `EWS_EMAIL` are required to start the server.

## How settings are read

`ewsmcp` reads settings once at process startup in `ewsmcp/config.py`. Starting without
`EWS_SERVER_URL` or `EWS_EMAIL` exits immediately with `ValidationError` (`Field required`). After
you change any setting, restart the server for the new value to take effect.

Settings come from two places, with process environment variables taking precedence over `.env`:

- **Process environment variables.** Names are **case-insensitive**, so `EWS_SERVER_URL` and
  `ews_server_url` mean the same setting. Unknown variables are ignored.
- **A `.env` file in the working directory.** If a UTF-8 `.env` file exists in the directory
  `ewsmcp` starts from, the server loads it automatically. Copy [`v5/.env.example`](../.env.example)
  to `.env` as a starting point, keep values unquoted (`KEY=value`) if you also pass `.env` to
  `docker run --env-file`, restrict its file permissions, and never commit it to version control.
- **Direct environment fallbacks.** Two helpers also fall back to `os.environ` when called outside
  `Settings`: `SEND_CONFIRM_SECRET` in `ewsmcp/confirm.py` and `EWS_KEEP_DUMPABLE` in
  `ewsmcp/extract_worker.py`.

When an MCP client runs `ewsmcp` locally over `stdio`, you do not need a `.env` file. Most clients
accept an `"env"` object inside their server definition, or `-e KEY=VALUE` flags on the command
line, and pass those variables directly to the `ewsmcp` child process:

```json
{
  "mcpServers": {
    "exchange": {
      "command": "/path/to/.venv/bin/ewsmcp",
      "env": {
        "EWS_SERVER_URL": "https://mail.example.com/EWS/Exchange.asmx",
        "EWS_EMAIL": "you@example.com",
        "EWS_USERNAME": "you",
        "EWS_PASSWORD": "change-me"
      }
    }
  }
}
```

## Connecting to Exchange

These settings tell `ewsmcp` which Exchange Web Services endpoint and mailbox to open, how to
authenticate, and which timezone to use for dates. Autodiscover is disabled; the server connects
only to the URL in `EWS_SERVER_URL`.

| setting | default | limits | what it does |
|---|---|---|---|
| `EWS_SERVER_URL` | `(required)` | - | Full EWS endpoint URL, such as `https://mail.example.com/EWS/Exchange.asmx`. |
| `EWS_EMAIL` | `(required)` | - | Primary SMTP address of the mailbox (`you@example.com`). Used as login name when `EWS_USERNAME` is unset, to bind confirm tokens, and to flag external recipients. |
| `EWS_USERNAME` | `None` | - | Exchange login username, such as `EXAMPLE\you` or `you`. Falls back to `EWS_EMAIL` when `None`. |
| `EWS_PASSWORD` | `None` | - | Exchange password. Treated as `""` when `None`. |
| `EWS_AUTH_TYPE_FORCE` | `None` | - | Pin `basic`, `ntlm`, or `digest` to skip the unauthenticated scheme probe. Case-insensitive; blank strings become `None`, which auto-negotiates. |
| `EWS_INSECURE_SKIP_VERIFY` | `False` | - | Disable TLS certificate verification on HTTPS traffic to Exchange and log a startup warning. |
| `EWS_TZ` | `'UTC'` | - | IANA timezone name such as `UTC` for date arguments (`today`, `+Nd`, `YYYY-MM-DD`, and ISO date-times), recurring calendar rules, and returned timestamps. |
| `REQUEST_TIMEOUT` | `30` | - | Per-request HTTP timeout in seconds for calls to Exchange. |

**When to change it:**

- Set `EWS_USERNAME` whenever your directory login differs from your SMTP address, such as
  `DOMAIN\username` for NTLM.
- Set `EWS_AUTH_TYPE_FORCE` if Exchange offers several `WWW-Authenticate` schemes and
  auto-negotiation picks one the server rejects, or if a proxy drops unauthenticated probe
  requests. See [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#exchange-rejects-the-credentials).
- Set `EWS_TZ` to the mailbox user's IANA timezone so `"today"`, `YYYY-MM-DD` dates, and recurring
  series align with their wall clock.
- Leave `EWS_INSECURE_SKIP_VERIFY` at `False` outside an isolated test lab.

## Reliability

These settings govern background warmup, liveness probes, worker concurrency, transient retries,
and the two circuit breakers. Startup does not wait for Exchange to connect, and recovery runs in
the background without exiting the process.

| setting | default | limits | what it does |
|---|---|---|---|
| `EWS_WARMUP_MAX_BACKOFF_SECONDS` | `300` | - | Maximum exponential-backoff ceiling in seconds during background warmup and reconnects, starting from `1` s with full jitter. |
| `EWS_HEARTBEAT_SECONDS` | `600` | - | Seconds between background `GetFolder` liveness probes while warm. Values `<= 0` disable the heartbeat loop. |
| `EWS_RETRY_MAX_WAIT_SECONDS` | `300` | - | Maximum retry window in seconds for `exchangelib` on non-401 transient errors such as HTTP 503 or SOAP `ErrorServerBusy`. HTTP 401 never retries. |
| `EWS_AUTH_FAILURE_COOLDOWN_SECONDS` | `900` | ge 60 | Seconds the authentication lockout guard blocks all Exchange calls after an HTTP 401 before allowing one recovery probe. |
| `EWS_AUTH_MAX_REJECTIONS` | `3` | ge 1, le 10 | Rejected credential submissions since the last successful login, including concurrent in-flight calls, before the lockout guard stays open until restart. |
| `EWS_MAX_CONCURRENCY` | `4` | - | Worker thread-pool size for blocking Exchange calls; clamped at runtime to at least `1`. |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | - | Consecutive tool calls failing with `upstream_unavailable`, `upstream_error` (including unexpected local exceptions), or `throttled` before the dispatcher opens its circuit breaker. |
| `CIRCUIT_OPEN_SECONDS` | `60` | - | Seconds the dispatcher circuit breaker stays open once tripped, refusing Exchange-backed tools with `upstream_unavailable` and `retry_after_s`. |

#### `CIRCUIT_OPEN_SECONDS`

- While the circuit breaker is open, tools that contact Exchange are refused immediately with
  `upstream_unavailable` and `retry_after_s`.
- `get_server_status`, `waiting_on`, and `find_similar` do not contact Exchange and still answer
  while the breaker is open.
- During an open circuit, `get_server_status` reports `connection.circuit_open: true` and
  `retry_after_s`; both keys are absent otherwise.

**When to change it:**

- Raise `EWS_AUTH_FAILURE_COOLDOWN_SECONDS` or lower `EWS_AUTH_MAX_REJECTIONS` to `1` or `2` when
  your directory enforces a strict failed-login lockout threshold. See
  [`SAFETY.md`](SAFETY.md#the-lockout-guard).
- Lower `EWS_MAX_CONCURRENCY` if Exchange returns `throttled` errors, or raise it slightly when one
  HTTP server handles heavy parallel read traffic.
- Shorten `EWS_RETRY_MAX_WAIT_SECONDS` if you want failing live calls to return `throttled` or
  `upstream_unavailable` faster during an Exchange outage.

## Safety

These settings enforce what the assistant is allowed to see, write, attach, and send. Every gate
runs in the central dispatcher before a tool handler executes; full details live in
[`SAFETY.md`](SAFETY.md).

| setting | default | limits | what it does |
|---|---|---|---|
| `EWS_CAPABILITY_TIER` | `'draft'` | - | One of `read` (18 tools), `draft` (29 tools), or `full` (34 tools), plus `find_similar` (+1) when semantic search is enabled; tools above the tier are not registered. |
| `SEND_ENABLED` | `False` | - | Global send kill-switch. While `False`, refuses every call that sends mail or meeting notices with `kill_switch` at phase one, before any confirmation token is issued (see table below). |
| `SEND_CONFIRM_SECRET` | `None` | - | HMAC-SHA256 secret for two-phase `confirm_token` signing. Uses a random per-process secret when `None`. |
| `CONFIRM_TTL_SECONDS` | `600` | - | Lifetime of a two-phase `confirm_token` in seconds. |
| `EWS_RECIPIENT_ALLOWLIST` | `(empty)` | - | Comma-separated `fnmatch` address globs allowed on recipient-bearing tools; stored event attendees with no address fail with `recipient_blocked`. Empty permits all addresses not matched by `EWS_RECIPIENT_DENYLIST`. |
| `EWS_RECIPIENT_DENYLIST` | `(empty)` | - | Comma-separated `fnmatch` address globs blocked before the allowlist is checked; when set, stored event attendees with no address also fail with `recipient_blocked`. |
| `EWS_MAX_SENDS_PER_HOUR` | `10` | - | Rolling 3600-second in-memory per-process cap on every confirmed call that sends; returns `rate_capped` when full. Values `<= 0` disable the cap. |
| `EWS_ATTACHMENT_DIR` | `(empty)` | - | Single local directory from which `create_draft` and `add_draft_attachment` may read relative `path` files. Empty disables file-path attachments. |
| `EWS_MAX_ATTACHMENT_MB` | `25` | ge 1, le 50 | Per-call attachment and `.eml` export ceiling in MiB. Also sizes the HTTP request body limit at startup as `bytes * 4 // 3 + 1 MiB`. |
| `EWS_KEEP_DUMPABLE` | `False` | - | When `False` on Linux, marks the server process non-dumpable (`PR_SET_DUMPABLE=0`) before starting attachment-extraction workers. Set `True` only for `ptrace` or core-dump debugging. |

#### `EWS_CAPABILITY_TIER`

- Exposes `18` tools at `read`, `29` at `draft`, and `34` at `full`, plus `+1` (`find_similar`)
  when semantic search is enabled.
- A tool above the server's tier is not registered: it is absent from the tool list, and calling it
  returns `validation` with `Unknown tool: <name>` (HTTP 404 over REST).
- `tier_blocked` (HTTP 403) is a second check inside the dispatcher that a client does not normally
  reach.

#### `EWS_MAX_SENDS_PER_HOUR`

- Counts every confirmed call that sends in a rolling 3600-second window: `send_draft`,
  `respond_to_event`, `set_oof`, invitations, update notices, and cancellations.
- Tracked in memory per process, so a restart resets the counter; values `<= 0` disable the cap.
- Checked at phase two after `confirm_token` is verified and before it is consumed, returning
  `rate_capped` with `retry_after_s >= 1` so a capped call keeps its token until expiry.

Ordinary mail leaves the mailbox only through `send_draft`. Meeting mail — invitations, update
notices, responses, and cancellations — is sent by the calendar tools:

| tool | what it can send | minimum tier | refused while `SEND_ENABLED=false` | two-phase confirmation | counted by `EWS_MAX_SENDS_PER_HOUR` | recipients checked against the lists |
|---|---|---|---|---|---|---|
| `send_draft` | the draft | `full` | yes (phase one) | always | yes | arguments and the stored draft |
| `respond_to_event` | a meeting response | `full` | yes (phase one) | always | yes | n/a |
| `set_oof` | automatic replies, when enabled | `full` | yes (phase one) | always | yes | n/a |
| `create_event` with `send_invitations=true` | invitations | `draft` | yes (phase one) | yes | yes | the `attendees` argument |
| `update_event` with `notify_attendees=true` | update notices | `draft` | yes (phase one) | yes | yes | the attendees stored on the event |
| `cancel_event` on a meeting with attendees | cancellation notices with the caller's `message` | `full` | yes (phase one) | always | yes | the attendees stored on the event |
| `cancel_event` on an appointment without attendees | nothing (moves it to Deleted Items) | `full` | no | always | no | n/a |
| `delete_messages` | nothing | `full` | no | for `disposition="permanent"` | no | n/a |

While `SEND_ENABLED` is `false`, every call in this table that sends is refused with `kill_switch`
at phase one, before any confirmation token is issued. Invitations and update notices need only
tier `draft`, `SEND_ENABLED=true`, and a confirmation. At phase two, `confirm_token` is verified
first (`confirm_invalid` when wrong, expired, stale, or already used), then `EWS_MAX_SENDS_PER_HOUR`
is checked (`rate_capped` with `retry_after_s >= 1`), and only then is `confirm_token` consumed —
so a `rate_capped` call keeps its token and succeeds once the hour has room if the token has not
expired.

### `SEND_CONFIRM_SECRET` and restarts

Two-phase confirmation mints a stateless HMAC-SHA256 token in `ewsmcp/confirm.py`. Each token binds
the mailbox, tool name, target id, previewed content hash, and expiry timestamp.

- **When `SEND_CONFIRM_SECRET` is `None`:** `ewsmcp` generates a random 32-byte secret in memory at
  startup. Confirmations work within that process, **and restarting the server invalidates every
  pending `confirm_token`**. Phase 2 then fails with `confirm_invalid` (`bad_signature`), so the
  caller must run phase 1 again for a fresh preview. Separate server instances also cannot verify
  each other's tokens.
- **When `SEND_CONFIRM_SECRET` is set:** pending tokens stay valid across restarts until
  `CONFIRM_TTL_SECONDS` elapses. Single-use token consumption is still tracked in process memory
  only, so keep `CONFIRM_TTL_SECONDS` short even with a fixed secret.

### Recipient list syntax and matching (`EWS_RECIPIENT_ALLOWLIST`, `EWS_RECIPIENT_DENYLIST`)

Both settings accept comma-separated shell-style wildcard patterns matched with Python's
`fnmatch.fnmatch`, where `*` matches any sequence, `?` matches one character, and `[seq]` matches a
character set:

```ini
EWS_RECIPIENT_ALLOWLIST=*@example.com,contractor@example.org
EWS_RECIPIENT_DENYLIST=all-staff@example.com,*@competitor.example
```

Exact rules enforced by `ewsmcp/tools/base.py`:

1. **Where it runs.** Every tool call carrying `to`, `cc`, `bcc`, or `attendees` is checked before
   its handler runs, including `create_draft`, `update_draft`, and `create_event`. Confirmed calls
   that resolve recipients from Exchange also check the stored item's recipients at phase one before
   any token is issued (and again at phase two): `send_draft` checks the draft's `to`, `cc`, and
   `bcc`, and `update_event` with `notify_attendees=true` and `cancel_event` on a meeting with
   attendees check the attendees stored on the event.
2. **Normalisation.** In every call, `to`, `cc`, `bcc`, and `attendees` must be a string or a list
   of strings; any other shape fails with `validation`. Patterns are split on `,`, trimmed, and
   lowercased; blank entries are ignored. When either list is non-empty, if `<` or `>` is present,
   the value must contain one `<`, one `>`, and end with `>`, and only the text inside `<...>` is
   extracted. For example, `"Ops <Ops@Example.COM>"` becomes `"ops@example.com"`. The address is
   lowercased and must have no whitespace and a single `@` with non-empty local and domain parts;
   malformed entries fail first with `validation`.
3. **Order of checks.** `EWS_RECIPIENT_DENYLIST` runs **first**: if an address matches any denylist
   pattern, the call fails with `recipient_blocked` even when the allowlist also matches. Next, if
   `EWS_RECIPIENT_ALLOWLIST` is non-empty and the address matches none of its patterns, the call
   fails with `recipient_blocked`. When both settings are empty, all recipients are allowed.
4. **Stored entries without an SMTP address.** On `update_event(notify_attendees=true)` and
   `cancel_event` (a meeting with attendees), a stored attendee with no address at all — such as a
   personal distribution list stored by display name only — appears in the preview under
   `attendees_without_address` next to `attendees`. When either recipient list is set, the call is
   refused at phase one with `recipient_blocked`: "cannot verify recipients: one recipient has no
   address that can be checked against the recipient lists"; with no list set, the call proceeds.
   On `send_draft`, every stored recipient is listed in the preview as stored, with a name-only
   entry shown in the library's text form such as `Mailbox(name='Project list')`. When a list is
   set, a recipient without an SMTP address (no address, or a directory name such as
   `/o=Org/ou=Group/cn=Recipients/cn=user`) fails with `validation` quoting that text
   (`invalid recipient in 'to': …`), not `recipient_blocked`. An event attendee whose stored address
   is a directory name is listed in `attendees` as stored and likewise fails with `validation` when
   a list is set.

**When to change it:**

- Use `EWS_CAPABILITY_TIER=read` on shared or unattended servers that must never modify mailbox
  items.
- Set `SEND_ENABLED=true` when you want outbound mail or meeting notices enabled. At `draft` tier,
  that allows confirmed invitations on `create_event` and update notices on `update_event`.
- Set `EWS_CAPABILITY_TIER=full` when you need `delete_messages` or appointment-only `cancel_event`,
  and combine `EWS_CAPABILITY_TIER=full` with `SEND_ENABLED=true` for `send_draft`,
  `respond_to_event`, `set_oof`, and `cancel_event` on a meeting with attendees. Irreversible
  actions remain gated by two-phase confirmation.
- Set `EWS_RECIPIENT_ALLOWLIST` during rollouts or testing so drafts, invites, updates,
  cancellations, and sends cannot target addresses outside approved domains.
- Set `EWS_ATTACHMENT_DIR` to a dedicated staging folder when local workflows attach files by
  relative `path` instead of `content_base64`.

## Serving

These settings choose between local `stdio` and networked `http` mode, and configure the HTTP bind
address, authentication key, and log verbosity. `MCP_HOST`, `MCP_PORT`, and `MCP_API_KEY` are
ignored when `MCP_TRANSPORT='stdio'`.

| setting | default | limits | what it does |
|---|---|---|---|
| `MCP_TRANSPORT` | `'stdio'` | - | `'stdio'` for child-process stdin/stdout, or `'http'` for Streamable HTTP on `/mcp` plus REST on `/api/tools/*`. |
| `MCP_HOST` | `'127.0.0.1'` | - | Bind address for `uvicorn` when `MCP_TRANSPORT='http'` (loopback only by default). The container image sets `0.0.0.0`; leave `MCP_HOST` unset in `.env` when using `docker run --env-file .env`. |
| `MCP_PORT` | `8000` | - | TCP listen port for `uvicorn` when `MCP_TRANSPORT='http'`. |
| `MCP_API_KEY` | `None` | - | Shared secret required on non-health HTTP routes via `Authorization: Bearer <key>` or `x-api-key: <key>`. |
| `LOG_LEVEL` | `'INFO'` | - | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) for `stderr` and `uvicorn`. |

### `MCP_API_KEY` — with and without it

In `http` mode (`ewsmcp/http.py`):

- **When `MCP_API_KEY` is set:** `GET /livez`, `GET /readyz`, `GET /health`, and `GET /version`
  remain public for health probes, while `/.well-known/*` and `/register` always return `404`
  because the server does not implement OAuth. Every other route — `/mcp`, `/api/tools`,
  `/api/tools/{name}`, `/openapi.json`, and `/metrics` — requires `Authorization: Bearer <key>` or
  `x-api-key: <key>` compared in constant time, and returns HTTP `401` (`auth_failed`) without it.
- **When `MCP_API_KEY` is `None` or empty:** **no HTTP authentication is enforced.** Any process or
  host that can reach `MCP_HOST:MCP_PORT` can list and invoke every registered tool and read
  `/metrics`. Leave `MCP_API_KEY` unset only on `127.0.0.1` on a single-user machine or inside an
  isolated network, such as when an MCP client probes `/mcp` without headers first and treats an
  initial `401` as an OAuth challenge.

**When to change it:**

- Keep `MCP_TRANSPORT='stdio'` when the assistant runs on the same machine as `ewsmcp`.
- Switch to `MCP_TRANSPORT='http'` and set a long random `MCP_API_KEY` whenever `ewsmcp` runs in a
  container, on another host, or as a shared local daemon for several clients. With the default
  `MCP_HOST='127.0.0.1'`, the server listens on loopback only; to serve other machines, put a TLS
  reverse proxy in front of it or set `MCP_HOST=0.0.0.0` behind a firewall. See
  [`DEPLOYMENT.md`](DEPLOYMENT.md#http-server).
- Set `LOG_LEVEL=DEBUG` temporarily when diagnosing connection or sync issues.

## Storage

These settings control where `ewsmcp` writes local state on disk and guard that directory against
cloud-sync clients.

| setting | default | limits | what it does |
|---|---|---|---|
| `DATA_DIR` | `(empty)` | - | Local directory for aliases (`memory/`), audit chain (`audit/`), SQLite mirror (`cache/`), and saved files (`attachments/`). Empty resolves to `~/.ewsmcp`. |
| `DATA_DIR_ALLOW_SYNCED` | `False` | - | Disable the startup check that refuses cloud-synced `DATA_DIR` paths. |

### `DATA_DIR` and the synced-folder refusal

When `DATA_DIR` is empty, `ewsmcp` uses `~/.ewsmcp`. Any relative path or `~` prefix is expanded
and resolved to an absolute path at startup in `ewsmcp/config.py`.

`DATA_DIR` holds **mail at rest**: cleaned message bodies and subjects in `cache/mirror.db`,
raw-to-alias mappings (ids plus the e-mail addresses of people returned by `find_people`) in
`memory/aliases.db`, tool metadata and target ids (`draft_id`, `event_id`, `ids` — never
recipients, subjects, or bodies) in `audit/`, and saved attachments or `.eml` exports in
`attachments/`. When `EWS_SEMANTIC_INDEX='pgvector'` is enabled, synced message text is also sent
to `EWS_SEMANTIC_OLLAMA_URL` and item ids and embeddings are stored outside `DATA_DIR` in
PostgreSQL table `ews.message_embeddings`. At startup, `ewsmcp` checks the lowercased resolved
`DATA_DIR` path and **refuses to start** with `pydantic_core._pydantic_core.ValidationError`
(`1 validation error for Settings … Value error, DATA_DIR … appears to be inside a cloud-synced
folder ('…')`) if it contains any of these markers:

- `onedrive`
- `dropbox`
- `google drive`
- `googledrive`
- `icloud`

Putting `DATA_DIR` inside a synced folder copies mailbox content to a third-party cloud, corrupts
concurrent SQLite WAL files, and breaks OS file locks on `cache/sync.lock`.

**When to change it:**

- Set `DATA_DIR` to an explicit local directory writable by the `ewsmcp` user — such as
  `C:\ewsmcp-data` on Windows or `/var/lib/ewsmcp` owned by a Linux service account — whenever your
  home directory or working directory sits under OneDrive, Dropbox, Google Drive, or iCloud, or
  when running separate instances for different mailboxes. Omit `DATA_DIR` in `.env` when running
  the Docker image so the container keeps `DATA_DIR=/data`.
- Leave `DATA_DIR_ALLOW_SYNCED` at `False`. Flip it to `True` only when a local-only filesystem path
  contains one of the marker substrings by coincidence. See
  [`DEPLOYMENT.md`](DEPLOYMENT.md#the-data-directory) for the directory layout.

## The local mirror

These settings control the per-mailbox SQLite mirror at `DATA_DIR/cache/mirror.db` and its
background `SyncFolderItems` delta sync in `ewsmcp/cache/sync.py`. Warm reads come from SQLite with
`"source": "cache"` and an `"as_of"` timestamp; uncached folders, `fresh=true` calls, and cache
errors fall back to live Exchange automatically.

| setting | default | limits | what it does |
|---|---|---|---|
| `EWS_CACHE_ENABLED` | `True` | - | Enable the SQLite mirror and background delta sync. When `False`, reads go live to Exchange; `waiting_on` and `find_similar` require the mirror. |
| `EWS_CACHE_FOLDERS` | `'inbox,sent'` | - | Comma-separated well-known `Account` folder names kept warm via `SyncFolderItems`, such as `inbox`, `sent`, `drafts`, `trash`, `junk`, or `outbox`. |
| `EWS_CACHE_SYNC_SECONDS` | `45` | - | Sleep interval in seconds between mail delta-sync cycles; clamped at runtime to at least `5` s. |
| `EWS_CACHE_HIERARCHY_SECONDS` | `600` | - | Minimum interval in seconds between slow-lane syncs of the folder tree, 14-day calendar window (up to 200 events), and tasks; clamped at runtime to at least `60` s. |
| `EWS_CACHE_WINDOW_DAYS` | `365` | - | Age window in days for mirrored mail; delta items older than `now - EWS_CACHE_WINDOW_DAYS` are skipped after fetch. |
| `EWS_CACHE_PURGE_ON_BOOT` | `False` | - | Wipe all mirror tables on startup in the sync-lease leader and resync from scratch. |

**When to change it:**

- Keep `sent` in `EWS_CACHE_FOLDERS` if you use `waiting_on`, which queries mirrored sent threads.
- Lower `EWS_CACHE_WINDOW_DAYS` to `90` on very large mailboxes to shrink `mirror.db` on disk;
  older items are filtered after `SyncFolderItems` fetches them, so initial sync time does not
  shrink, and older mail remains searchable live on Exchange.
- Set `EWS_CACHE_ENABLED=false` when you want zero cached mail bodies on disk.
- Set `EWS_CACHE_PURGE_ON_BOOT=true` for a single restart to rebuild the mirror cleanly, then unset
  it so later restarts resume from saved `sync_state` tokens.

## Semantic search (optional)

These settings enable optional hybrid vector search via `search_messages(mode="semantic")` and the
`find_similar` tool, backed by PostgreSQL `pgvector` and an Ollama embedding server in
`ewsmcp/semantic.py`. When `EWS_SEMANTIC_INDEX='none'`, core `ewsmcp` needs neither PostgreSQL nor
Ollama, and `find_similar` is not registered.

| setting | default | limits | what it does |
|---|---|---|---|
| `EWS_SEMANTIC_INDEX` | `'none'` | - | `'none'` (disabled) or `'pgvector'` (sends synced message text to `EWS_SEMANTIC_OLLAMA_URL`, stores item ids and embeddings in PostgreSQL `ews.message_embeddings`, and registers `find_similar`). |
| `EWS_SEMANTIC_PG_DSN` | `None` | - | PostgreSQL connection string such as `postgresql://user:pass@localhost:5432/mail` when `EWS_SEMANTIC_INDEX='pgvector'`. |
| `EWS_SEMANTIC_OLLAMA_URL` | `'http://localhost:11434'` | - | Base URL of the Ollama server called at `/api/embed`. |
| `EWS_SEMANTIC_MODEL` | `'bge-m3'` | - | Ollama embedding model name. Must emit 1024-dimensional vectors (`EMBED_DIM = 1024` in `ewsmcp/semantic.py`). |

**When to change it:**

- Enable `EWS_SEMANTIC_INDEX=pgvector` only when you need concept-level similarity across Arabic
  and English mail beyond normalized FTS5 keyword search. Before enabling it, provision PostgreSQL
  with the `vector` extension, install `psycopg[binary]` into the same Python environment with
  `pip install "psycopg[binary]"`, start Ollama with `bge-m3` pulled, and keep
  `EWS_CACHE_ENABLED=true`. If `EWS_SEMANTIC_PG_DSN` is unset, `psycopg` is missing, or PostgreSQL
  setup fails at startup, `ewsmcp` logs an error and falls back to keyword-only search without
  registering `find_similar`.
- Keep `EWS_SEMANTIC_MODEL='bge-m3'` unless substituting another 1024-dimension model in Ollama;
  other vector dimensions do not match the `vector(1024)` column schema.

## Response size

These settings bound how much text a single read returns to the model context window. Card snippets
(`200` characters), `get_event` bodies (`2,000` characters), thread entries, and `get_attachment`
text slices (`20,000` characters) have separate fixed caps in code.

| setting | default | limits | what it does |
|---|---|---|---|
| `DEFAULT_PAGE_SIZE` | `20` | le 50 | Validated at startup (`<= 50`); paged tools currently declare their own `limit` defaults (`10`, `20` or `25`) in their schemas. |
| `BODY_MAX_CHARS` | `4000` | - | Maximum cleaned plain-text characters returned in message bodies (`message.body` on `get_message` and `get_messages` with `format="full"`). Does not apply to `get_event`, which cuts event bodies at `2,000` characters. |

**When to change it:**

- Raise `BODY_MAX_CHARS` to `8000` or `12000` when working routinely with long single messages and
  a large context window, or lower it to trim token usage.
- Leave `DEFAULT_PAGE_SIZE` at `20`, because values above `50` fail startup validation; pass
  `limit` per tool call to control page sizes.

## All settings

Every setting from `ewsmcp/config.py` in one reference table:

| setting | default | limits | meaning |
|---|---|---|---|
| `EWS_SERVER_URL` | `(required)` | - | Exchange EWS endpoint URL, such as `https://mail.example.com/EWS/Exchange.asmx`. |
| `EWS_EMAIL` | `(required)` | - | Primary SMTP address of the mailbox; also login fallback, token mailbox binding, and internal-domain reference. |
| `EWS_USERNAME` | `None` | - | Exchange login username; falls back to `EWS_EMAIL` when `None`. |
| `EWS_PASSWORD` | `None` | - | Exchange account password; treated as `""` when `None`. |
| `EWS_AUTH_TYPE_FORCE` | `None` | - | Pin `basic`, `ntlm`, or `digest` to skip auth probing; `None` or blank auto-negotiates. |
| `EWS_INSECURE_SKIP_VERIFY` | `False` | - | Disable TLS certificate verification on Exchange HTTPS requests. |
| `EWS_TZ` | `'UTC'` | - | IANA timezone name for date arguments (`today`, `+Nd`, `YYYY-MM-DD`, ISO date-times), recurring calendar series, and formatted timestamps. |
| `REQUEST_TIMEOUT` | `30` | - | Per-request Exchange HTTP timeout in seconds. |
| `EWS_WARMUP_MAX_BACKOFF_SECONDS` | `300` | - | Maximum exponential-backoff delay in seconds during warmup and reconnects. |
| `EWS_HEARTBEAT_SECONDS` | `600` | - | Seconds between live Exchange liveness probes while warm (`<= 0` disables). |
| `EWS_RETRY_MAX_WAIT_SECONDS` | `300` | - | Maximum retry wait in seconds for `exchangelib` on non-401 transient errors. |
| `EWS_AUTH_FAILURE_COOLDOWN_SECONDS` | `900` | ge 60 | Cooldown in seconds after an HTTP 401 rejection before one recovery probe may run. |
| `EWS_AUTH_MAX_REJECTIONS` | `3` | ge 1, le 10 | Rejected credential submissions since the last successful login before staying in `auth_failed` until restart. |
| `EWS_MAX_CONCURRENCY` | `4` | - | Worker thread-pool size for blocking Exchange calls (clamped to `>= 1`). |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | - | Consecutive upstream or throttle tool failures that open the dispatcher circuit. |
| `CIRCUIT_OPEN_SECONDS` | `60` | - | Seconds the dispatcher circuit stays open, refusing Exchange-backed tools with `upstream_unavailable` while `get_server_status`, `waiting_on`, and `find_similar` still answer. |
| `EWS_CAPABILITY_TIER` | `'draft'` | - | `read`, `draft`, or `full` capability tier; tools above the tier are not registered. |
| `SEND_ENABLED` | `False` | - | Global send kill-switch; while `False`, refuses every call that sends mail or meeting notices with `kill_switch` at phase one. |
| `SEND_CONFIRM_SECRET` | `None` | - | HMAC secret for two-phase confirm tokens; random per-process when `None`. |
| `CONFIRM_TTL_SECONDS` | `600` | - | Two-phase confirm token lifetime in seconds. |
| `EWS_RECIPIENT_ALLOWLIST` | `(empty)` | - | Comma-separated `fnmatch` globs of permitted recipient addresses (empty allows all). |
| `EWS_RECIPIENT_DENYLIST` | `(empty)` | - | Comma-separated `fnmatch` globs of blocked recipient addresses, checked before the allowlist. |
| `EWS_MAX_SENDS_PER_HOUR` | `10` | - | In-memory per-process rolling 1-hour cap on every confirmed call that sends; checked before `confirm_token` is consumed (`<= 0` disables). |
| `EWS_ATTACHMENT_DIR` | `(empty)` | - | Local directory from which draft tools may read relative `path` attachments (empty disables). |
| `EWS_MAX_ATTACHMENT_MB` | `25` | ge 1, le 50 | Per-call size ceiling in MiB for attachments and `.eml` exports; sizes the HTTP body limit. |
| `EWS_KEEP_DUMPABLE` | `False` | - | Keep Linux process dumpability enabled instead of setting `PR_SET_DUMPABLE=0` before worker spawn. |
| `MCP_TRANSPORT` | `'stdio'` | - | Transport mode: `'stdio'` or `'http'`. |
| `MCP_HOST` | `'127.0.0.1'` | - | Bind address in `http` mode (ignored in `stdio` mode). |
| `MCP_PORT` | `8000` | - | Bind TCP port in `http` mode (ignored in `stdio` mode). |
| `MCP_API_KEY` | `None` | - | Bearer / `x-api-key` token required on non-health HTTP endpoints when set. |
| `LOG_LEVEL` | `'INFO'` | - | Log level for Python `stderr` and `uvicorn`. |
| `DATA_DIR` | `(empty)` | - | Local storage directory (empty resolves to `~/.ewsmcp`); cloud-synced folders are refused. |
| `DATA_DIR_ALLOW_SYNCED` | `False` | - | Opt out of the cloud-synced folder check on `DATA_DIR`. |
| `EWS_CACHE_ENABLED` | `True` | - | Enable the local SQLite mailbox mirror and background delta sync. |
| `EWS_CACHE_FOLDERS` | `'inbox,sent'` | - | Comma-separated mail folders kept warm via `SyncFolderItems` delta sync. |
| `EWS_CACHE_SYNC_SECONDS` | `45` | - | Fast-lane mail delta-sync interval in seconds (clamped to `>= 5`). |
| `EWS_CACHE_HIERARCHY_SECONDS` | `600` | - | Slow-lane sync interval in seconds for folders, 14-day calendar window, and tasks (clamped to `>= 60`). |
| `EWS_CACHE_WINDOW_DAYS` | `365` | - | Mail mirror age window in days (items older than the window are skipped after fetch). |
| `EWS_CACHE_PURGE_ON_BOOT` | `False` | - | Wipe mirror tables at startup (leader only) and resync from scratch. |
| `EWS_SEMANTIC_INDEX` | `'none'` | - | Semantic adapter: `'none'` or `'pgvector'`. |
| `EWS_SEMANTIC_PG_DSN` | `None` | - | PostgreSQL DSN for `pgvector` semantic indexing (requires `psycopg`). |
| `EWS_SEMANTIC_OLLAMA_URL` | `'http://localhost:11434'` | - | Ollama base URL for `pgvector` embeddings. |
| `EWS_SEMANTIC_MODEL` | `'bge-m3'` | - | 1024-d Ollama embedding model used when `EWS_SEMANTIC_INDEX='pgvector'`. |
| `DEFAULT_PAGE_SIZE` | `20` | le 50 | Validated at startup (`<= 50`); tool schemas set their own per-tool `limit` defaults. |
| `BODY_MAX_CHARS` | `4000` | - | Maximum cleaned plain-text characters returned in message bodies (`get_message`, `get_messages`); `get_event` cuts at `2,000`. |
