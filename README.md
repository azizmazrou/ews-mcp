# EWS MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker Image](https://img.shields.io/badge/ghcr.io-ews--mcp-blue?logo=docker)](https://github.com/azizmazrou/ews-mcp/pkgs/container/ews-mcp)

> A Model Context Protocol server that gives an LLM assistant **real,
> typed control of a Microsoft Exchange mailbox** — mail, calendar,
> people, tasks, drafts and (explicitly gated) sending — speaking EWS
> natively. No Graph proxy, no Microsoft 365 connector. Works with
> Claude Code, Claude Desktop, Open WebUI, and any other MCP client.

## Two lines live in this repository

| Line | Where | Status | Container image |
|---|---|---|---|
| **4.5 — current** | [`v5/`](v5/) | Active development. **Recommended for all new setups.** | `ghcr.io/azizmazrou/ews-mcp:v4.5*` |
| 4.0 — legacy | [`src/`](src/) | Maintenance only. | `ghcr.io/azizmazrou/ews-mcp:latest` |

The 4.5 line is a greenfield rewrite: a consolidated **28-tool** surface,
short alias ids the model can actually copy, token-lean responses (~60-token
result cards instead of raw Outlook HTML), a cache-first local mirror with
Arabic-correct full-text search, and a safety model enforced in one place.
The legacy line keeps `:latest` stable for existing deployments until 4.5
is promoted. **If you are new here, use 4.5 and read
[`v5/README.md`](v5/README.md).**

## Quick start — 4.5 locally over stdio (recommended)

No Docker needed. The MCP client starts the server as a child process;
it inherits your machine's network (VPNs included), and nothing listens
on any port.

```bash
git clone https://github.com/azizmazrou/ews-mcp && cd ews-mcp
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install ./v5
```

Then register it with Claude Code:

```bash
claude mcp add exchange \
  -e EWS_SERVER_URL="https://mail.example.com/EWS/Exchange.asmx" \
  -e EWS_EMAIL="user@example.com" \
  -e EWS_USERNAME="user" -e EWS_PASSWORD="…" \
  -- /absolute/path/to/.venv/bin/ewsmcp
```

Claude Desktop and other clients: same command + env in a config block —
see the [4.5 quick start](v5/README.md#quick-start--run-it-locally-over-stdio-no-docker).
Defaults are safe: `draft` tier, sending disabled.

## Quick start — Docker / HTTP (server deployments)

When the server runs on a host that can reach Exchange directly:

```bash
# 4.5 (pin an exact tag; the 4.5 line never publishes :latest)
docker run -d --name ews-mcp-v5 -p 8000:8000 --env-file .env \
  -v ewsmcp-data:/data ghcr.io/azizmazrou/ews-mcp:v4.5.0a2

# legacy 4.0
docker run -d --name ews-mcp --env-file .env --network host \
  ghcr.io/azizmazrou/ews-mcp:latest
```

4.5 serves Streamable HTTP at `/mcp`, plain REST at `/api/tools/<name>`,
and health at `/livez` `/readyz` `/health`; see
[`v5/docker-compose.example.yml`](v5/docker-compose.example.yml) for a
single-service Compose example. Legacy transport docs:
[`docs/legacy/CONNECTION_GUIDE.md`](docs/legacy/CONNECTION_GUIDE.md).

## What the assistant can do (4.5)

- **Read fast** — `get_mailbox_overview` (morning brief in one call),
  `search_messages` (FTS over a local mirror, Arabic-correct, semantic
  mode optional), `get_message`, `get_thread`, `get_attachment`.
- **Calendar & people** — `list_events`, `get_event`,
  `check_availability`, `find_people`, `get_contact`.
- **Tasks & follow-ups** — `list_tasks`, `update_task`, `waiting_on`
  (sent threads nobody answered).
- **Write safely** — `create_draft` / `update_draft` (never send),
  labels, move; `send_draft` only exists at the `full` tier, behind a
  kill-switch, recipient allowlists, an hourly cap, and a two-phase
  **content-bound** confirmation that goes stale if the draft changes.
- **Operate** — `get_server_status` (connection, tier, cache watermarks —
  works even while Exchange is unreachable).

The complete, generated reference — every tool with its parameters,
envelope, error codes, and the v3→4.5 rename map — is
[`v5/docs/API.md`](v5/docs/API.md).

## The safety model (4.5)

| Mechanism | What it does |
|---|---|
| Capability tiers | `read` ⊂ `draft` ⊂ `full` — above-tier tools are not even registered |
| Kill-switch | `SEND_ENABLED=false` (default) refuses every send-class call |
| Recipient guards | Allow/deny globs on argument **and** draft-resolved recipients |
| Two-phase confirm | Preview + HMAC token bound to the draft's actual content; single-use |
| Rate cap | `EWS_MAX_SENDS_PER_HOUR` |
| Audit chain | Hash-chained log, verifier script, persists across restarts |

## Documentation

| | |
|---|---|
| [`docs/README.md`](docs/README.md) | **The documentation map** — start here |
| [`v5/README.md`](v5/README.md) | 4.5 install & use: stdio, HTTP, Docker, configuration |
| [`v5/docs/API.md`](v5/docs/API.md) | 4.5 API reference (generated from the registry) |
| [`v5/DESIGN.md`](v5/DESIGN.md) | 4.5 architecture and rationale |
| [`docs/legacy/`](docs/legacy/) | Legacy 4.0 documentation (`:latest` users) |
| [`examples/skills/exchange-assistant/`](examples/skills/exchange-assistant/) | Example assistant skill on top of the tool surface |

## Development

```bash
pip install -e ./v5[dev]
python -m pytest v5/tests -q            # full 4.5 suite — no Exchange needed
python -m ruff check v5
python v5/scripts/boot_smoke.py full    # end-to-end boot, dead endpoint
python v5/scripts/dump_tool_table.py --check   # docs ↔ registry drift gate
```

CI: `v5-tests` (blocking ruff + tests on 3.11/3.12 + boot smokes + Docker
import smoke) runs on every push touching `v5/`; `v5-publish` builds
`ghcr.io/…:v4.5*` from tags `v4.5.*`, gated on the full test job. The
legacy image publishes from `main` pushes and `v3.*`/`v4.0.*` tags only.

## Repository layout

```
v5/            the 4.5 server (package `ews-mcp`, entry point `ewsmcp`)
src/           the legacy 4.0 server (ships as :latest)
docs/          documentation map + legacy 4.0 docs
examples/      example assistant skill
.env.example   legacy 4.0 configuration template (4.5: v5/.env.example)
```

## Contributing & license

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT — see [LICENSE](LICENSE).
