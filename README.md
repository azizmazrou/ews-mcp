# EWS MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker Image](https://img.shields.io/badge/ghcr.io-ews--mcp-blue?logo=docker)](https://github.com/azizmazrou/ews-mcp/pkgs/container/ews-mcp)

EWS MCP Server is a Model Context Protocol (`MCP`) server that connects an AI assistant to a
Microsoft Exchange mailbox over Exchange Web Services (`EWS`). It reads and searches mail, manages
drafts, calendar events, contacts, tasks, and inbox rules, and places outgoing mail behind operator
controls and two-phase confirmation. It talks to Exchange directly without Microsoft Graph or a
Microsoft 365 connector, and works with Claude Code, Claude Desktop, Open WebUI, and other MCP
clients.

## Two lines live in this repository

Choose the 4.5 line for every new installation. The legacy 4.0 line stays on `:latest` only so
existing deployments do not break before 4.5 is promoted.

| Line | Directory | Status | Container image |
|---|---|---|---|
| **4.5 — current** | [`v5/`](v5/) | Active development. Recommended for all new setups. | `ghcr.io/azizmazrou/ews-mcp:v4.5*` |
| 4.0 — legacy | [`src/`](src/) | Maintenance only. Kept stable for `:latest` users. | `ghcr.io/azizmazrou/ews-mcp:latest` |

The 4.5 line provides 34 tools, short alias ids such as `m12` and `e3`, cleaned text cards instead
of raw Outlook HTML, a local SQLite mirror with Arabic-aware full-text search, and one central
safety gate. Start with [`v5/README.md`](v5/README.md).

Legacy 4.0 (`:latest`) users: stop the server before or right after an Exchange password change,
update `EWS_PASSWORD`, and start it again. The 4.0 server retries `HTTP 401` responses as
throttling and can trigger a directory lockout; see
[`docs/legacy/TROUBLESHOOTING.md`](docs/legacy/TROUBLESHOOTING.md#authentication-issues). The 4.5
line stops on the first rejection.

## Quick start — 4.5 locally over stdio (recommended)

In `stdio` mode, your MCP client launches `ewsmcp` as a local child process and talks to it over
standard input and output. No network port opens on your machine, and the server uses your
workstation network and VPN. Python 3.11 or newer is required.

Clone the repository and install from the repository root:

```bash
git clone https://github.com/azizmazrou/ews-mcp.git
cd ews-mcp
python3 -m venv .venv        # Windows: python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install "./v5[extract]"
```

This creates `.venv/bin/ewsmcp`, or `.venv\Scripts\ewsmcp.exe` on Windows. Use that absolute path
when registering the server.

Register with Claude Code. Add `--scope user` to make it available in every project, or run the
command in the project where you will use it:

```bash
claude mcp add exchange \
  -e EWS_SERVER_URL="https://mail.example.com/EWS/Exchange.asmx" \
  -e EWS_EMAIL="you@example.com" \
  -e EWS_USERNAME="you" \
  -e EWS_PASSWORD="change-me" \
  -- /path/to/ews-mcp/.venv/bin/ewsmcp
```

For Claude Desktop (`claude_desktop_config.json`) or another MCP client, add the command and
credentials to `mcpServers`:

```json
{
  "mcpServers": {
    "exchange": {
      "command": "/path/to/ews-mcp/.venv/bin/ewsmcp",
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

On Windows, set `command` to `C:\\path\\to\\ews-mcp\\.venv\\Scripts\\ewsmcp.exe`. Defaults start at
the `draft` capability tier with `SEND_ENABLED=false`, so nothing can send mail until you enable
it. Full setup instructions and `.env` usage are in
[`v5/README.md`](v5/README.md#quick-start--run-it-locally-over-stdio-no-docker).

## Quick start — Docker / HTTP (server deployments)

Run the container when the server lives on a host whose own network reaches Exchange directly. Copy
[`v5/.env.example`](v5/.env.example) to `.env`, fill in your Exchange settings and `MCP_API_KEY`,
and start the 4.5 container from the directory holding `.env`:

```bash
# 4.5 — pin an exact tag; the 4.5 line never publishes :latest
docker run -d --name ews-mcp-v5 -p 127.0.0.1:8000:8000 --env-file .env \
  -v ewsmcp-data:/data ghcr.io/azizmazrou/ews-mcp:v4.5.0a4
```

To build from a local checkout instead, run `docker build -t ews-mcp:dev v5` from the repository
root.

In HTTP mode, 4.5 serves Streamable HTTP at `/mcp`, JSON REST at `/api/tools/{name}`, OpenAPI at
`/openapi.json`, Prometheus metrics at `/metrics`, and health checks at `/livez`, `/readyz`, and
`/health`. See [`v5/docs/DEPLOYMENT.md`](v5/docs/DEPLOYMENT.md) and
[`v5/docker-compose.example.yml`](v5/docker-compose.example.yml) for server setups.

Legacy 4.0 uses different variable names. Copy the root [`.env.example`](.env.example) to `.env`,
fill in your credentials, and start the legacy container:

```bash
docker run -d --name ews-mcp --env-file .env --network host \
  ghcr.io/azizmazrou/ews-mcp:latest
```

Legacy deployment and connection guides are in
[`docs/legacy/DEPLOYMENT.md`](docs/legacy/DEPLOYMENT.md) and
[`docs/legacy/CONNECTION_GUIDE.md`](docs/legacy/CONNECTION_GUIDE.md).

## What the assistant can do (4.5)

The default 4.5 registry exposes **34** tools across eight areas, plus `find_similar` when
`EWS_SEMANTIC_INDEX=pgvector`.

| Area | What you can ask for | Tools |
|---|---|---|
| Mail reading and search | Morning overview, folder tree search, keyword or Arabic-normalized full-text search across one folder or `f:all`, batch sender/subject checks, cleaned single or batch messages, `.eml` export, full threads, and attachment or document text reading | `get_mailbox_overview`, `list_folders`, `search_messages`, `check_messages`, `get_message`, `get_messages`, `get_thread`, `get_attachment`, `find_similar` |
| Drafts and sending | Create text or sanitised HTML drafts, edit recipients or remove attachments, attach files or existing messages, discard drafts, and send after a content-bound preview | `create_draft`, `update_draft`, `add_draft_attachment`, `delete_draft`, `send_draft` |
| Mail filing and cleanup | Mark messages read or unread, add or remove categories, mark follow-up flags complete, move up to 50 messages, or delete messages | `update_messages`, `move_messages`, `delete_messages` |
| Calendar and automatic replies | List expanded events, inspect attendance and recurrence rules, find free slots, create or update single and recurring events, accept or decline invitations, cancel meetings you organize, and read or set out-of-office replies | `list_events`, `get_event`, `check_availability`, `create_event`, `update_event`, `respond_to_event`, `cancel_event`, `get_oof_settings`, `set_oof` |
| People and directory | Look up colleagues across the Global Address List (`GAL`) and personal contacts, or inspect one contact with mail history | `find_people`, `get_contact` |
| Tasks and follow-ups | List tasks, mark a task complete or change its due date, and list sent threads that have no reply | `list_tasks`, `update_task`, `waiting_on` |
| Inbox rules | Inspect server-side rules, create or replace mailbox-internal filing and tagging rules, and delete rules | `list_rules`, `save_rule`, `delete_rule` |
| Operations | Check connection state, capability tier, kill-switch, tool count, and cache sync status even when Exchange is unreachable | `get_server_status` |

Capability tiers expose **18** tools at `read`, **29** at `draft` by default, and **34** at `full`,
or **35** when the semantic index is enabled. Every tool, argument, response envelope, error code,
and v3-to-4.5 rename is documented in [`v5/docs/API.md`](v5/docs/API.md).

## The safety model (4.5)

Releases before `v4.5.0a4` have gaps in these gates; see
[`v5/docs/UPGRADING.md`](v5/docs/UPGRADING.md).

Every tool call passes through one central gate chain before reaching Exchange. Full details and
non-goals are in [`v5/docs/SAFETY.md`](v5/docs/SAFETY.md).

| Mechanism | What it does |
|---|---|
| Capability tiers | `EWS_CAPABILITY_TIER` exposes `read` with 18 tools, `draft` with 29 tools by default, or `full` with 34 tools; tools above the tier are not registered. |
| Kill-switch | `SEND_ENABLED=false` by default; refuses `send_draft`, `respond_to_event`, `set_oof`, and any `create_event`, `update_event`, or `cancel_event` call that would notify attendees, at phase one before issuing a token. |
| Lockout guard | Stops on the first `HTTP 401`, cools down for `EWS_AUTH_FAILURE_COOLDOWN_SECONDS` at `900` s by default, and stays open until restart after `EWS_AUTH_MAX_REJECTIONS` rejections since the last successful login, `3` by default. |
| Recipient guards | `EWS_RECIPIENT_ALLOWLIST` and `EWS_RECIPIENT_DENYLIST` check address globs on recipient arguments and on the stored recipients of `send_draft`, `update_event` when notifying attendees, and `cancel_event`. |
| Two-phase confirm | Returns a preview and a single-use HMAC `confirm_token` bound to the target content or arguments, including draft subject, recipients, body, and attachment metadata; changes invalidate the token. |
| Rate cap | `EWS_MAX_SENDS_PER_HOUR` caps confirmed calls that send — `send_draft`, `respond_to_event`, `set_oof`, invitations, update notices, and cancellations — at `10` per rolling hour per process in memory by default, keeping `confirm_token` unspent if capped. |
| Audit chain | Best-effort hash-chained JSONL log of tool calls and target ids under `DATA_DIR/audit`; `v5/scripts/verify_audit_chain.py` checks single-writer directories for accidental damage. |

## Documentation

Start at [`docs/README.md`](docs/README.md) for the complete map, including legacy 4.0 guides.

| Page | What you find there |
|---|---|
| [`docs/README.md`](docs/README.md) | Complete documentation map for 4.5 and legacy 4.0 |
| [`v5/README.md`](v5/README.md) | 4.5 getting started: local `stdio` install, client config, first prompts |
| [`v5/docs/SAFETY.md`](v5/docs/SAFETY.md) | Safety controls, HTML and document handling, and what is not promised |
| [`v5/docs/CONFIGURATION.md`](v5/docs/CONFIGURATION.md) | Every environment variable, default, limit, and `.env` rule |
| [`v5/docs/DEPLOYMENT.md`](v5/docs/DEPLOYMENT.md) | Running over `stdio`, shared `DATA_DIR`, HTTP, Docker, Compose, health, and updates |
| [`v5/docs/API.md`](v5/docs/API.md) | Generated tool schemas, response envelopes, id aliases, error codes, and v3 rename map |
| [`v5/docs/TROUBLESHOOTING.md`](v5/docs/TROUBLESHOOTING.md) | Symptoms, causes, fixes, and error code reference |
| [`v5/docs/UPGRADING.md`](v5/docs/UPGRADING.md) | Upgrading between 4.5 tags and migrating from 3.x or 4.0 |
| [`v5/docs/ARCHITECTURE.md`](v5/docs/ARCHITECTURE.md) | Request flow, cache mirror, alias store, isolated workers, and code map |
| [`v5/DESIGN.md`](v5/DESIGN.md) | Architectural rules and invariants enforced by the test suite |
| [`SECURITY.md`](SECURITY.md) | Supported versions, vulnerability reporting, and security scope |
| [`examples/skills/exchange-assistant/SKILL.md`](examples/skills/exchange-assistant/SKILL.md) | Example assistant skill built on the 4.5 tools |
| [`docs/legacy/`](docs/legacy/) | Legacy 4.0 setup, connection, API, deployment, and troubleshooting guides |

## Development

Install the development dependencies and run all four checks from `v5/`:

```bash
cd v5
pip install -e ".[dev]"
python -m pytest tests -q
python -m ruff check .
python scripts/boot_smoke.py full
python scripts/dump_tool_table.py --check
```

The test suite runs offline without an Exchange server. In CI, `v5-tests` runs `ruff`, `pytest` on
Python 3.11 and 3.12, `dump_tool_table.py --check`, `boot_smoke.py`, and a Docker import check on
changes touching `v5/`. `v5-publish` builds `ghcr.io/azizmazrou/ews-mcp:v4.5*` on `v4.5.*` tags and
manual `workflow_dispatch` runs, gated on `v5-tests`. Legacy images publish `:latest` on `main`
pushes and the version tags of the legacy line.

## Repository layout

```text
v5/            4.5 server (package ews-mcp, entry point ewsmcp)
src/           legacy 4.0 server (ships as :latest)
docs/          documentation map and legacy 4.0 guides
examples/      example assistant skill
.env.example   legacy 4.0 configuration template; 4.5 uses v5/.env.example
```

## Contributing & license

Contribution workflow and local setup are in [`CONTRIBUTING.md`](CONTRIBUTING.md). Security
reporting is in [`SECURITY.md`](SECURITY.md). Licensed under MIT; see [`LICENSE`](LICENSE).
