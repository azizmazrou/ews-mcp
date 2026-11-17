# Documentation map

Two server lines live in this repository. Check which line you run before reading a guide.

## Current line — 4.5 (`v5/`, images `ghcr.io/…:v4.5*`)

| Page | What you find there | Who it is for |
|---|---|---|
| [`README.md`](../README.md) | Project overview, 4.5 vs legacy 4.0 comparison, quick starts, tool areas, safety summary | Anyone landing on the repository |
| [`v5/README.md`](../v5/README.md) | Local `stdio` install, MCP client configuration, safe defaults, and first prompts | Someone connecting an assistant to their mailbox |
| [`v5/docs/SAFETY.md`](../v5/docs/SAFETY.md) | Capability tiers, kill-switch, recipient lists, two-phase confirmation, lockout guard, HTML and attachment isolation, audit chain, and what is not promised | Users, operators, and security reviewers |
| [`v5/docs/CONFIGURATION.md`](../v5/docs/CONFIGURATION.md) | Every environment variable with its type, default, limits, and `.env` loading rules | Users and operators configuring `ewsmcp` |
| [`v5/docs/DEPLOYMENT.md`](../v5/docs/DEPLOYMENT.md) | Choosing a mode, local `stdio`, multiple local clients, HTTP server, Docker, Compose, health and metrics, audit logs, `DATA_DIR`, and updates | Someone running `ewsmcp` on a host or in a container |
| [`v5/docs/API.md`](../v5/docs/API.md) | Generated tool schemas, canonical envelope, short alias ids, error codes, confirmation flow, cache freshness, multi-folder search, drafts, attachments, inbox rules, and v3 rename and drop lists | Someone building a client or assistant skill |
| [`v5/docs/TROUBLESHOOTING.md`](../v5/docs/TROUBLESHOOTING.md) | Starting with `get_server_status`, connection and `auth_failed` recovery, refused calls, stale ids, attachments, HTML drafts, HTTP clients, storage, and error codes | Anyone diagnosing a failure |
| [`v5/docs/UPGRADING.md`](../v5/docs/UPGRADING.md) | Versions and container tags, upgrading across 4.5 releases, and migrating from 3.x or 4.0 | Existing users and operators upgrading |
| [`v5/docs/ARCHITECTURE.md`](../v5/docs/ARCHITECTURE.md) | Request path, SQLite mirror vs live EWS, alias store, hostile-input handling, transports, disk layout, breakers and retries, code map, and test gates | Contributors and operators reading internals |
| [`v5/DESIGN.md`](../v5/DESIGN.md) | Design laws and invariants enforced across `v5/` | Contributors changing the 4.5 server |
| [`examples/skills/exchange-assistant/SKILL.md`](../examples/skills/exchange-assistant/SKILL.md) | Example assistant skill for morning overview, triage, draft-first replies, and scheduling | Skill and prompt authors |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Repository layout, filing issues, pull-request proposals, local checks from `v5/`, and code style | Contributors |
| [`SECURITY.md`](../SECURITY.md) | Supported versions, how to report a vulnerability, response expectations, and security scope | Security researchers and operators |
| [`CHANGELOG.md`](../CHANGELOG.md) | Release notes and unreleased changes on `main` | Anyone checking what changed between versions |
| [`v5/.env.example`](../v5/.env.example) | Annotated environment variable template for 4.5 | Users and operators creating a `.env` file |
| [`v5/docker-compose.example.yml`](../v5/docker-compose.example.yml) | Single-service Docker Compose example on loopback at `read` tier with sending disabled | Operators deploying with Docker Compose |

## Legacy line — 4.0 (`src/`, ships as `:latest`)

Everything under [`legacy/`](legacy/) describes the legacy 4.0 server only:

| Document | What it covers |
|---|---|
| [`legacy/SETUP.md`](legacy/SETUP.md) | Installation and first run |
| [`legacy/CONNECTION_GUIDE.md`](legacy/CONNECTION_GUIDE.md) | Claude Desktop / Open WebUI / SSE connection recipes |
| [`legacy/API.md`](legacy/API.md) | Legacy tool reference |
| [`legacy/ARCHITECTURE.md`](legacy/ARCHITECTURE.md) | Legacy internals |
| [`legacy/DEPLOYMENT.md`](legacy/DEPLOYMENT.md) | Docker / compose deployment |
| [`legacy/TROUBLESHOOTING.md`](legacy/TROUBLESHOOTING.md) | Common failures and fixes, including the password-change retry caution |
| [`legacy/AGENT_SECRETARY.md`](legacy/AGENT_SECRETARY.md) | The legacy agent-secretary feature stack |
| [`legacy/IMPERSONATION.md`](legacy/IMPERSONATION.md) | Multi-mailbox / delegate access |
| [`legacy/REPLY_FORWARD.md`](legacy/REPLY_FORWARD.md) | Reply/forward formatting details |
| [`legacy/COMMON_PITFALLS.md`](legacy/COMMON_PITFALLS.md) | Recurring regression patterns and developer notes |

The 4.5 line omits several legacy features by design: one-shot send tools, impersonation, OAuth2,
contact and folder management, raw MIME content in tool responses, and the agent-secretary stack.
See [`v5/docs/API.md`](../v5/docs/API.md#intentionally-dropped-vs-v3) and
[`v5/docs/UPGRADING.md`](../v5/docs/UPGRADING.md#from-the-legacy-line-3x-and-40).
