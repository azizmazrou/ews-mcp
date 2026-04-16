# EWS MCP Server

<div align="center">

**Microsoft Exchange integration for the Model Context Protocol**

*Give AI assistants first-class access to mail, calendar, contacts, tasks, and directory services*

[![Version](https://img.shields.io/badge/version-3.4.x-blue.svg)](https://github.com/azizmazrou/ews-mcp)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue.svg)](https://ghcr.io/azizmazrou/ews-mcp)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

[Quick Start](#quick-start) | [Tools](#all-tools) | [Configuration](#configuration) | [Documentation](#documentation)

</div>

---

## Overview

An MCP server that wraps [exchangelib](https://github.com/ecederstrand/exchangelib) to expose Microsoft Exchange Web Services (EWS) to MCP clients such as Claude Desktop, Open WebUI, or any custom client.

- **46 tools** across email, drafts, attachments, calendar, contacts, directory (GAL), tasks, search, folders, out-of-office, and optional AI helpers
- **3 auth modes**: OAuth2 (client credentials), Basic, NTLM
- **Impersonation / delegation**: every non-AI tool accepts a `target_mailbox` to act on shared, delegated, or other users' mailboxes
- **Two transports**: `stdio` (default, for Claude Desktop) and `sse` (HTTP server with OpenAPI schema, for Open WebUI and REST clients)
- **Enterprise middleware**: rate limiter, circuit breaker, structured logging, audit log

---

## What's New (since v3.4.0)

### Drafts workflow
- `create_draft`, `create_reply_draft`, `create_forward_draft` — build reviewable HTML drafts in the Drafts folder before sending
- HTML reply/forward prototypes preserve the original conversation, inline images, CDATA blocks, and Outlook-style quoted headers

### Folder discovery & IDs
- New `find_folder` tool — locate a folder by name or ID across the full hierarchy
- `move_email` and `manage_folder` now accept `destination_folder_id` / `parent_folder_id` to resolve folders by stable Exchange ID rather than by display name

### Availability & scheduling
- `check_availability` now correctly parses exchangelib free/busy responses and includes the current mailbox by default
- Scheduling responses clarified so the AI can act on the result without re-prompting

### Platform
- Windows wrapper entrypoint fixes the Claude Desktop MSIX working-directory issue
- Signature placement, separator format, and duplicate `RE:` / `FW:` prefixes fixed in reply/forward

## What's New in v3.4

### Reliability & Code Quality

- **Circuit Breaker**: Trips after 3 consecutive EWS failures, rejects requests instantly for 60s instead of waiting for timeout (saves ~30s/request when Exchange is down)
- **Proper async/await**: All blocking `resolve_names()` calls wrapped in `asyncio.to_thread()`, inbox+sent scans run concurrently via `asyncio.gather()`
- **Simplified errors**: Error messages capped at 200 chars, Pydantic validation errors shortened to human-readable format
- **Removed dead code**: `handle_ews_errors` decorator (-70 lines), deduplicated JSON serialization

---

## What's New in v3.3

### Tool Consolidation

v3.3 merged 10 redundant tools into 5 unified tools (search, find_person, manage_folder, oof_settings, analyze_contacts). The base tool count later grew again as new features (drafts, find_folder, email MIME, attach-email-to-draft) were added.

| Merge | Before | After |
|-------|--------|-------|
| Search | `search_emails` + `advanced_search` + `full_text_search` | `search_emails` with `mode` param |
| Contact Lookup | `search_contacts` + `get_contacts` + `resolve_names` | `find_person` with `source` param |
| Folder Mgmt | `create_folder` + `delete_folder` + `rename_folder` + `move_folder` | `manage_folder` with `action` param |
| OOF | `set_oof_settings` + `get_oof_settings` | `oof_settings` with `action` param |
| Network Analysis | `get_communication_history` + `analyze_network` | `analyze_contacts` with `analysis_type` param |

```python
# Unified search with mode
search_emails(mode="advanced", keywords="report", folders=["inbox", "sent"])
search_emails(mode="full_text", search_query="quarterly budget")

# Unified contact lookup with source
find_person(query="Ahmed", source="all")
find_person(source="contacts")  # list all contacts

# Unified folder management
manage_folder(action="create", folder_name="Archive")
manage_folder(action="rename", folder_id="AAMk...", new_name="Old Projects")
```

> Every **base tool (42)** accepts `target_mailbox` for impersonation/delegation. The 4 optional AI tools currently act only on the primary mailbox — see [Known limitations](#known-limitations).

---

## Feature Highlights

<table>
<tr>
<td width="50%">

### Email (10 tools) + Drafts (3 tools)
- Send, read, search (quick / advanced / full-text), delete, move, copy, update
- Reply / Forward with thread + inline-image preservation
- Draft variants (`create_draft`, `create_reply_draft`, `create_forward_draft`) for AI-review-before-send flows

</td>
<td width="50%">

### Calendar (7 tools)
- Create, update, delete appointments
- Accept / decline / tentative responses
- Free/busy availability + multi-attendee time-finder
- Timezone-aware scheduling

</td>
</tr>
<tr>
<td>

### Contacts + Intelligence (5 tools)
- `create_contact` / `update_contact` / `delete_contact`
- `find_person` — unified GAL + contacts + email-history search
- `analyze_contacts` — communication history, top contacts, domains, dormant, VIPs

</td>
<td>

### Attachments (7 tools)
- `list_attachments`, `download_attachment`, `add_attachment`, `delete_attachment`
- `read_attachment` — text extraction for PDF, DOCX, XLSX
- `get_email_mime` — raw RFC-822 MIME
- `attach_email_to_draft` — attach an existing message as `.eml`

</td>
</tr>
<tr>
<td>

### Folders (3) + Tasks (5) + Search (1) + OOF (1)
- `list_folders`, `find_folder`, `manage_folder` (create/delete/rename/move)
- Full task CRUD + `complete_task`
- `search_by_conversation`, `oof_settings` (get/set)

</td>
<td>

### Enterprise Features
- Impersonation / delegation on every base tool
- OAuth2 / Basic / NTLM
- stdio + SSE/HTTP transports with OpenAPI 3.0 schema
- Rate limiter, circuit breaker, structured logs, audit log
- Optional AI layer (OpenAI / Anthropic / OpenAI-compatible)

</td>
</tr>
</table>

---

## Quick Start

### Option 1: Docker (Recommended)

```bash
# Pull latest image
docker pull ghcr.io/azizmazrou/ews-mcp:latest

# Create configuration
cat > .env << 'EOF'
EWS_SERVER_URL=mail.company.com
EWS_EMAIL=user@company.com
EWS_AUTH_TYPE=basic
EWS_USERNAME=user@company.com
EWS_PASSWORD=your-password
TIMEZONE=UTC
EOF

# Run
docker run -d --name ews-mcp --env-file .env ghcr.io/azizmazrou/ews-mcp:latest
```

### Option 2: OAuth2 (Office 365)

```bash
cat > .env << 'EOF'
EWS_SERVER_URL=outlook.office365.com
EWS_EMAIL=user@company.com
EWS_AUTH_TYPE=oauth2
EWS_CLIENT_ID=your-azure-client-id
EWS_CLIENT_SECRET=your-azure-secret
EWS_TENANT_ID=your-azure-tenant
TIMEZONE=UTC
EOF
```

### Option 3: Local Development

```bash
git clone https://github.com/azizmazrou/ews-mcp.git
cd ews-mcp
pip install -r requirements.txt
cp .env.example .env  # Configure your credentials
python -m src.main
```

---

## All Tools

**Grand total: 46** — 42 base tools (always on, subject to category flags) + 4 optional AI tools.

### Email (10)

| Tool | Description |
|------|-------------|
| `send_email` | Send with attachments, CC/BCC, importance, inline base64 attachments |
| `read_emails` | Read from any folder with pagination and `unread_only` filter |
| `search_emails` | Unified search — `mode: "quick"` / `"advanced"` / `"full_text"` |
| `get_email_details` | Full email content including HTML body |
| `delete_email` | Soft delete (trash) or `hard_delete: true` for permanent removal |
| `move_email` | Move to another folder by name or `destination_folder_id` |
| `copy_email` | Copy to another folder by name or `destination_folder_id` |
| `update_email` | Mark read/unread, flag status, categories, importance |
| `reply_email` | Reply with thread + signature + inline-image preservation |
| `forward_email` | Forward with full body and inline images |

### Email Drafts (3)

| Tool | Description |
|------|-------------|
| `create_draft` | Save a draft in `Drafts` for later review/send |
| `create_reply_draft` | Build a reply draft (quoted original + signature placeholder) without sending |
| `create_forward_draft` | Build a forward draft without sending |

### Attachments (7)

| Tool | Description |
|------|-------------|
| `list_attachments` | List attachments with metadata (name, size, MIME, inline flag) |
| `download_attachment` | Download as base64 or save to file (see security note in docs) |
| `add_attachment` | Attach files to a draft email |
| `delete_attachment` | Remove attachments from a message |
| `read_attachment` | Extract text from PDF / DOCX / XLSX |
| `get_email_mime` | Return full RFC-822 MIME content of a message |
| `attach_email_to_draft` | Attach another message (as `.eml`) to a draft |

### Calendar (7)

| Tool | Description |
|------|-------------|
| `create_appointment` | Schedule meetings with attendees, body, location, reminders |
| `get_calendar` | Retrieve events for a date range |
| `update_appointment` | Modify time, attendees, location, or cancel |
| `delete_appointment` | Cancel meeting (with optional cancellation notification) |
| `respond_to_meeting` | Accept / Decline / Tentative responses with optional body |
| `check_availability` | Free/busy for attendees over a time window |
| `find_meeting_times` | Suggested slots across multiple attendees |

### Contacts (3)

| Tool | Description |
|------|-------------|
| `create_contact` | Add a contact with email, phones, company, title, department |
| `update_contact` | Modify contact fields |
| `delete_contact` | Remove a contact |

### Contact Intelligence (2)

| Tool | Description |
|------|-------------|
| `find_person` | Unified lookup — `source: "all" / "gal" / "contacts" / "email_history" / "domain"` |
| `analyze_contacts` | Unified analysis — `analysis_type: "communication_history" / "overview" / "top_contacts" / "by_domain" / "dormant" / "vip"` |

### Tasks (5)

| Tool | Description |
|------|-------------|
| `create_task` | Create task with due date, status, importance, reminder |
| `get_tasks` | List tasks filtered by status / include_completed |
| `update_task` | Modify task fields |
| `complete_task` | Mark task complete |
| `delete_task` | Remove a task |

### Search (1)

| Tool | Description |
|------|-------------|
| `search_by_conversation` | Find all messages sharing a conversation/thread ID |

### Folders (3)

| Tool | Description |
|------|-------------|
| `list_folders` | Folder hierarchy with optional counts, depth, hidden-folder toggle |
| `find_folder` | Locate a folder by name or ID anywhere in the mailbox |
| `manage_folder` | Unified management — `action: "create" / "delete" / "rename" / "move"` |

### Out-of-Office (1)

| Tool | Description |
|------|-------------|
| `oof_settings` | `action: "get"` / `"set"` — internal/external reply, scheduling |

### AI (4, optional)

Enabled per-feature via `enable_ai=true` plus individual flags. These tools currently act on the **primary mailbox only** (they do not honor `target_mailbox` — see [Known limitations](#known-limitations)).

| Tool | Description | Feature flag |
|------|-------------|--------------|
| `semantic_search_emails` | Natural-language email search via embeddings | `enable_semantic_search` |
| `classify_email` | Priority / sentiment / optional spam classification | `enable_email_classification` |
| `summarize_email` | AI summary of a message (configurable length) | `enable_email_summarization` |
| `suggest_replies` | Generate N draft reply variants | `enable_smart_replies` |

---

## Usage Examples

### Reply & Forward (v3.2 Feature)

```python
# Reply preserving full conversation thread
reply_email(
    message_id="AAMkADc3MWUy...",
    body="<p>Great idea! Let's discuss in tomorrow's meeting.</p>",
    reply_all=False
)

# Reply all with attachment
reply_email(
    message_id="AAMkADc3MWUy...",
    body="Please see attached analysis.",
    reply_all=True,
    attachments=["/path/to/analysis.pdf"]
)

# Forward with custom message
forward_email(
    message_id="AAMkADc3MWUy...",
    to=["director@company.com"],
    cc=["team@company.com"],
    body="<p><b>Priority:</b> Please review the proposal below.</p>"
)
```

**What gets preserved:**
- Full HTML body with formatting
- Inline images (signatures, logos, embedded graphics)
- Conversation threading metadata
- Original attachments
- Outlook-style headers

### Impersonation / Multi-Mailbox (v3.2 Feature)

```python
# Read from shared mailbox
emails = read_emails(
    folder="inbox",
    max_results=10,
    target_mailbox="info@company.com"
)

# Send on behalf of support
send_email(
    to=["customer@external.com"],
    subject="Re: Your Inquiry",
    body="Thank you for contacting support...",
    target_mailbox="support@company.com"
)

# Create calendar event in another user's calendar
create_appointment(
    subject="Team Sync",
    start="2025-12-15T10:00:00",
    end="2025-12-15T11:00:00",
    attendees=["team@company.com"],
    target_mailbox="manager@company.com"
)

# Search across multiple mailboxes
for mailbox in ["sales@company.com", "support@company.com"]:
    results = search_emails(
        subject_contains="urgent",
        target_mailbox=mailbox
    )
```

### Person Search (Multi-Strategy)

```python
# Never get 0 results - multi-strategy search
person = find_person(
    query="Ahmed",
    source="all",
    include_stats=True
)

# Returns comprehensive Person object:
# - name, email addresses, phone numbers
# - organization, department, job title
# - communication stats (emails sent/received)
# - relationship strength score
# - sources (GAL, Contacts, Email History)
```

### Smart Meeting Scheduling

```python
# Suggest slots that work for every attendee in a date range
find_meeting_times(
    attendees=["alice@company.com", "bob@company.com"],
    duration_minutes=60,
    start_date="2026-04-20",
    end_date="2026-04-22",
    time_slots_per_day=3,
    min_confidence_percent=80,
)
```

### Draft-before-send workflow

Drafts give the AI assistant a safe "preview and confirm" step. Nothing leaves the mailbox until the user explicitly sends.

```python
# Create a reply draft
draft = create_reply_draft(
    message_id="AAMkAGI...",
    body="<p>Thanks for the update — will review by Friday.</p>",
    to_all=False,
)
# draft["draft_id"] → open in client / add attachments / send later

# Attach files to the draft
add_attachment(
    message_id=draft["draft_id"],
    attachment_paths=["/path/to/report.pdf"],
)

# Or attach another message as .eml
attach_email_to_draft(
    draft_id=draft["draft_id"],
    message_id_to_attach="AAMkAGI-some-other-msg",
)
```

### Folder discovery

```python
# Find a folder anywhere in the hierarchy by name
folder = find_folder(folder_name="Archive/Q1 Reports")
# Use the returned stable folder_id instead of a fragile display path
move_email(
    message_id="AAMkAGI...",
    destination_folder_id=folder["folder_id"],
)
```

---

## Claude Desktop Integration

Add to your Claude Desktop config:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
**Linux**: `~/.config/Claude/claude_desktop_config.json`

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

---

## Configuration

### Environment Variables

All settings are parsed by `src/config.py` (Pydantic `Settings`). Examples live in `.env.example`, `.env.basic.example`, `.env.oauth2.example`, `.env.ai.example`.

#### Required

| Variable | Description |
|----------|-------------|
| `EWS_EMAIL` | Primary mailbox email address |
| `EWS_AUTH_TYPE` | `oauth2` (default), `basic`, or `ntlm` |

#### EWS connection

| Variable | Default | Description |
|----------|---------|-------------|
| `EWS_SERVER_URL` | — | Explicit server URL; if empty, autodiscover is used |
| `EWS_AUTODISCOVER` | `true` | Enable Exchange autodiscover |

#### Auth — OAuth2 (Office 365)

| Variable | Description |
|----------|-------------|
| `EWS_CLIENT_ID` | Azure AD app client ID |
| `EWS_CLIENT_SECRET` | Azure AD app client secret |
| `EWS_TENANT_ID` | Azure AD tenant ID |

#### Auth — Basic / NTLM

| Variable | Description |
|----------|-------------|
| `EWS_USERNAME` | Username (email or `DOMAIN\\user`) |
| `EWS_PASSWORD` | Password |

#### Impersonation / delegation

| Variable | Default | Description |
|----------|---------|-------------|
| `EWS_IMPERSONATION_ENABLED` | `false` | Enable `target_mailbox` on base tools |
| `EWS_IMPERSONATION_TYPE` | `impersonation` | `impersonation` (service account with `ApplicationImpersonation`) or `delegate` |

#### Transport

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `sse` (HTTP + Server-Sent Events) |
| `MCP_HOST` | `0.0.0.0` | Bind address for SSE (override to `127.0.0.1` for local-only) |
| `MCP_PORT` | `8000` | Port for SSE |
| `MCP_SERVER_NAME` | `ews-mcp-server` | Identifier advertised to MCP clients |

#### OpenAPI (SSE transport)

| Variable | Default | Description |
|----------|---------|-------------|
| `API_BASE_URL` | — | Public URL advertised in the OpenAPI `servers` array |
| `API_BASE_URL_INTERNAL` | — | Internal container URL (e.g. `http://ews-mcp:8000`) |
| `API_TITLE` | `Exchange Web Services (EWS) MCP API` | OpenAPI title |
| `API_VERSION` | `3.4.0` | OpenAPI version |

#### Category feature flags

All default to `true`. Set to `false` to skip registering a whole category:

| Variable | Toggles |
|----------|---------|
| `ENABLE_EMAIL` | Email, drafts, attachments |
| `ENABLE_CALENDAR` | Calendar |
| `ENABLE_CONTACTS` | Contacts + contact intelligence |
| `ENABLE_TASKS` | Tasks |
| `ENABLE_FOLDERS` | Folder tools (folders are always loaded, but can be disabled here) |
| `ENABLE_ATTACHMENTS` | Attachment tools |

#### AI (all optional, off by default)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_AI` | `false` | Master switch |
| `AI_PROVIDER` | `openai` | `openai`, `anthropic`, or `local` (OpenAI-compatible) |
| `AI_API_KEY` | — | Provider API key |
| `AI_MODEL` | auto | e.g. `gpt-4o-mini`, `claude-3-5-sonnet-20241022` |
| `AI_EMBEDDING_MODEL` | auto | e.g. `text-embedding-3-small` |
| `AI_BASE_URL` | — | Custom base URL (local / proxy) |
| `AI_MAX_TOKENS` | `4096` | Completion tokens |
| `AI_TEMPERATURE` | `0.7` | Sampling temperature |
| `ENABLE_SEMANTIC_SEARCH` | `false` | Enables `semantic_search_emails` |
| `ENABLE_EMAIL_CLASSIFICATION` | `false` | Enables `classify_email` |
| `ENABLE_EMAIL_SUMMARIZATION` | `false` | Enables `summarize_email` |
| `ENABLE_SMART_REPLIES` | `false` | Enables `suggest_replies` |

#### Performance / reliability

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_CACHE` | `true` | Response caching |
| `CACHE_TTL` | `300` | Cache TTL seconds |
| `CONNECTION_POOL_SIZE` | `10` | EWS connection pool size |
| `REQUEST_TIMEOUT` | `30` | HTTP timeout seconds |
| `RATE_LIMIT_ENABLED` | `true` | Enable rate limiter |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `25` | Sliding-window limit |
| `ENABLE_AUDIT_LOG` | `true` | Emit audit-log entries |
| `MAX_ATTACHMENT_SIZE` | `157286400` | 150 MB default |

#### Misc

| Variable | Default | Description |
|----------|---------|-------------|
| `TIMEZONE` | `UTC` | IANA timezone (e.g. `America/New_York`) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Architecture

```
EWS MCP Server
├── MCP Protocol Layer             stdio  •  SSE (HTTP + /openapi.json)
├── Middleware
│   ├── RateLimiter                25 req/min sliding window (configurable)
│   ├── CircuitBreaker             trips after 3 connectivity failures, 60s cool-down
│   ├── ErrorHandler               exception → structured response mapper
│   ├── Logging                    app log + error log + audit log (rotating)
│   └── OpenAPI Adapter            per-tool POST /api/tools/{name} routes
├── Tool Registry (42 base + 4 AI = 46)
│   ├── Email              (10)    send, read, search(3 modes), update, delete, move, copy, reply, forward
│   ├── Drafts             (3)     create_draft, create_reply_draft, create_forward_draft
│   ├── Attachments        (7)     list, download, add, delete, read(PDF/DOCX/XLSX), mime, attach_email_to_draft
│   ├── Calendar           (7)     create, get, update, delete, respond, availability, find_meeting_times
│   ├── Contacts           (3)     create, update, delete
│   ├── Contact Intelligence(2)    find_person, analyze_contacts
│   ├── Tasks              (5)     create, get, update, complete, delete
│   ├── Search             (1)     search_by_conversation
│   ├── Folders            (3)     list, find, manage(create/delete/rename/move)
│   ├── Out-of-Office      (1)     oof_settings(get/set)
│   └── AI (optional)      (4)     semantic_search, classify, summarize, suggest_replies
├── Services
│   ├── PersonService              multi-source contact discovery + relationship scoring
│   ├── EmailService               message lookups, folder resolution
│   ├── ThreadService              conversation reconstruction
│   └── AttachmentService          attachment I/O
├── Adapters
│   ├── GALAdapter                 multi-strategy directory search (exact → partial → domain)
│   └── CacheAdapter               in-memory TTL cache
├── AI (optional)
│   ├── Providers                  OpenAI  •  Anthropic  •  OpenAI-compatible local
│   ├── EmbeddingService           file-backed cache at data/embeddings/embeddings.json
│   └── ClassificationService      priority / sentiment / summary / reply suggestions
├── EWS Client (exchangelib)       impersonation & delegation via target_mailbox
└── Authentication                 OAuth2 (MSAL) • Basic • NTLM
```

---

## Documentation

### Getting Started
- [Setup Guide](docs/SETUP.md) - Step-by-step installation
- [Deployment Guide](docs/DEPLOYMENT.md) - Production deployment
- [Docker Guide](docs/GHCR.md) - Container usage

### Feature Guides
- [Reply & Forward Guide](docs/REPLY_FORWARD.md) - Email threading & signatures
- [Impersonation Guide](docs/IMPERSONATION.md) - Multi-mailbox access
- [API Reference](docs/API.md) - Complete tool documentation

### Integration
- [Open WebUI Setup](OPENWEBUI_SETUP.md) - REST API integration
- [Troubleshooting](docs/TROUBLESHOOTING.md) - Common issues

### Architecture
- [Architecture Overview](docs/ARCHITECTURE.md) - Technical deep dive
- [v3.0 Changes](docs/V3_IMPLEMENTATION_SUMMARY.md) - Version history

---

## Known limitations

Items to be aware of when deploying. See [CHANGELOG.md](CHANGELOG.md) for the full history.

- **AI tools do not honor `target_mailbox`.** `semantic_search_emails`, `classify_email`, `summarize_email`, and `suggest_replies` always operate on the primary authenticated mailbox. Use non-AI tools (`read_emails`, `search_emails`, `get_email_details`) with impersonation when you need multi-mailbox behaviour.
- **`read_attachment` extracts PDF / DOCX / XLSX only.** Other formats fall through to the default "text/plain only" path.
- **SSE transport is unauthenticated by default.** `MCP_HOST` defaults to `0.0.0.0`. For any deployment that is reachable beyond localhost, put the server behind an auth-enforcing reverse proxy or bind to `127.0.0.1`. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
- **Global TLS verification.** The EWS HTTP adapter currently does not verify server certificates. Intended for corporate environments with internal CAs; review before exposing to untrusted networks.
- **Audit log content.** The audit log currently records tool arguments. Review `logs/` retention and access control if arguments may contain sensitive payloads.

## Version History

See [CHANGELOG.md](CHANGELOG.md) for the full history. Recent highlights:

- **v3.4.x (Unreleased)** — HTML reply/forward drafts; `find_folder`; folder-ID support on move/parent resolution; Windows MSIX wrapper; availability parsing fix.
- **v3.4.0** — Circuit breaker; `asyncio.to_thread` / `asyncio.gather`; 200-char error truncation; removed `handle_ews_errors`; deduplicated JSON encoder.
- **v3.3.0** — Tool consolidation (unified search / find_person / manage_folder / oof_settings / analyze_contacts).
- **v3.2.0** — Reply, forward, inline base64 attachments; impersonation on every base tool.
- **v3.0.0** — Person-centric architecture; multi-strategy GAL search; enterprise logging.

---

## License

MIT License - See [LICENSE](LICENSE) for details.

## Contributing

Contributions welcome! Please read the contributing guidelines before submitting PRs.

## Support

- Issues: [GitHub Issues](https://github.com/azizmazrou/ews-mcp/issues)
- Discussions: [GitHub Discussions](https://github.com/azizmazrou/ews-mcp/discussions)

---

<div align="center">

**Built with exchangelib and the Model Context Protocol**

*Making AI assistants work seamlessly with Microsoft Exchange*

</div>
