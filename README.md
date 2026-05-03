# EWS MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker Image](https://img.shields.io/badge/ghcr.io-ews--mcp-blue?logo=docker)](https://github.com/azizmazrou/ews-mcp/pkgs/container/ews-mcp)
[![Multi-arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-green)](https://github.com/azizmazrou/ews-mcp/pkgs/container/ews-mcp)

> A Model Context Protocol server that gives an LLM agent **real, typed
> control of a Microsoft Exchange / Office 365 mailbox** — email,
> calendar, contacts, tasks, folders, attachments, threads, search,
> follow-up flags, and a set of agent-side primitives for autonomous
> workflows.

```bash
docker pull ghcr.io/azizmazrou/ews-mcp:latest
docker run -d --name ews-mcp --env-file .env --network host \
  ghcr.io/azizmazrou/ews-mcp:latest
```

Designed to plug into **Claude Desktop**, **Open WebUI**, **Claude
Code**, or any other MCP-aware client. Speaks EWS natively — no Graph
proxy, no Microsoft 365 connector.

---

## At a glance

| | |
|---|---|
| **67 tools** | Email · Drafts · Attachments · Calendar · Contacts · Tasks · Folders · Search · OOF · Memory · Commitments · Approvals · Voice · Rules · Briefings |
| **3 transports** | stdio (default) · SSE/HTTP · OpenAPI |
| **3 auth methods** | OAuth2 (Office 365) · Basic · NTLM (corporate Exchange) |
| **Multi-mailbox** | Every base tool accepts `target_mailbox=<smtp>` for impersonation / delegate access |
| **AI provider** | Optional. OpenAI · Anthropic · any OpenAI-compatible local endpoint (Ollama / LM Studio / llama.cpp) |
| **Storage** | Single per-mailbox SQLite file. No vector database needed. |
| **Image** | Multi-arch (`linux/amd64` + `linux/arm64`) on GHCR |

---

## Why this MCP

### ~12× cheaper LLM I/O against Outlook

Outlook bodies are MSO HTML — 5–10× more bytes than the information
they carry. Reading and writing through this server, the LLM can opt
into Markdown on either side:

```python
# Read
get_email_details(message_id="...", format="markdown")
# 25 KB of Outlook HTML  →  3.8 KB of clean GFM markdown (~12× fewer tokens)

# Write
send_email(
    to=["alice@company.com"],
    subject="Q1 review",
    body_format="markdown",
    body="# Hi Alice\n\nApproved on Q1...",
)
# Server converts to HTML before EWS — Outlook signature with inline
# cid: image refs preserved end-to-end.
```

### Document text extraction for everything an email actually carries

`read_attachment` returns ready-to-reason text from binary attachments.
The agent never has to download and parse an Excel file itself.

| Format | What you get |
|---|---|
| **PDF** | text + tables, page-bounded |
| **DOCX** | text + tables |
| **XLSX / XLS** | sheet-by-sheet rows |
| **PPTX** | slide-by-slide text + speaker notes + tables |
| **MSG** | Outlook compound file: envelope + body + nested attachment listing |
| **EML** | RFC-822 (non-Outlook exports) |
| **HTML** | RTL-safe Arabic-aware markdown conversion |
| **CSV / LOG / JSON / XML / MD / TXT** | BOM-aware UTF-8 / UTF-16 decode |

### Reply-path that doesn't break Outlook signatures

`reply_email` and `forward_email` build a fresh
Outlook-compatible HTML body manually instead of calling EWS
`create_reply()` / `create_forward()`. The result:

- Conversation thread preserved with the standard Outlook border-top
  separator and `From: / Sent: / To: / Cc: / Subject:` header block
- Inline images from the original message (signature graphics, embedded
  logos, screenshots inside the thread) copied to the new message
- Threading headers (`In-Reply-To` / `References`) set so Outlook
  groups the reply into the same conversation

Same path whether the body comes in as HTML, markdown, or text.

### A clean MCP / skill boundary

The server does **deterministic data work** — fetch, transform, embed,
extract, persist. The consuming agent does the **reasoning** —
classify, summarise, decide, compose. So the AI surface here is
deliberately small:

- `semantic_search_emails` (vector cosine over a SQLite-cached embedding store)
- `read_attachment` text extraction (binary parsing belongs server-side)
- `body_format` Markdown ⇄ HTML conversion (deterministic transform)

LLM-reasoning tools that just round-tripped to a model
(`classify_email`, `summarize_email`, `suggest_replies`,
`extract_commitments`, `build_voice_profile`) are intentionally **not**
exposed. The agent already has an LLM running and does those tasks
in-prompt with the data this MCP returns. Cheaper, faster, smarter.

### Per-mailbox SQLite cache — no vector DB

A single SQLite file at `data/ews_mcp_<mailbox>.sqlite` holds three
caches: converted markdown bodies, extracted attachment text, and
embedding vectors. Backups are a single-file copy.

---

## Quick start

### 1 — Pull the image

```bash
docker pull ghcr.io/azizmazrou/ews-mcp:latest
```

| Tag | When to use |
|---|---|
| `latest` | Always-current build of `main` |
| `4.0.0` (`v*.*.*`) | A specific semver release |
| `4.0` | Latest patch on a minor line |
| `sha-<7chars>` | Exact commit |

Public image, no `docker login` required.

### 2 — Configure

```bash
cat > .env <<'EOF'
EWS_SERVER_URL=outlook.office365.com
EWS_EMAIL=user@company.com
TIMEZONE=UTC

# Auth — pick ONE block

# OAuth2 (Office 365)
EWS_AUTH_TYPE=oauth2
EWS_CLIENT_ID=00000000-0000-0000-0000-000000000000
EWS_CLIENT_SECRET=...
EWS_TENANT_ID=00000000-0000-0000-0000-000000000000

# Basic
# EWS_AUTH_TYPE=basic
# EWS_USERNAME=user@company.com
# EWS_PASSWORD=...

# NTLM (corporate Exchange behind ADFS)
# EWS_AUTH_TYPE=ntlm
# EWS_USERNAME=DOMAIN\\user
# EWS_PASSWORD=...

# Optional — only needed for semantic_search_emails
ENABLE_AI=true
AI_PROVIDER=local            # or openai, anthropic
AI_BASE_URL=http://ollama:11434/v1
AI_EMBEDDING_MODEL=nomic-embed-text

# Optional — SSE transport for remote MCP clients
# MCP_TRANSPORT=sse
# MCP_HOST=0.0.0.0
# MCP_API_KEY=$(openssl rand -hex 32)
EOF
```

### 3 — Run

```bash
docker run -d \
  --name ews-mcp \
  --restart unless-stopped \
  --env-file .env \
  --network host \
  -v ./data:/app/data \
  -v ./logs:/app/logs \
  ghcr.io/azizmazrou/ews-mcp:latest
```

The two volume mounts are recommended so the SQLite cache and logs
survive container recreation.

### Claude Desktop — three setups, pick one

#### A. Custom Connector UI (no JSON — easiest, v4.0.1+)

If you've started the server with SSE transport and an `MCP_API_KEY` set,
open Claude Desktop → **Connectors** → **Custom Connector** and fill in:

| Field | Value |
|---|---|
| **Name** | `EWS` (anything) |
| **HTTP URL** | `http://<host>:8000/sse?api_key=YOUR_MCP_API_KEY` |
| **OAuth Client ID** | _(leave blank)_ |
| **OAuth Client Secret** | _(leave blank)_ |

The `?api_key=` query-param is a stop-gap until v4.1.0 lands proper OAuth
2.0. The token is hmac-compared on the server, never logged. Replace
`<host>` with `localhost` (local install), your LAN IP (e.g.
`192.168.1.10`), or a public hostname behind a TLS proxy. Use HTTPS for
anything beyond the local network.

#### B. Local stdio container (no SSE port — simplest auth model)

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ews": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "--env-file", "/path/to/.env",
        "ghcr.io/azizmazrou/ews-mcp:latest"
      ]
    }
  }
}
```

Claude Desktop launches the container per session, talks to it over
stdin/stdout. No network port exposed; no auth headache. Best for a
single user on the same machine.

#### C. Remote SSE via `mcp-remote` (header-based auth, JSON config)

For users on older Claude Desktop versions or who need custom headers:

```json
{
  "mcpServers": {
    "ews": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://your-host/sse",
        "--header", "Authorization: Bearer YOUR_MCP_API_KEY"
      ]
    }
  }
}
```

Config-file location: `%APPDATA%\Claude\claude_desktop_config.json`
(Windows), `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS), `~/.config/Claude/claude_desktop_config.json` (Linux).

### docker-compose

A reference `docker-compose.yml` ships in the repo. After cloning
(see "From source" below) you can:

```bash
cp .env.example .env  # edit credentials
docker compose up -d
```

### From source (development / forks)

If you want to modify the code, fork-and-PR, or run a custom build:

```bash
git clone https://github.com/azizmazrou/ews-mcp.git
cd ews-mcp
pip install -r requirements.txt
cp .env.example .env
python -m src.main
```

Or container-build:

```bash
docker build -t ews-mcp:dev .
docker run -d --name ews-mcp --env-file .env --network host ews-mcp:dev
```

### Releasing your own fork

The repo's CI (`.github/workflows/docker-publish.yml`) publishes a
multi-arch image to GHCR on every push to `main` and on every
`v*.*.*` tag. To cut a release on a fork:

```bash
# 1. Make sure your fork has GHCR write permissions enabled in
#    Settings → Actions → General → Workflow permissions:
#    "Read and write permissions"
# 2. Tag and push
git tag v4.0.1
git push origin v4.0.1
```

The workflow runs, builds for `amd64` + `arm64`, and publishes
`ghcr.io/<your-fork>/ews-mcp:4.0.1`, `:4.0`, `:4`, `:sha-<…>`, and
updates `:latest` if the tag is on the default branch.

---

## Tool surface

| Category | # | Tools |
|---|---|---|
| **Email** | 14 | `send_email` · `read_emails` · `search_emails` (quick / advanced / full_text) · `get_email_details` · `get_emails_bulk` · `delete_email` · `move_email` · `copy_email` · `update_email` · `reply_email` · `forward_email` · `create_draft` · `create_reply_draft` · `create_forward_draft` |
| **Attachments** | 7 | `list_attachments` · `download_attachment` · `add_attachment` · `delete_attachment` · `read_attachment` · `get_email_mime` · `attach_email_to_draft` |
| **Calendar** | 7 | `create_appointment` · `get_calendar` · `update_appointment` · `delete_appointment` · `respond_to_meeting` · `check_availability` · `find_meeting_times` |
| **Contacts** | 5 | `create_contact` · `update_contact` · `delete_contact` · `find_person` (GAL + contacts + email history) · `analyze_contacts` |
| **Tasks** | 5 | `create_task` · `get_tasks` · `update_task` · `complete_task` · `delete_task` |
| **Folders** | 3 | `list_folders` · `find_folder` · `manage_folder` |
| **Search** | 1 | `search_by_conversation` |
| **OOF** | 4 | `oof_settings` · `configure_oof_policy` · `get_oof_policy` · `apply_oof_policy` |
| **AI** | 1 | `semantic_search_emails` |
| **Memory KV** | 4 | `memory_set` · `memory_get` · `memory_list` · `memory_delete` |
| **Commitments** | 3 | `track_commitment` · `list_commitments` · `resolve_commitment` |
| **Approvals** | 5 | `submit_for_approval` · `list_pending_approvals` · `approve` · `reject` · `execute_approved_action` |
| **Voice** | 1 | `get_voice_profile` |
| **Rules** | 5 | `rule_create` · `rule_list` · `rule_delete` · `rule_simulate` · `evaluate_rules_on_message` |
| **Compound** | 2 | `generate_briefing` · `prepare_meeting` |

Per-tool reference with input / output schemas: [`docs/API.md`](docs/API.md).

---

## Examples

### Read an email cheaply

```python
get_email_details(
    message_id="AAMk...",
    format="markdown",
    trim_quoted=True,
)
```

### Compose a reply in markdown — signature preserved

```python
reply_email(
    message_id="AAMk...",
    body_format="markdown",
    body="""
    Approved on the Q1 budget. Two changes:

    - Move line 4 (cloud spend) up by 8%
    - Defer the contractor budget to Q2

    See attached for revised numbers.
    """.strip(),
    attachments=["/path/to/q1-budget-rev2.xlsx"],
)
```

### Read a forwarded email thread that came as a `.msg` attachment

```python
read_attachment(
    message_id="AAMk...",
    attachment_name="Re_ Q1 Performance Review.msg",
)
```

### Find a person across every signal

```python
find_person(query="Ahmed", source="all", include_stats=True)
```

### Free-busy across multiple attendees

```python
find_meeting_times(
    attendees=["alice@company.com", "bob@company.com"],
    duration_minutes=60,
    date_range_start="2026-04-20",
    date_range_end="2026-04-22",
)
```

### Filter on Outlook follow-up flag

```python
search_emails(is_flagged=True, max_results=20)
```

### Operate on a shared mailbox

```python
read_emails(folder="inbox", target_mailbox="support@company.com")
```

(Requires `EWS_IMPERSONATION_ENABLED=true` and an account with
delegate or impersonation rights on the target.)

---

## Configuration reference

Pydantic `Settings` parsed from env or `.env`. Sample files:
`.env.example`, `.env.basic.example`, `.env.oauth2.example`,
`.env.ai.example`.

#### Required

| Variable | Description |
|---|---|
| `EWS_EMAIL` | Primary mailbox SMTP address |
| `EWS_AUTH_TYPE` | `oauth2`, `basic`, or `ntlm` |

#### Connection

| Variable | Default | Description |
|---|---|---|
| `EWS_SERVER_URL` | autodiscover | Explicit EWS endpoint |
| `EWS_AUTODISCOVER` | `true` | |
| `EWS_INSECURE_SKIP_VERIFY` | `false` | TLS off — internal CA setups only |
| `EWS_DOWNLOAD_DIR` | `downloads` | Jail dir for `download_attachment` writes |

#### Multi-mailbox

| Variable | Default | Description |
|---|---|---|
| `EWS_IMPERSONATION_ENABLED` | `false` | Enable `target_mailbox=` on every tool |

#### AI (optional — only for `semantic_search_emails`)

| Variable | Description |
|---|---|
| `ENABLE_AI` | Master switch |
| `ENABLE_SEMANTIC_SEARCH` | Enable the tool |
| `AI_PROVIDER` | `openai`, `anthropic`, or `local` |
| `AI_BASE_URL` | Endpoint (required for `local`) |
| `AI_EMBEDDING_MODEL` | e.g. `nomic-embed-text`, `text-embedding-3-small` |
| `AI_API_KEY` | Required for non-`local` providers |

`AI_MODEL` (chat) is **not** required in v4 — the AI tool that ships
on this server uses embeddings only. The chat-reasoning tools that
lived here in v3.x have moved to the consuming agent's own LLM.

#### Transport

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `sse` |
| `MCP_HOST` | `127.0.0.1` | SSE bind — non-loopback requires `MCP_API_KEY` |
| `MCP_PORT` | `8000` | SSE port |
| `MCP_API_KEY` | — | Bearer token for SSE clients |

---

## Documentation

- [API.md](docs/API.md) — every tool with input / output schemas
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — component design + data flow + extension points
- [DEPLOYMENT.md](docs/DEPLOYMENT.md) — production deployment patterns
- [SETUP.md](docs/SETUP.md) — first-time configuration walk-through
- [CONNECTION_GUIDE.md](docs/CONNECTION_GUIDE.md) — Claude Desktop / SSE / OpenAPI clients
- [REPLY_FORWARD.md](docs/REPLY_FORWARD.md) — signature preservation deep-dive
- [IMPERSONATION.md](docs/IMPERSONATION.md) — multi-mailbox / delegate setup
- [AGENT_SECRETARY.md](docs/AGENT_SECRETARY.md) — memory / commitments / approvals / rules / voice / OOF policy / briefing
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — diagnostic playbook
- [COMMON_PITFALLS.md](docs/COMMON_PITFALLS.md) — recurring foot-guns when extending
- [CHANGELOG.md](CHANGELOG.md) — version history
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute / fork / release

---

## License

MIT — see [LICENSE](LICENSE).
