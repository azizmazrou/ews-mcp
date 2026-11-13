# Documentation map

Two server lines live in this repository — make sure you read the docs
for the one you run.

## Current line — 4.5 (`v5/`, images `ghcr.io/…:v4.5*`)

| Document | What it covers |
|---|---|
| [`v5/README.md`](../v5/README.md) | Install & use: **stdio quick start (no Docker)** for Claude Code / Claude Desktop / any MCP client, tool overview, HTTP transport, Docker, full configuration reference, the send flow, health & `auth_failed` recovery, multi-client setup, upgrade notes |
| [`v5/docs/API.md`](../v5/docs/API.md) | **Full API reference, generated from the registry**: every tool with parameters, the canonical envelope, id aliases, error taxonomy, two-phase confirmation, cache freshness contract, multi-folder search, v3→4.5 rename map, intentionally-dropped list |
| [`v5/DESIGN.md`](../v5/DESIGN.md) | Architecture and rationale: dispatcher gate chain, alias store, cache mirror, body cleaning, audit chain |
| [`v5/.env.example`](../v5/.env.example) | Annotated configuration template |
| [`v5/docker-compose.example.yml`](../v5/docker-compose.example.yml) | Single-service Docker Compose example (loopback port, read tier, sends off) |
| [`examples/skills/exchange-assistant/`](../examples/skills/exchange-assistant/) | Example assistant skill composed on top of the tool surface |

## Legacy line — 4.0 (`src/`, ships as `:latest`)

Everything under [`legacy/`](legacy/) describes the legacy server only:

| Document | What it covers |
|---|---|
| [`legacy/SETUP.md`](legacy/SETUP.md) | Installation and first run |
| [`legacy/CONNECTION_GUIDE.md`](legacy/CONNECTION_GUIDE.md) | Claude Desktop / Open WebUI / SSE connection recipes |
| [`legacy/API.md`](legacy/API.md) | Legacy tool reference |
| [`legacy/ARCHITECTURE.md`](legacy/ARCHITECTURE.md) | Legacy internals |
| [`legacy/DEPLOYMENT.md`](legacy/DEPLOYMENT.md) | Docker / compose deployment |
| [`legacy/TROUBLESHOOTING.md`](legacy/TROUBLESHOOTING.md) | Common failures and fixes (including password-change retry caution) |
| [`legacy/AGENT_SECRETARY.md`](legacy/AGENT_SECRETARY.md) | The legacy agent-secretary feature stack |
| [`legacy/IMPERSONATION.md`](legacy/IMPERSONATION.md) | Multi-mailbox / delegate access |
| [`legacy/REPLY_FORWARD.md`](legacy/REPLY_FORWARD.md) | Reply/forward formatting details |
| [`legacy/COMMON_PITFALLS.md`](legacy/COMMON_PITFALLS.md) | Recurring regression patterns (developer notes) |

The 4.5 line intentionally does not implement some legacy features
(impersonation, OAuth2, folder management, MIME export, the
agent-secretary stack) — the full list and reasoning are in
[`v5/docs/API.md`](../v5/docs/API.md).
