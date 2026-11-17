# ews-mcp 4.5 — Exchange (EWS) for MCP assistants

`ews-mcp` connects an MCP client to a Microsoft Exchange mailbox over Exchange Web Services
(`EWS`). It exposes **34 tools** for mail, calendar, contacts, tasks, and inbox rules, uses short
alias ids instead of raw Exchange ids, keeps a local SQLite mirror for fast search, and blocks
outgoing sends until you explicitly enable and confirm them.

> The `v5/` folder is an internal repository path for the **4.5.x** release line. Container images
> publish as `ghcr.io/azizmazrou/ews-mcp:v4.5*` and never `:latest`. For the legacy 4.0 server,
> see [`../docs/README.md`](../docs/README.md).

## Where to go next

| Page | What it covers |
|---|---|
| [`docs/API.md`](docs/API.md) | Every tool, parameter, response envelope, alias id, and error code |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | Every environment setting, its default, limits, and meaning |
| [`docs/SAFETY.md`](docs/SAFETY.md) | Capability tiers, kill-switch, recipient rules, two-phase confirmation, and limits |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Running over `stdio`, several clients, HTTP, Docker, Compose, health, and storage |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | Symptoms, causes, fixes, and error codes |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the server is built: mirror, dispatcher, sanitiser, document worker, and transports |
| [`docs/UPGRADING.md`](docs/UPGRADING.md) | Changes between 4.5 releases and migrating from the legacy 4.0 line |

## Quick start — run it locally over stdio (no Docker)

`stdio` is the default transport. Your MCP client starts `ewsmcp` as a local child process and
talks to it over standard input and output. Nothing listens on a network port and no API key is
needed. Because `ewsmcp` runs on your machine, it reaches Exchange through your machine's network
routes, including a workstation VPN.

**1. Install from the repository root** — requires Python 3.11 or newer:

```bash
git clone https://github.com/azizmazrou/ews-mcp && cd ews-mcp
python3 -m venv .venv        # Windows: python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install "./v5[extract]"      # or: pip install ./v5 without PDF text reading
```

This creates `.venv/bin/ewsmcp` on Linux and macOS, or `.venv\Scripts\ewsmcp.exe` on Windows. Use
its absolute path in the client configuration below.

**2. Connect Claude Code** — run in the project where you will use it, or add `--scope user` to
make it available in every project, then restart the session:

```bash
claude mcp add exchange \
  -e EWS_SERVER_URL="https://mail.example.com/EWS/Exchange.asmx" \
  -e EWS_EMAIL="user@example.com" \
  -e EWS_USERNAME="user" \
  -e EWS_PASSWORD="…" \
  -- /absolute/path/to/.venv/bin/ewsmcp
```

`claude mcp add` registers for the current project unless `--scope user` is given. On Windows,
write the command on one line and point to `ewsmcp.exe`. Run `claude mcp list` to confirm
`exchange` shows as connected.

**3. Or connect Claude Desktop** — open `Settings` → `Developer` → `Edit Config` and add this
block to `claude_desktop_config.json`:

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

Any other MCP client uses the same `ewsmcp` command path and `env` variables. If you prefer a
configuration file over client `env` entries, copy [`.env.example`](.env.example) to `.env` in the
working directory where `ewsmcp` starts; `ewsmcp` loads `.env` automatically.

**Safe defaults out of the box:**

| Setting | Default | Effect |
|---|---|---|
| `EWS_CAPABILITY_TIER` | `draft` | Exposes read and write tools only; `full`-tier tools such as `send_draft` and `delete_messages` are not registered |
| `SEND_ENABLED` | `false` | Refuses every call that sends — drafts, meeting mail, and automatic replies — with `kill_switch` at phase one |
| `DATA_DIR` | `~/.ewsmcp` | Stores aliases, audit log, and SQLite cache locally; synced folders such as OneDrive, Dropbox, Google Drive, and iCloud are refused |

If your home directory is inside a cloud-synced folder, set `DATA_DIR` to a plain local directory.

**4. Check that it works:**

Ask your assistant for the Exchange server status, which calls `get_server_status`. It answers at
any time: while the server is still connecting, after Exchange rejected the credentials, and while
the circuit breaker is open. Once login finishes, `connection.state` reports `"warm"`. During
initial startup it reports `"connecting"`, and if Exchange rejects the password it reports
`"auth_failed"`.

**5. Three first things to ask your assistant:**

1. *"Give me an overview of my unread mail and today's calendar."* — calls `get_mailbox_overview`
   and returns short alias ids such as `m1` and `e1` instead of raw Exchange ids.
2. *"Find recent messages from `ops@example.com` and read the latest one."* — calls
   `search_messages` and then `get_message` with the returned `m`-alias.
3. *"Draft a short reply to `m1` saying I will confirm tomorrow."* — calls `create_draft` and
   saves the reply in `Drafts` without sending it.

## What the tools cover

`EWS_CAPABILITY_TIER` exposes **18** tools at `read`, **29** at `draft`, and **34** at `full`. Full
parameter and response details live in [`docs/API.md`](docs/API.md).

| Area | Count | Tier needed | Tools |
|---|---|---|---|
| Mail read | 8 | `read` | `list_folders`, `search_messages`, `check_messages`, `get_message`, `get_messages`, `get_thread`, `get_attachment`, `get_mailbox_overview` |
| Calendar, people, and status | 7 | `read` | `list_events`, `get_event`, `check_availability`, `find_people`, `get_contact`, `get_oof_settings`, `get_server_status` |
| Tasks and follow-ups — read | 2 | `read` | `list_tasks`, `waiting_on` |
| Inbox rules — read | 1 | `read` | `list_rules` |
| Drafts, filing, and calendar edits | 8 | `draft` | `create_draft`, `update_draft`, `add_draft_attachment`, `delete_draft`, `update_messages`, `move_messages`, `create_event`, `update_event` |
| Task updates | 1 | `draft` | `update_task` |
| Inbox rules — write | 2 | `draft` | `save_rule`, `delete_rule` |
| Sending, meeting replies, deletion, and OOF | 5 | `full` | `send_draft`, `respond_to_event`, `cancel_event`, `delete_messages`, `set_oof` |

Setting `EWS_SEMANTIC_INDEX=pgvector` with `EWS_SEMANTIC_PG_DSN` registers one extra `read`-tier
tool, `find_similar`, and enables semantic ranking in `search_messages`.

## The send flow (two-phase, content-bound)

Ordinary mail leaves the mailbox only through `send_draft`. Meeting mail — invitations, update
notices, responses, and cancellations — is sent by the calendar tools. While `SEND_ENABLED` is
`false`, nothing that sends is allowed: every sending call is refused with `kill_switch` at phase
one, before any confirmation token is issued.

- Set `SEND_ENABLED=true` to lift the global send kill-switch.
- Set `EWS_CAPABILITY_TIER=full` to register `send_draft`, `respond_to_event`, `cancel_event`, and
  `set_oof`. Meeting invitations on `create_event` and update notices on `update_event` need only
  tier `draft`, `SEND_ENABLED=true`, and a confirmation.
- Set `SEND_CONFIRM_SECRET` to a long random string so confirmation tokens stay valid across
  server restarts; when unset, `ewsmcp` generates a fresh in-memory secret on each boot.

Once enabled, sending a message always takes two tool calls so the assistant and user review what
Exchange actually holds before anything leaves the mailbox:

1. **Create a draft.** `create_draft` saves the message in `Drafts` and returns a `draft_id` alias
   such as `d1` with `"saved as draft — NOT sent"`.
2. **Preview the send.** Calling `send_draft(draft_id="d1")` without `confirm_token` sends nothing;
   the server reads `d1` from Exchange, checks recipients against `EWS_RECIPIENT_ALLOWLIST` and
   `EWS_RECIPIENT_DENYLIST`, inspects links in the body, and returns `preview`, `warnings`, and a
   single-use `confirm_token` valid for `CONFIRM_TTL_SECONDS`, which defaults to `600` seconds.
3. **Bind the token to the content.** The HMAC `confirm_token` binds the mailbox, tool name,
   `draft_id`, expiry, and a SHA-256 hash of `subject`, sorted `to`/`cc`/`bcc`, text and HTML
   bodies, and attachment ids, names, and sizes.
4. **Show the preview to the user.** The assistant presents the server-returned recipients,
   subject, body snippet, attachments, link hosts, and warnings, then waits for approval.
5. **Confirm and send.** Calling `send_draft(draft_id="d1", confirm_token="…")` verifies the token,
   rejects an already-used token with `confirm_invalid`, and checks the in-memory per-process
   `EWS_MAX_SENDS_PER_HOUR` cap — `10` per hour by default across all confirmed calls that send —
   before consuming the token. A call refused with `rate_capped` keeps its token, which works
   again once the hour has room if it has not expired. Otherwise the server re-fetches `d1` under
   a per-draft lock, re-checks its content hash, aborts with `confirm_invalid` if anything
   changed, or sends that exact draft.

Link inspection guarantees, recipient rules, and threat boundaries live in
[`docs/SAFETY.md`](docs/SAFETY.md). Token mechanics, `idempotency_key` replay, and response shapes
live in [`docs/API.md#two-phase-confirmation`](docs/API.md#two-phase-confirmation).

## Configuration (env)

`ewsmcp` reads case-insensitive environment variables and any `.env` file in its working
directory; see [`.env.example`](.env.example) for a starter template. New setups typically touch
at most these ten settings:

| Variable | Default | Meaning |
|---|---|---|
| `EWS_SERVER_URL` | *(required)* | Exchange EWS endpoint URL, such as `https://mail.example.com/EWS/Exchange.asmx` |
| `EWS_EMAIL` | *(required)* | Primary SMTP address of the mailbox; also used as login username when `EWS_USERNAME` is unset |
| `EWS_USERNAME` | `None` | Exchange login username when different from `EWS_EMAIL` |
| `EWS_PASSWORD` | `None` | Exchange account password; treated as `""` when unset |
| `EWS_AUTH_TYPE_FORCE` | `None` | Pin `basic`, `ntlm`, or `digest`; unset auto-negotiates |
| `EWS_TZ` | `UTC` | IANA time zone name for date grammar, recurring series, and formatted timestamps |
| `EWS_CAPABILITY_TIER` | `draft` | Tool surface tier: `read`, `draft`, or `full`; tools above the tier are not registered |
| `SEND_ENABLED` | `false` | Global send kill-switch; while `false`, refuses every call that sends with `kill_switch` at phase one |
| `SEND_CONFIRM_SECRET` | `None` | HMAC secret for `confirm_token` signing across restarts; random per process when unset |
| `DATA_DIR` | `""` | Local directory for aliases, audit log, and mirror; empty resolves to `~/.ewsmcp` |

All 45 settings — covering timeouts, lockout guard, HTTP mode, attachment limits, cache sync, and
semantic search — are documented in [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

## Several clients on one machine

Multiple `ewsmcp` `stdio` processes can share one `DATA_DIR`: one process locks `cache/sync.lock`
and runs background delta sync, while the others read the shared SQLite mirror as followers and
take over within `30` seconds if the leader exits. Each `stdio` process still keeps its own
Exchange login and lockout guard. To share one Exchange session as well, run a single HTTP server
on loopback with `MCP_TRANSPORT=http` and `MCP_HOST=127.0.0.1`. Setup details live in
[`docs/DEPLOYMENT.md#several-clients-on-one-machine`](docs/DEPLOYMENT.md#several-clients-on-one-machine).

## HTTP transport (only when the server runs on another machine)

When `ewsmcp` runs on another host or in a container, start it with
`MCP_TRANSPORT=http MCP_PORT=8000 MCP_API_KEY=<long-random-string> ewsmcp` to serve Streamable
HTTP at `/mcp`, JSON tool calls at `POST /api/tools/<name>`, and probes at `/livez`, `/readyz`,
and `/health`. This listens on `127.0.0.1` only. To serve other machines, put a TLS reverse proxy
in front of it, or set `MCP_HOST=0.0.0.0` behind a firewall. A container uses its host's network
rather than a workstation VPN:

```bash
# from the repository root; the 4.5 image is built from v5/
# (`docker build .` there builds the legacy 4.0 image instead)
docker build -t ews-mcp:dev v5
# .env is your copy of v5/.env.example
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env -v ewsmcp-data:/data ews-mcp:dev
```

Client bridging, authentication headers, [`docker-compose.example.yml`](docker-compose.example.yml),
and pinned `ghcr.io/azizmazrou/ews-mcp:v4.5*` images are covered in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Health & operations

Over `stdio` or HTTP, `get_server_status` answers at any time: while the server is still
connecting, after Exchange rejected the credentials, and while the circuit breaker is open,
reporting connection state, capability tier, kill-switch state, and cache sync status. While the
breaker is open, `connection` also carries `circuit_open: true` and `retry_after_s`. HTTP mode
adds `/livez`, `/readyz`, `/health`, `/version`, and `/metrics`. Where one process writes to
`DATA_DIR`, run `python scripts/verify_audit_chain.py ~/.ewsmcp/audit` (use your `DATA_DIR` if you
set one) from `v5/` to check the audit log. Full details live in
[`docs/DEPLOYMENT.md#health-readiness-and-metrics`](docs/DEPLOYMENT.md#health-readiness-and-metrics)
and [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

### When Exchange rejects the credentials (`auth_failed`)

- **Stops on the first rejection:** On the first HTTP `401` from Exchange, `ewsmcp` fails at once
  without retrying and sets `connection.state` to `"auth_failed"` so a wrong password cannot loop
  into a directory account lockout.
- **Cooldown and rejection cap:** The authentication breaker blocks Exchange calls for
  `EWS_AUTH_FAILURE_COOLDOWN_SECONDS`, default `900` seconds and minimum `60`, and stays open
  until restart after `EWS_AUTH_MAX_REJECTIONS` rejections since the last successful login,
  default `3` and range `1` to `10` including concurrent calls already in flight.
- **Fix and restart:** Credentials are read once at startup; update `EWS_EMAIL`, `EWS_USERNAME`,
  `EWS_PASSWORD`, or `EWS_AUTH_TYPE_FORCE` and restart `ewsmcp`. See
  [`docs/TROUBLESHOOTING.md#exchange-rejects-the-credentials`](docs/TROUBLESHOOTING.md#exchange-rejects-the-credentials).

## Why it looks like this

- **Token economy:** Search returns ~60-token cards, message bodies strip quoted history and
  signatures once at sync time, and raw HTML requires an explicit flag.
- **Copyable ids:** Tools emit short persistent aliases such as `m12` and `e3` instead of ~150-char
  base64 Exchange ids that change whenever items move.
- **Safety in one place:** Every tool call passes through one dispatcher that enforces the
  kill-switch, capability tier, recipient rules, content-bound two-phase confirmation, and hourly
  send cap.
- **Cache-first reads:** Background `SyncFolderItems` delta sync keeps a local SQLite FTS5 mirror
  with Arabic orthographic normalization, `source`/`as_of` provenance, and live EWS fallback.
- **Never-exit boot and lockout guard:** Startup never waits on Exchange — warmup runs in the
  background — and HTTP `401` responses trip a cooldown and rejection cap instead of retrying bad
  credentials.

Architecture walkthrough and design rules: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`DESIGN.md`](DESIGN.md).

## Example assistant skill

[`examples/skills/exchange-assistant/SKILL.md`](../examples/skills/exchange-assistant/SKILL.md)
shows how an assistant skill composes these tools into a daily routine: morning overview, inbox
triage, and reply drafting with two-phase confirmation. The server stays a deterministic data
plane; priorities, tone, and judgment live in the calling assistant.

## Upgrading from 4.5.0a2

`4.5.0a3` requires `exchangelib==5.6.0`, adds the Inbox rule tools `list_rules`, `save_rule`, and
`delete_rule`, and elects one background sync leader when several processes share `DATA_DIR`;
reinstall from the repository root with `pip install -U ./v5`. Full notes, including changes in
`v4.5.0a4`, live in [`docs/UPGRADING.md#from-450a2`](docs/UPGRADING.md#from-450a2) and
[`docs/UPGRADING.md#to-450a4-from-450a3`](docs/UPGRADING.md#to-450a4-from-450a3).

## Upgrading from 4.5.0a1

Later `4.5` releases default `EWS_TZ` to `UTC`, require `mcp>=1.30`, replace the host-specific
Compose file with [`docker-compose.example.yml`](docker-compose.example.yml), surface
`auth_failed` on credential rejection, and bound multi-folder `search_messages` calls. Full
upgrade notes from `4.5.0a1` and from the legacy `3.x`/`4.0` line live in
[`docs/UPGRADING.md#from-450a1`](docs/UPGRADING.md#from-450a1).

## Development

Run the development commands from `v5/`:

```bash
cd v5                              # everything below runs from v5/, not the repository root
pip install -e ".[dev]"
python -m pytest tests -q          # the full suite, no Exchange needed
python -m ruff check .
python scripts/boot_smoke.py full  # end-to-end boot against a dead endpoint
python scripts/dump_tool_table.py --check   # docs vs registry drift gate
```
