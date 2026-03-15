# EWS MCP Server v3.4

<div align="center">

**The Most Powerful AI-Exchange Integration**

*Transform how AI assistants interact with Microsoft Exchange*

[![Version](https://img.shields.io/badge/version-3.4.0-blue.svg)](https://github.com/azizmazrou/ews-mcp)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue.svg)](https://ghcr.io/azizmazrou/ews-mcp)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

[Quick Start](#-quick-start) | [Features](#-feature-highlights) | [Documentation](#-documentation) | [Examples](#-usage-examples)

</div>

---

## What's New in v3.4

### Reliability & Code Quality

- **Circuit Breaker**: Trips after 3 consecutive EWS failures, rejects requests instantly for 60s instead of waiting for timeout (saves ~30s/request when Exchange is down)
- **Proper async/await**: All blocking `resolve_names()` calls wrapped in `asyncio.to_thread()`, inbox+sent scans run concurrently via `asyncio.gather()`
- **Simplified errors**: Error messages capped at 200 chars, Pydantic validation errors shortened to human-readable format
- **Removed dead code**: `handle_ews_errors` decorator (-70 lines), deduplicated JSON serialization

---

## What's New in v3.3

### Tool Consolidation — 46 → 36 Tools

v3.3 merges 10 tools into 5 unified tools, reducing token cost by **~55%** per `list_tools` call:

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

> All **36 tools** support `target_mailbox` parameter!

---

## Feature Highlights

<table>
<tr>
<td width="50%">

### Email Operations (11 tools)
- Send, read, search, delete emails
- **Reply** with thread preservation
- **Forward** with inline images intact
- Move, copy, update email properties
- **Unified search**: quick, advanced, full-text modes

</td>
<td width="50%">

### Calendar Management (7 tools)
- Create/update appointments
- Meeting invitations & responses
- Free/busy availability check
- **AI-powered meeting time finder**
- Timezone-aware scheduling

</td>
</tr>
<tr>
<td>

### Contact Intelligence (2 tools)
- **find_person**: Multi-source search (GAL + Contacts + Email)
- **analyze_contacts**: Network analysis, communication history, VIP detection

</td>
<td>

### Enterprise Features
- **Impersonation** - Access any mailbox
- OAuth2 / Basic / NTLM auth
- Intelligent caching (70% less API calls)
- Enterprise logging & audit trails

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

## All 36 Tools

### Email Tools (11)

| Tool | Description |
|------|-------------|
| `send_email` | Send with attachments, CC/BCC, importance levels |
| `read_emails` | Read from any folder with pagination |
| `search_emails` | **Unified search** — `mode: "quick"` (default), `"advanced"`, `"full_text"` |
| `get_email_details` | Full email content including HTML body |
| `delete_email` | Soft delete (trash) or permanent removal |
| `move_email` | Move between folders |
| `copy_email` | Duplicate to another folder |
| `update_email` | Mark read/unread, flag, categorize |
| `reply_email` | Reply with thread & signature preservation |
| `forward_email` | Forward with full body & inline images |
| `list_attachments` | List all email attachments |

### Attachment Tools (5)

| Tool | Description |
|------|-------------|
| `list_attachments` | List attachments with metadata |
| `download_attachment` | Download as base64 or save to file |
| `add_attachment` | Attach files to draft emails |
| `delete_attachment` | Remove attachments |
| `read_attachment` | Extract text from PDF, DOCX, XLSX, PPTX, CSV, TXT, HTML, ZIP |

### Calendar Tools (7)

| Tool | Description |
|------|-------------|
| `create_appointment` | Schedule meetings with attendees |
| `get_calendar` | Retrieve events for date range |
| `update_appointment` | Modify existing appointments |
| `delete_appointment` | Cancel meetings |
| `respond_to_meeting` | Accept/decline/tentative responses |
| `check_availability` | Get free/busy information |
| `find_meeting_times` | AI-powered optimal time suggestions |

### Contact Tools (3)

| Tool | Description |
|------|-------------|
| `create_contact` | Add new contacts with full details |
| `update_contact` | Modify contact information |
| `delete_contact` | Remove contacts |

### Contact Intelligence Tools (2)

| Tool | Description |
|------|-------------|
| `find_person` | **Unified lookup** — `source: "all"` (default), `"gal"`, `"contacts"`, `"email_history"`, `"domain"` |
| `analyze_contacts` | **Unified analysis** — `analysis_type: "communication_history"`, `"overview"`, `"top_contacts"`, `"by_domain"`, `"dormant"`, `"vip"` |

### Task Tools (5)

| Tool | Description |
|------|-------------|
| `create_task` | Create tasks with due dates |
| `get_tasks` | List tasks by status |
| `update_task` | Modify task details |
| `complete_task` | Mark as complete |
| `delete_task` | Remove tasks |

### Search Tools (1)

| Tool | Description |
|------|-------------|
| `search_by_conversation` | Find all emails in a thread |

### Folder Tools (2)

| Tool | Description |
|------|-------------|
| `list_folders` | Get folder hierarchy with counts |
| `manage_folder` | **Unified management** — `action: "create"`, `"delete"`, `"rename"`, `"move"` |

### Out-of-Office Tools (1)

| Tool | Description |
|------|-------------|
| `oof_settings` | **Unified OOF** — `action: "get"` or `"set"` |

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
# AI-powered meeting time finder
find_meeting_times(
    attendees=["alice@company.com", "bob@company.com"],
    duration_minutes=60,
    preferences={
        "prefer_morning": True,
        "working_hours_start": 9,
        "working_hours_end": 17,
        "avoid_lunch": True
    }
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

| Variable | Required | Description |
|----------|----------|-------------|
| `EWS_EMAIL` | Yes | Your email address |
| `EWS_AUTH_TYPE` | Yes | `oauth2`, `basic`, or `ntlm` |
| `EWS_SERVER_URL` | No | Exchange server (auto-constructs EWS URL) |

**OAuth2 (Office 365):**
| Variable | Description |
|----------|-------------|
| `EWS_CLIENT_ID` | Azure AD app client ID |
| `EWS_CLIENT_SECRET` | Azure AD app secret |
| `EWS_TENANT_ID` | Azure AD tenant ID |

**Basic/NTLM:**
| Variable | Description |
|----------|-------------|
| `EWS_USERNAME` | Username |
| `EWS_PASSWORD` | Password |

**Impersonation (Optional):**
| Variable | Default | Description |
|----------|---------|-------------|
| `EWS_IMPERSONATION_ENABLED` | `false` | Enable multi-mailbox access |
| `EWS_IMPERSONATION_TYPE` | `impersonation` | `impersonation` or `delegate` |

**Advanced:**
| Variable | Default | Description |
|----------|---------|-------------|
| `TIMEZONE` | `UTC` | IANA timezone (e.g., `America/New_York`) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `sse` (HTTP/REST) |
| `MCP_PORT` | `8000` | Port for SSE transport |

---

## Architecture

```
EWS MCP Server v3.3
├── MCP Protocol Layer (stdio/SSE)
├── Tool Registry (36 tools)
│   ├── Email Tools (11) ─────────── send, read, search (3 modes), reply, forward...
│   ├── Calendar Tools (7) ──────── appointments, meetings, availability
│   ├── Contact Tools (3) ───────── create, update, delete
│   ├── Intelligence Tools (2) ──── find_person (5 sources), analyze_contacts (6 types)
│   ├── Task Tools (5) ──────────── task management
│   ├── Search Tools (1) ────────── conversation threads
│   ├── Folder Tools (2) ────────── list + manage_folder (4 actions)
│   ├── Attachment Tools (5) ────── read, download, content extraction
│   └── OOF Tools (1) ───────────── oof_settings (get/set)
├── Service Layer
│   ├── PersonService ───────────── person discovery & ranking
│   ├── EmailService ────────────── email operations
│   ├── ThreadService ───────────── conversation threading
│   └── AttachmentService ───────── format support (PDF, DOCX, XLSX...)
├── Adapter Layer
│   ├── GALAdapter ──────────────── multi-strategy directory search
│   └── CacheAdapter ────────────── intelligent TTL caching
├── EWS Client (exchangelib)
│   └── Impersonation Support ───── multi-mailbox access
├── Authentication (OAuth2/Basic/NTLM)
└── Exchange Web Services API
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

## Version History

### v3.4 - Reliability & Code Quality
- **Circuit breaker**: Auto-trips after 3 EWS failures, instant rejection for 60s
- **Async/await**: `asyncio.to_thread()` for blocking EWS calls, `asyncio.gather()` for concurrent scans
- **Simplified errors**: 200-char max, human-readable validation messages
- **Code cleanup**: -70 lines dead code, deduplicated JSON encoder

### v3.3 - Tool Consolidation
- **10 tools merged into 5**: 46 → 36 tools, ~55% token savings on `list_tools`
- **Unified search**: `search_emails` with quick/advanced/full_text modes
- **Unified contacts**: `find_person` with 5 source types, `analyze_contacts` with 6 analysis types
- **Unified folders**: `manage_folder` with create/delete/rename/move actions
- **Unified OOF**: `oof_settings` with get/set actions
- **Server-side filtering**: `analyze_contacts` communication history uses EWS sender filter

### v3.2 - Reply, Forward & Impersonation
- **Reply & Forward**: Full body preservation, inline images, Outlook-style headers
- **Impersonation**: All tools support `target_mailbox` parameter
- **Documentation**: Comprehensive guides for new features

### v3.0 - Person-Centric Architecture
- Multi-strategy GAL search (eliminates 0-results bug)
- Person-first data model with relationship scoring
- Intelligent caching (70% reduction in API calls)
- Enterprise logging system

### v2.x - Enterprise Features
- 40+ tools for email, calendar, contacts, tasks
- Attachment content extraction (PDF, DOCX, XLSX, PPTX)
- AI-powered meeting time finder
- Folder management

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
