# Troubleshooting

Every tool failure returns `{"ok": false, "error": {"code": "…", "message": "…"}}`, with an
optional `hint` and `retry_after_s`. Start by checking server status, then look up the symptom or
error code below.

## Start with get_server_status

`get_server_status` is available at every tier and answers at any time: while the server is still
connecting, after Exchange rejected the credentials, and while the circuit breaker is open.
`waiting_on` and `find_similar` do not contact Exchange either and still answer while the circuit
breaker is open once their local mirror or semantic index is ready. Over HTTP, `GET /livez`,
`GET /readyz`, `GET /health`, and `GET /version` answer without `MCP_API_KEY`; see
[DEPLOYMENT.md](DEPLOYMENT.md#health-readiness-and-metrics).

| Field in `get_server_status` | What it tells you |
|---|---|
| `connection.state` | `connecting` at initial startup before the first probe succeeds, `warm` when ready, `degraded` after a failed heartbeat probe (tool calls still try live Exchange), or `auth_failed` after an HTTP `401` rejection |
| `connection.last_error`, `next_retry_in_s` | `last_error` is the error of the last finished connection attempt, or `null` when `warm` or while the first attempt is still retrying; `next_retry_in_s` is seconds until the next probe, or `null` when `warm`, permanently stopped, or while an attempt is in progress |
| `connection.auth_rejections`, `auth_permanent` | Rejections since the last successful login and whether background probes have stopped until restart |
| `connection.circuit_open`, `connection.retry_after_s` | `true` and seconds remaining while the dispatcher circuit breaker is open; both keys are absent otherwise |
| `tier`, `send_kill_switch` | Active `EWS_CAPABILITY_TIER` and whether sending is off; `send_kill_switch: true` means `SEND_ENABLED=false` |
| `cache` | Whether the SQLite mirror is `enabled` and `ready`, row counts, `db_mb`, folder `watermarks`, and `sync` health including `sync.role` (`leader` or `follower`) |
| `document_memory_ceiling` | Whether the `1024` MB attachment worker ceiling is `enforced` on the host OS |

## Cannot connect

### Server exits at startup or the MCP client never sees it

- **Symptom:** Running `ewsmcp` with no settings exits immediately with
  `pydantic_core._pydantic_core.ValidationError: 2 validation errors for Settings` (`ews_server_url`
  `Field required` and `ews_email` `Field required`), an MCP `stdio` client reports `Connection
  closed` on launch, or `claude mcp list` in another folder does not list `exchange`.
- **Cause:** `Settings` in `ewsmcp/config.py` requires `EWS_SERVER_URL` and `EWS_EMAIL` at startup
  and reads `.env` only from the process's current working directory, not from `v5/` or
  `.venv/bin/`. When an MCP client spawns `/path/to/.venv/bin/ewsmcp`, the child process does not
  start in your repository checkout, so a `.env` file there is not loaded. Also, `claude mcp add`
  registers the server for the current project unless `--scope user` is given.
- **Fix:** Pass `EWS_SERVER_URL`, `EWS_EMAIL`, `EWS_USERNAME`, and `EWS_PASSWORD` in the MCP
  client's `env` block or `-e` flags, or `cd` into the directory containing `.env` before running
  `ewsmcp` directly. Add `--scope user` on `claude mcp add` to make it available in every project,
  or run `claude mcp add` in the project where you will use it. See
  [CONFIGURATION.md](CONFIGURATION.md#how-settings-are-read).

### Endpoint or VPN is not reachable

- **Symptom:** During initial startup, tools requiring Exchange return `code: "upstream_unavailable"`
  with `"Exchange connection still warming up (attempt …; last error: …)"`, or live calls fail with
  a connection/timeout message, and `GET /readyz` returns HTTP `503` with `status: "unavailable"`.
- **Cause:** `ewsmcp` starts without waiting for Exchange to respond and warms the connection in
  the background, backing off up to `EWS_WARMUP_MAX_BACKOFF_SECONDS`, which defaults to `300`
  seconds. While `connection.state` is `connecting` (initial startup only), tools requiring
  Exchange are refused immediately; if a later heartbeat fails after reaching `warm`, state becomes
  `degraded` and `/readyz` returns `503` while tool calls still try live Exchange. Either
  `EWS_SERVER_URL` is wrong, the workstation VPN is disconnected, or `ewsmcp` runs in a container
  or remote host that does not share your workstation's VPN route. Note that 4.5 disables
  Autodiscover and requires the full EWS URL, such as `https://mail.example.com/EWS/Exchange.asmx`,
  not a bare hostname.
- **Fix:** Check `EWS_SERVER_URL` and `REQUEST_TIMEOUT`, which defaults to `30` seconds. When
  Exchange is only reachable over a workstation VPN, run `ewsmcp` locally over `stdio` rather than
  in Docker; see [DEPLOYMENT.md](DEPLOYMENT.md#local-over-stdio).

### TLS certificate verification fails

- **Symptom:** `get_server_status` stays in `connecting` or `degraded` with an SSL/`SSLError`
  certificate verification error in `connection.last_error`, and live calls fail with
  `code: "upstream_unavailable"` or `code: "upstream_error"`.
- **Cause:** On-premises Exchange presents a certificate signed by an internal CA that Python's
  `requests` trust store does not recognize, or a certificate whose hostname does not match
  `EWS_SERVER_URL`.
- **Fix:** Point `REQUESTS_CA_BUNDLE` at your internal CA bundle in the process environment. Only
  in a trusted test environment where no CA bundle is available, set `EWS_INSECURE_SKIP_VERIFY=true`
  and restart `ewsmcp`. That disables TLS verification on Exchange traffic and logs a startup
  warning; see [CONFIGURATION.md](CONFIGURATION.md#connecting-to-exchange).

### Exchange throttles requests or the circuit breaker opens

- **Symptom:** Calls return `code: "throttled"` with `retry_after_s: 60`, or tools that need
  Exchange return `code: "upstream_unavailable"` with `"circuit open after repeated upstream
  failures"` and `retry_after_s`, while `get_server_status.connection` shows `circuit_open: true`
  and `retry_after_s`.
- **Cause:** Exchange returned `ErrorServerBusy` or a back-off signal, or the dispatcher saw
  `CIRCUIT_FAILURE_THRESHOLD` consecutive failures of kind `upstream_unavailable`, `upstream_error`,
  or `throttled` and opened its circuit breaker for `CIRCUIT_OPEN_SECONDS`. Unexpected local
  exceptions map through `errors.map_exception` to `upstream_error` and count toward the breaker,
  whereas `validation`, `not_found`, and `internal` (used only for an unwritable attachments folder
  or an unavailable document worker) do not. While the breaker is open, `get_server_status`,
  `waiting_on`, and `find_similar` still answer; the breaker closes once `CIRCUIT_OPEN_SECONDS`
  have passed.
  - `CIRCUIT_FAILURE_THRESHOLD` defaults to `5`.
  - `CIRCUIT_OPEN_SECONDS` defaults to `60`.
- **Fix:** Wait `retry_after_s` seconds before retrying Exchange-backed tools. If `throttled`
  recurs under normal use, lower `EWS_MAX_CONCURRENCY` from its default of `4`, raise
  `EWS_CACHE_SYNC_SECONDS` from its default of `45`, and avoid repeated `fresh=true` or wide
  `folder="f:all"` and `recursive=true` searches. See [CONFIGURATION.md](CONFIGURATION.md#reliability).

## Exchange rejects the credentials

### `auth_failed` and the lockout guard

- **Symptom:** Exchange-backed tools return `code: "auth_failed"` with `hint`: *"Retrying will not
  help: credentials are read once at startup and repeated failures risk locking the directory
  account. Ask the operator to check the configured address/username and password, update the
  configuration, and restart the server."* `GET /readyz` returns HTTP `503` with
  `status: "auth_failed"` and `connection` fields `state: "auth_failed"`, `auth_rejections`,
  `auth_permanent`, `next_retry_in_s`, and `last_error`. `get_server_status` returns the same
  `connection` object, and `GET /metrics` exports `ewsmcp_auth_failed 1` and
  `ewsmcp_auth_rejections`.
- **Cause:** Exchange returned HTTP `401`, including `"currently locked out"` responses. Because
  each rejected password counts toward directory account lockout, `RejectingFaultTolerance` aborts
  on the first `401` without retrying, and `AuthBreaker` opens across every Exchange path: tool
  calls, warmup, heartbeat, and background sync.
  - `EWS_AUTH_FAILURE_COOLDOWN_SECONDS`: defaults to `900` seconds, minimum `60`. The breaker stays
    open for this cooldown before allowing one recovery probe; if that probe succeeds, the rejection
    count resets to `0`.
  - `EWS_AUTH_MAX_REJECTIONS`: defaults to `3`, allowed range `1` to `10`. Counts rejections since
    the last successful login, including concurrent requests already in flight when the first `401`
    arrived. Once reached, `auth_permanent` becomes `true`, `next_retry_in_s` becomes `null`, and
    the breaker stays open until `ewsmcp` restarts.
- **Fix:**
  1. Check `EWS_EMAIL`, `EWS_USERNAME` — such as `DOMAIN\user` versus `you@example.com` — and
     `EWS_PASSWORD` in your client `env` block or `.env`.
  2. Check stderr logs for `Exchange HTTP 401 rejection: WWW-Authenticate schemes […];
     ews_auth_type_force=…`. When auto-negotiation picks a scheme Exchange rejects, set
     `EWS_AUTH_TYPE_FORCE` to `basic`, `ntlm`, or `digest`.
  3. **When changing a password:** stop every running `ewsmcp` process first, update `EWS_PASSWORD`,
     and start `ewsmcp` again. Any running process keeps probing with the old password after every
     cooldown until it reaches `EWS_AUTH_MAX_REJECTIONS` (by default up to 3 failed logins per
     process, about 15 minutes apart). Credentials are read once at startup, so retrying tool calls
     never reloads `.env` or clears `auth_permanent: true`.

## A tool call is refused

### Tool is missing or unknown above the server's tier

- **Symptom:** A write, send, or delete tool is absent from `tools/list` and `GET /api/tools`, and
  calling it returns `code: "validation"` with `"Unknown tool: <name>"` — for example
  `"Unknown tool: create_draft"` at `read` tier — mapped to HTTP `404` over REST.
- **Cause:** A tool above `EWS_CAPABILITY_TIER` is not registered: `read` exposes 18 tools, `draft`
  exposes 29, and `full` exposes 34 (35 when semantic search is active). `tier_blocked`, mapped to
  HTTP `403`, is a second check inside the dispatcher that a client does not normally reach.
- **Fix:** Set `EWS_CAPABILITY_TIER=draft` for drafts, moves, flags, tasks, calendar edits, and
  inbox rules, or `EWS_CAPABILITY_TIER=full` for `send_draft`, `respond_to_event`, `set_oof`,
  `cancel_event`, and `delete_messages`, then restart `ewsmcp`. See
  [SAFETY.md](SAFETY.md#capability-tiers).

### Sending is disabled (`kill_switch`)

- **Symptom:** Calling `send_draft`, `respond_to_event`, `set_oof`, `create_event` with
  `send_invitations=true`, `update_event` with `notify_attendees=true`, or `cancel_event` on a
  meeting with attendees fails at phase one with `code: "kill_switch"`, before any confirmation
  token is issued. Class `send` tools return `"<tool> is blocked: SEND_ENABLED=false on this
  server."`, and notifying calendar calls return
  `"<tool> is blocked: notifying attendees sends mail and SEND_ENABLED=false on this server."`:

```json
{
  "ok": false,
  "error": {
    "code": "kill_switch",
    "message": "cancel_event is blocked: notifying attendees sends mail and SEND_ENABLED=false on this server.",
    "hint": "Cancel the meeting from Outlook, or have the operator flip SEND_ENABLED."
  }
}
```

- **Cause:** `SEND_ENABLED` defaults to `false`, even when `EWS_CAPABILITY_TIER=full`. While
  `SEND_ENABLED` is `false`, nothing that sends mail or meeting notices is allowed: `ToolSpec`
  refuses all six sending calls at phase one, with a second check inside the three calendar
  handlers. Calls that send nothing — `cancel_event` on an appointment without attendees,
  `create_event` and `update_event` without notification, and `delete_messages` — are not affected.
- **Fix:** Keep working with drafts and silent calendar saves (`send_invitations=false`,
  `notify_attendees=false`), or set `SEND_ENABLED=true` and restart `ewsmcp`. Invitations and
  update notices from `create_event` and `update_event` need only tier `draft`,
  `SEND_ENABLED=true`, and a confirmation; `send_draft`, `respond_to_event`, `set_oof`, and
  `cancel_event` also require `EWS_CAPABILITY_TIER=full`.

### Address is not permitted (`recipient_blocked`)

- **Symptom:** A `create_draft`, `update_draft`, `create_event`, `send_draft`,
  `update_event` with `notify_attendees=true`, or `cancel_event` call fails with
  `code: "recipient_blocked"` naming `EWS_RECIPIENT_DENYLIST` or `EWS_RECIPIENT_ALLOWLIST`, or (on
  `update_event` and `cancel_event`) saying a stored attendee has no checkable address. Non-string
  recipient arguments, or stored non-SMTP / malformed recipient strings while a list is configured
  (including on `send_draft`), fail with `code: "validation"`:

```json
{
  "ok": false,
  "error": {
    "code": "recipient_blocked",
    "message": "recipient 'peer@partner.example' is not allowlisted (EWS_RECIPIENT_ALLOWLIST)"
  }
}
```

```json
{
  "ok": false,
  "error": {
    "code": "recipient_blocked",
    "message": "cannot verify recipients: one recipient has no address that can be checked against the recipient lists"
  }
}
```

- **Cause:** An address in the call arguments — or stored on the draft (`to`, `cc`, `bcc`) or
  calendar event (required, optional, and resource `attendees`) fetched at phase one during
  `send_draft`, `update_event` with `notify_attendees=true`, or `cancel_event` on a meeting —
  matched `EWS_RECIPIENT_DENYLIST` or failed to match `EWS_RECIPIENT_ALLOWLIST`. When either list
  is set, `update_event` with `notify_attendees=true` and `cancel_event` also fail at phase one
  with `recipient_blocked` if a stored event attendee has no address at all (such as a personal
  distribution list, otherwise shown under `attendees_without_address`), saving nothing. By
  contrast, a stored `send_draft` recipient that is not an SMTP address, or a stored
  directory-name address such as `/o=Org/ou=Group/cn=Recipients/cn=user` on a draft or event, is
  listed as stored and fails with `validation` (`invalid recipient in 'to': …` /
  `… in 'attendees': …`) when a list is set.
- **Fix:** Remove the disallowed recipient, expand personal distribution lists or name-only entries
  to individual SMTP addresses, or edit without `notify_attendees=true` when appropriate.
  Otherwise, update `EWS_RECIPIENT_ALLOWLIST` and `EWS_RECIPIENT_DENYLIST` and restart `ewsmcp`.
  See [SAFETY.md](SAFETY.md#recipients).

### Hourly send limit reached (`rate_capped`)

- **Symptom:** A confirmed `send_draft`, `respond_to_event`, `set_oof`,
  `create_event` with `send_invitations=true`, `update_event` with `notify_attendees=true`, or
  `cancel_event` on a meeting with attendees fails at phase two with `code: "rate_capped"` and
  `retry_after_s` (at least `1`):

```json
{
  "ok": false,
  "error": {
    "code": "rate_capped",
    "message": "send rate cap reached (1/hour)",
    "retry_after_s": 3599
  }
}
```

- **Cause:** Confirmed calls that send reached `EWS_MAX_SENDS_PER_HOUR`, which defaults to `10`,
  within the rolling 3,600-second window. Silent calendar edits and plain appointment deletions do
  not count, while a confirmed sending call that its handler later refuses (such as `create_event`
  with `end` before `start`) still uses one slot and spends its `confirm_token`. The counter is
  held in memory per process.
- **Fix:** At phase two, the server verifies the token, rejects already-spent tokens with
  `confirm_invalid`, checks `EWS_MAX_SENDS_PER_HOUR`, and only then consumes the token. Because a
  `rate_capped` refusal keeps its `confirm_token`, wait `retry_after_s` seconds and retry the same
  call with the same `confirm_token` if `CONFIRM_TTL_SECONDS` has not elapsed, or re-preview if the
  token expired. Restarting `ewsmcp` resets that process's counter; setting
  `EWS_MAX_SENDS_PER_HOUR=0` disables the cap.

### Confirmation token rejected (`confirm_invalid`)

- **Symptom:** Phase 2 of a two-phase tool fails with `code: "confirm_invalid"` and
  `"confirm_token rejected (<reason>)"`.
- **Cause:**
  - `expired`: more than `CONFIRM_TTL_SECONDS`, which defaults to `600` seconds, passed since
    phase 1.
  - `stale`: the draft, calendar event, or inbox rule changed after the preview — for example via
    `update_draft`, `add_draft_attachment`, or Outlook — or bound call arguments differ from
    phase 1. Tools with a preview hook (`send_draft`, `update_event` with `notify_attendees=true`,
    `cancel_event`, `save_rule`, `delete_rule`) bind the target id and resolved preview content and
    re-fetch at phase 2, saving nothing if the target changed. For `update_event`, that bound
    content covers the event's `subject`, `location`, `start`, `end`, `type`, and stored
    `attendees`, plus `scope`, `notify_attendees`, and every requested change including full
    `body`. Extra arguments outside that content, such as `idempotency_key` on `send_draft`, are
    not bound, whereas tools without a preview hook bind all call arguments.
  - `consumed`: the token was already used once.
  - `bad_signature` or `malformed`: `ewsmcp` restarted between phase 1 and phase 2 without a pinned
    `SEND_CONFIRM_SECRET`, or the token string was altered.
- **Fix:** Call the tool again without `confirm_token` to obtain a fresh preview and token, check
  the preview, and repeat with the new `confirm_token`. Note that `idempotency_key` replay on
  `send_draft` (`replayed: true`) works only when `draft_id` is the raw EWS id; with a `d` alias a
  retry is treated as a new call and fails once the sent draft has left Drafts. See
  [SAFETY.md](SAFETY.md#two-phase-confirmation).

## Results look out of date

- **Symptom:** A message that just arrived or changed in Outlook is missing from `search_messages`,
  `get_mailbox_overview`, `list_folders`, or `list_tasks`, or `get_message` omits `flag_complete`
  or attachment metadata (`attachments_hint` shown instead).
- **Cause:** Reads use the local SQLite mirror first and return `source: "cache"` with `as_of`:
  - `EWS_CACHE_FOLDERS` defaults to `inbox,sent` over `EWS_CACHE_WINDOW_DAYS`, which defaults to
    `365` days.
  - Mail delta-sync runs every `EWS_CACHE_SYNC_SECONDS`, which defaults to `45` seconds.
  - The folder tree, 14-day calendar window, and tasks refresh every `EWS_CACHE_HIERARCHY_SECONDS`,
    which defaults to `600` seconds.
  - `get_message` includes `flag_complete` and full attachment metadata only on live reads.
- **Fix:** Pass `fresh=true` on `search_messages`, `check_messages`, `get_message`, `get_messages`,
  `get_thread`, `get_mailbox_overview`, `list_folders`, or `list_tasks` to force a live Exchange
  read (`source: "live"`). If the mirror itself needs a clean rebuild, check
  `get_server_status.cache` and restart the leader process once with `EWS_CACHE_PURGE_ON_BOOT=true`,
  then remove that setting.

## Ids stopped working

- **Symptom:** Passing an alias like `m424242` fails with `code: "validation"`, or `code:
  "not_found"` inside `get_messages.failed`, rule tools, or Exchange item fetches:

```json
{
  "ok": false,
  "error": {
    "code": "validation",
    "message": "Unknown alias 'm424242': it is stale or from a previous session. EWS ids change when items move; re-run the search/list tool to get fresh ids, then retry."
  }
}
```

- **Cause:** Two situations make an id unusable:
  1. **Unknown alias:** the alias is not in `DATA_DIR/memory/aliases.db` because `DATA_DIR` was
     wiped or changed across restarts, a container ran without a persistent `/data` volume, or the
     alias was never minted. Single-item input resolution returns `validation`, while unknown `m`
     aliases in `get_messages.failed` and unknown `rule_id` handles in `save_rule` and
     `delete_rule` report `not_found`.
  2. **External move or delete:** aliases survive restarts and `move_messages` rebinds them
     automatically, but Exchange assigns a new raw `ItemId` whenever an item moves folders in
     Outlook or via an Inbox rule, so Exchange returns `ErrorItemNotFound` (`code: "not_found"`).
- **Fix:** Mount a persistent `DATA_DIR`, such as `-v ewsmcp-data:/data` in Docker, so
  `memory/aliases.db` survives restarts. Re-run `search_messages`, `list_events`, `list_tasks`,
  `list_folders`, or `list_rules` to obtain fresh aliases whenever an item moved externally.

## Attachments and documents

### Reading a PDF fails without the `extract` extra

- **Symptom:** `get_attachment` on a `.pdf` returns `code: "validation"` with `"PDF text extraction
  requires the optional 'pypdf' dependency, which is not installed."`.
- **Cause:** Local installation ran `pip install ./v5` without `[extract]`, which installs
  `pypdf>=6.21,<7`. The Docker image installs `.[extract]` automatically.
- **Fix:** `ews-mcp` is not published on PyPI, so install from your local repository root rather
  than running `pip install "ews-mcp[extract]"` against PyPI: run `pip install "./v5[extract]"` in
  the server's virtual environment and restart `ewsmcp`, or call `get_attachment` with
  `mode="save"` to write the PDF under `DATA_DIR/attachments`.

### Attachment selector fails with `not_found` or `validation`

- **Symptom:** `get_attachment` fails with `code: "validation"` asking you to pick one attachment,
  or `code: "not_found"` saying `"No attachment matches …"` or `"Attachment id 'a7' belongs to
  another message's attachment. Available here: …"`.
- **Cause:** When a message has more than one attachment, `attachment` is required. `get_attachment`
  reads `attachment` in this order:
  1. File name (case-insensitive; a matching file name always wins).
  2. An `a<digits>` id returned by the draft tools, resolved via `aliases.db` to the attachment it
     was issued for; if it was issued for an attachment of another message, the call fails with
     `not_found` and never treats it as an index.
  3. A raw Exchange attachment id.
  4. A zero-based index written as digits (`"0"`, `"1"`, …), including `a<digits>` when no such
     alias exists in `aliases.db` or when `aliases.db` cannot be read.
- **Fix:** Call `get_message` to inspect `message.attachments` and pass `attachment=<name>` or
  `attachment=<zero-based index>` as a string (such as `"0"`), or pass the `a<digits>` id minted
  for an attachment on that draft.

### Attachment exceeds `EWS_MAX_ATTACHMENT_MB`

- **Symptom:** `get_attachment` in `mode="auto"` or `mode="text"`, `create_draft`,
  `add_draft_attachment`, or `get_message(format="eml")` returns `code: "validation"` stating the
  size exceeds the `EWS_MAX_ATTACHMENT_MB` ceiling, or `POST /api/tools/{name}` returns HTTP `413`
  with `"request body exceeds … bytes"`.
- **Cause:** Per-call attachment extraction/upload size and `.eml` export size are capped by
  `EWS_MAX_ATTACHMENT_MB`, which defaults to `25` and accepts `1` to `50`. Size is checked both
  before and after download, and the HTTP request body limit on `/mcp` and `/api/tools/{name}`
  derives from the same setting.
- **Fix:** Raise `EWS_MAX_ATTACHMENT_MB` up to `50` and restart `ewsmcp`, or use
  `get_attachment` with `mode="info"` for metadata. For incoming file attachments,
  `get_attachment(mode="save")` writes raw bytes directly under `DATA_DIR/attachments` without
  enforcing `EWS_MAX_ATTACHMENT_MB` (attached Exchange items cannot use `mode="save"`).

### Document text comes back `partial: true` or `truncated: true`

- **Symptom:** `get_attachment` returns `ok: true` with `truncated: true` and `next_offset`, returns
  `partial: true` with `notes`, or fails with `code: "validation"` and `"the document could not be
  read within the time and memory limits"`.
- **Cause:**
  - `truncated: true` with `next_offset`: extracted text exceeds one `20,000`-character slice; the
    remaining text is held in the 10-minute in-memory cache.
  - `partial: true` with `notes`: extraction stopped cleanly at a safety bound — 75% of the
    20-second worker timeout, `5,000,000` total characters, `500` PDF pages, `1,000` slides, `200`
    sheets, or `5,000` rows by `50` columns per worksheet.
  - `validation` with `"within the time and memory limits"`: a single unit hung past 20 seconds or
    exceeded the `1,024` MB worker memory ceiling.
- **Fix:** Pass `offset=<next_offset>` to `get_attachment` with the same `message_id` and
  `attachment` to read the next slice from memory. When `partial: true` or a worker limit cut the
  file, check `notes`, `pages_read`, `sheets_read`, or `slides_read` to see where extraction
  stopped, and use `get_attachment(mode="save")` if you need the complete original file on disk.
  See [API.md](API.md#reading-attachments).

### Outlook `.msg` attachment returns metadata only

- **Symptom:** `get_attachment` on a `.msg` file returns `ok: true` with `mode: "info"` and
  `"Outlook .msg message file — metadata only. Call again with mode='save'…"` even when
  `mode="text"` was passed.
- **Cause:** Compound-file `.msg` extraction is not supported in 4.5. RFC-822 `.eml` files and
  attached Exchange items are supported.
- **Fix:** Call `get_attachment` with `mode="save"` to write the `.msg` file under
  `DATA_DIR/attachments` and open it in an external mail client.

## HTML drafts

### HTML draft is refused (`validation`)

- **Symptom:** `create_draft` or `update_draft` with `body_format="html"` fails with
  `code: "validation"` and either `"HTML body exceeds 200000 characters (got …)"` or `"HTML body
  has no visible text after sanitising"`.
- **Cause:** `sanitise_html` rejects HTML inputs longer than `200,000` characters, drops
  disallowed elements — including `<script>`, `<style>`, `<form>`, `<svg>`, and all `<img>` tags —
  unwraps empty links, and requires at least one visible non-blank character. Blank fillers such as
  `U+3164` and `U+2800` do not count as visible text.
- **Fix:** Trim the HTML input under `200,000` characters and include visible text outside dropped
  tags, or switch to `body_format="text"`. Attach images via `attachments` or
  `add_draft_attachment` rather than `<img>`. See [API.md](API.md#draft-bodies).

## HTTP mode and clients

### Client treats the initial `401` on `/mcp` as an OAuth challenge

- **Symptom:** An HTTP MCP client fails to connect to `http://host:8000/mcp` when `MCP_API_KEY` is
  set, asking for OAuth metadata or registration after receiving HTTP `401` with
  `code: "auth_failed"` and `"missing or invalid bearer token"`.
- **Cause:** `ewsmcp` does not implement OAuth and returns `404` on `/.well-known/*` and
  `/register`. Some MCP clients still probe `/mcp` unauthenticated before attaching configured
  headers and interpret that first `401` as an OAuth challenge. Note also that 4.5 accepts
  `MCP_API_KEY` only in `Authorization: Bearer <key>` or `X-API-Key: <key>` headers, never in a
  `?api_key=` URL query parameter.
- **Fix:** Bridge stdio clients with `npx -y mcp-remote http://127.0.0.1:8000/mcp --header
  "Authorization: Bearer <key>"`, or unset `MCP_API_KEY` and restrict network access instead with
  `MCP_HOST=127.0.0.1` or an internal container network. `x-api-key` on `POST /api/tools/<name>` is
  unaffected. See [DEPLOYMENT.md](DEPLOYMENT.md#http-server).

### Container is `healthy`, yet host requests reset or fail

- **Symptom:** `docker ps` reports `(healthy)` on `/livez`, while `curl http://127.0.0.1:8000/readyz`
  on the host fails with `Connection reset by peer` or `Empty reply from server`, or container logs
  show `aliaser init failed` / `cache init failed`.
- **Cause:** Passing `--env-file .env` to `docker run` overrides image defaults (`MCP_HOST=0.0.0.0`,
  `DATA_DIR=/data`). Setting `MCP_HOST=127.0.0.1` inside `.env` binds `uvicorn` to container
  loopback only, which the in-container healthcheck reaches and host port publishing cannot; setting
  a host `DATA_DIR` path inside `.env` points the unprivileged `mcp` user away from `/data`.
- **Fix:** Leave `MCP_HOST` and `DATA_DIR` unset in `.env` files passed to `docker run` (use
  `-p 127.0.0.1:8000:8000` on the host to keep the port local), and write unquoted `KEY=value`
  lines in Docker env files.

## Storage

### `DATA_DIR` is inside a cloud-synced folder

- **Symptom:** `ewsmcp` refuses to start with
  `pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings … Value error,
  DATA_DIR … appears to be inside a cloud-synced folder ('onedrive'). …`.
- **Cause:** Empty `DATA_DIR` resolves to `~/.ewsmcp`. If the resolved path contains `onedrive`,
  `dropbox`, `google drive`, `googledrive`, or `icloud`, startup fails so local mailbox databases
  and saved attachments are not synced to third-party cloud storage.
- **Fix:** Point `DATA_DIR` at a local folder outside any sync client, such as `C:\ewsmcp-data` or
  `/var/lib/ewsmcp` (owned by the service user). Set `DATA_DIR_ALLOW_SYNCED=true` only if you
  deliberately accept syncing mail at rest. See [CONFIGURATION.md](CONFIGURATION.md#storage).

### `DATA_DIR` is not writable (`aliaser init failed`, `cache init failed`)

- **Symptom:** `ewsmcp` starts and answers requests, while stderr logs
  `aliaser init failed ([Errno 13] Permission denied…) — running with raw EWS ids` and
  `cache init failed (…) — running pure-EWS reads`. `get_server_status` reports
  `"cache": {"enabled": true, "ready": false}`, tool outputs return raw Exchange ids instead of
  short aliases, no audit records are written under `DATA_DIR/audit`, and saving attachments or
  `.eml` files fails with `code: "internal"`.
- **Cause:** `DATA_DIR` (for example `/var/lib/ewsmcp` in `.env`) does not exist or is not writable
  by the OS user running `ewsmcp`. Because local storage failures degrade to pass-through rather
  than aborting startup, `ewsmcp` falls back to `NullAliaser`, live-only Exchange reads, and
  best-effort no-op audit writes.
- **Fix:** Point `DATA_DIR` at a user-owned local folder (such as `~/.ewsmcp` or
  `$HOME/.local/share/ewsmcp`), or create the target directory and grant ownership to the service
  user before starting `ewsmcp`, then restart and check that `get_server_status` shows
  `cache.ready: true`. See [DEPLOYMENT.md](DEPLOYMENT.md#the-data-directory).

### Two servers share one `DATA_DIR`

- **Symptom:** `get_server_status` reports `cache.sync.role: "follower"`, logs show
  `"EWS_CACHE_PURGE_ON_BOOT skipped: another process holds the sync lease"`,
  `verify_audit_chain.py` exits `1` with a hash mismatch, or a password change causes multiple
  `401` rejections across processes.
- **Cause:** Multiple local `ewsmcp` processes for the same mailbox coordinate background sync via
  an OS file lock on `DATA_DIR/cache/sync.lock`. The holder reports `role: "leader"` and runs
  `SyncEngine`; other processes report `role: "follower"`, read the shared SQLite mirror, skip
  `EWS_CACHE_PURGE_ON_BOOT`, and retry the lock every 30 seconds to take over if the leader exits.
  However, each process keeps its own Exchange session, `AuthBreaker` counter, send-cap counter,
  and in-memory audit chain head: after a password change each process probes the old password
  again after every cooldown up to `EWS_AUTH_MAX_REJECTIONS`, and interleaved audit records break
  `DATA_DIR/audit` by themselves. Also, `DATA_DIR` must never be shared across different mailboxes
  or over network filesystems such as NFS or SMB.
- **Fix:** Seeing `role: "follower"` on a second local `stdio` client for the same mailbox is
  normal for cached reads, though `verify_audit_chain.py` is meaningful only where one process
  writes to `DATA_DIR`. To run `EWS_CACHE_PURGE_ON_BOOT=true`, stop follower processes first or set
  it on the leader. To share one Exchange session, one lockout counter, one send cap, and one
  intact audit chain across clients, run one loopback HTTP server (`MCP_TRANSPORT=http`,
  `MCP_HOST=127.0.0.1`) instead; see [DEPLOYMENT.md](DEPLOYMENT.md#several-clients-on-one-machine).

## Error codes

Every error envelope uses one of these 12 codes; see [API.md](API.md#error-taxonomy):

| Code | HTTP status | Meaning |
|---|---|---|
| `validation` | 400 | Invalid or out-of-range arguments, unknown alias on single-item input, refused document/HTML/rule input, or unknown/above-tier tool name (`404` on REST); oversized REST bodies return `413` and unknown HTTP routes return `404` with this code |
| `auth_failed` | 401 | Missing/invalid `MCP_API_KEY`, Exchange HTTP `401`, or open `AuthBreaker` lockout guard |
| `tier_blocked` | 403 | Dispatcher second check when `EWS_CAPABILITY_TIER` is below the tool's minimum tier; clients do not normally reach this because above-tier tools are not registered |
| `kill_switch` | 403 | Outbound send, meeting response/cancellation, invitation, update notice, or auto-reply attempted while `SEND_ENABLED=false`; refused at phase one before a token is issued |
| `recipient_blocked` | 403 | Recipient in arguments or stored on the draft/event rejected by `EWS_RECIPIENT_DENYLIST` or `EWS_RECIPIENT_ALLOWLIST` (including stored event attendees with no address) |
| `not_found` | 404 | Unknown folder/rule/contact/attachment (including a draft `a<digits>` id issued for another message's attachment), unknown `m` alias in `get_messages.failed`, or stale Exchange item id after an external move or delete |
| `confirm_invalid` | 409 | `confirm_token` expired, already consumed, bad-signature, malformed, or stale after a target content or bound-argument change |
| `throttled` | 429 | Exchange asked the client to back off; includes `retry_after_s` and counts toward the circuit breaker |
| `rate_capped` | 429 | Hourly send limit `EWS_MAX_SENDS_PER_HOUR` reached across confirmed sending calls; checked after token verification and before consuming `confirm_token`, and includes `retry_after_s` |
| `internal` | 500 | Unwritable `DATA_DIR/attachments` folder or unavailable document worker; does not count toward the circuit breaker |
| `upstream_error` | 502 | Unrecognised Exchange failure or unexpected local exception mapped by `errors.map_exception`; counts toward the circuit breaker |
| `upstream_unavailable` | 503 | Initial Exchange warmup still `connecting`, Exchange unreachable/timed out, dispatcher circuit breaker open, or required local mirror / semantic index not ready; includes `retry_after_s` when applicable |
