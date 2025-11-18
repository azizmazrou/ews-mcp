# EWS MCP Server v3.0

A complete Model Context Protocol (MCP) server that interfaces with Microsoft Exchange Web Services (EWS), enabling AI assistants to interact with Exchange for email, calendar, contacts, and task operations.

> **New in v3.0**: Person-centric architecture with intelligent multi-strategy GAL search that **eliminates the 0-results bug**!

> **Docker Images**: Pre-built images are available at `ghcr.io/azizmazrou/ews-mcp:latest`.

## What's New in v3.0

Version 3.0 is a **major architectural upgrade** that transforms the system from email-centric to **person-centric**.

### GAL 0-Results Bug - FIXED!

The notorious GAL search 0-results bug has been **completely solved** with a multi-strategy search approach:

| Scenario | v2.x Result | v3.0 Result |
|----------|-------------|-------------|
| Exact name match | Works | Works |
| Partial name ("Ahmed") | 0 results | Finds all matches |
| Domain search ("@company.com") | 0 results | Finds all users |
| Typos/variations | 0 results | Fuzzy matches |

### Person-Centric Architecture

**Before (v2.x):** Email-centric, scattered logic
```python
# Old way - juggling emails and accounts
send_email(to=["user@example.com"], ...)
find_person(query="Ahmed")
get_history(email="user@example.com")
```

**After (v3.0):** Person-first, unified operations
```python
# New way - work with PEOPLE naturally
person = await find_person("Ahmed")
# Returns: Person with name, emails, phone, title, department, communication stats
```

### New Architecture Components

- **Person Model** - First-class entity with comprehensive profile (name, emails, phones, title, department, communication stats)
- **PersonService** - Orchestrates person discovery across GAL, Contacts, and Email History
- **GALAdapter** - Multi-strategy search (exact, partial, domain, fuzzy)
- **CacheAdapter** - Intelligent caching to reduce Exchange load
- **ThreadService** - Email thread preservation with HTML formatting
- **AttachmentService** - All formats supported (PDF, DOCX, XLSX, PPTX, ZIP, CSV, TXT, HTML)
- **EmailService** - Enhanced with thread support

### Enterprise Logging

- **Console**: Minimal INFO level for monitoring
- **File logs**: DEBUG level for troubleshooting (ews-mcp.log)
- **Error logs**: ERROR level only (ews-mcp-errors.log)
- **Audit logs**: Compliance trail (audit.log)

## Features

- **Person-Centric Operations**: Work with people naturally, not just email addresses
- **Multi-Strategy GAL Search**: Never see 0 results when people exist in the directory
- **Email Operations**: Send, read, search, delete, move, copy emails with attachment support
- **Attachment Content Extraction**: Read text from PDF, DOCX, XLSX, PPTX, TXT, CSV, HTML, ZIP files (Arabic/UTF-8 support)
- **Calendar Management**: Create, update, delete appointments, respond to meetings, AI-powered meeting time finder
- **Contact Management**: Full CRUD operations for Exchange contacts
- **Contact Intelligence**: Advanced contact search across GAL & email history, communication analytics, network analysis
- **Task Management**: Create and manage Exchange tasks
- **Folder Management**: Create, delete, rename, move mailbox folders
- **Advanced Search**: Conversation threading, full-text search across email content
- **Out-of-Office**: Configure automatic replies with scheduling
- **Multi-Authentication**: Support for OAuth2, Basic Auth, and NTLM
- **Timezone Support**: Proper handling of timezones (tested with Asia/Riyadh, UTC, etc.)
- **HTTP/SSE Transport**: Support for both stdio and HTTP/SSE for web clients (n8n compatible)
- **Docker Ready**: Production-ready containerization with best practices
- **Rate Limiting**: Built-in rate limiting with automatic retry (exponential backoff)
- **Error Handling**: Comprehensive error handling with @handle_ews_errors decorator
- **Intelligent Caching**: Reduce Exchange load with TTL-based caching
- **Enterprise Logging**: Multi-tier logging for monitoring and troubleshooting

## Quick Start

### Using Pre-built Docker Image (Easiest)

Choose your authentication method:

#### Option 1: Basic Authentication (Fastest Setup - 1 minute)

**Best for**: Testing, on-premises Exchange, quick demos

```bash
# Pull the latest image
docker pull ghcr.io/azizmazrou/ews-mcp:latest

# Create .env file with Basic Auth
cat > .env <<EOF
EWS_SERVER_URL=https://mail.company.com/EWS/Exchange.asmx
EWS_EMAIL=user@company.com
EWS_AUTODISCOVER=false
EWS_AUTH_TYPE=basic
EWS_USERNAME=user@company.com
EWS_PASSWORD=your-password
TIMEZONE=UTC
LOG_LEVEL=INFO
EOF

# Run the container
docker run -d \
  --name ews-mcp-server \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  ghcr.io/azizmazrou/ews-mcp:latest

# View logs - look for "EWS-MCP v3.0 starting"
docker logs -f ews-mcp-server
```

#### Option 2: OAuth2 Authentication (Production - Office 365)

**Best for**: Office 365, production environments, enhanced security

```bash
# Pull the latest image
docker pull ghcr.io/azizmazrou/ews-mcp:latest

# Use pre-configured OAuth2 template
cp .env.oauth2.example .env

# Edit .env with your Azure AD credentials:
# - EWS_CLIENT_ID (from Azure AD app registration)
# - EWS_CLIENT_SECRET (from Azure AD app registration)
# - EWS_TENANT_ID (from Azure AD app registration)
# See OAuth2 Setup section below for detailed instructions

# Run the container
docker run -d \
  --name ews-mcp-server \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  ghcr.io/azizmazrou/ews-mcp:latest

# View logs
docker logs -f ews-mcp-server
```

### Building from Source

```bash
# Clone repository
git clone https://github.com/azizmazrou/ews-mcp.git
cd ews-mcp

# Copy and configure environment
cp .env.example .env
# Edit .env with your Exchange credentials

# Build and run with Docker Compose
docker-compose up -d

# View logs
docker-compose logs -f
```

### Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements-dev.txt

# Configure environment
cp .env.example .env
# Edit .env with your credentials

# Run server
python -m src.main
```

## Configuration

Choose your authentication method based on your Exchange setup:

| Auth Method | Use Case | Setup Time | Security |
|-------------|----------|------------|----------|
| **Basic Auth** | Testing, On-premises Exchange | 1 minute | Moderate |
| **OAuth2** | Office 365, Production | 10 minutes | High |
| **NTLM** | Windows Domain, On-premises | 5 minutes | Moderate |

### Basic Authentication (Easiest - For Testing/On-Premises)

**Best for**: Quick testing, on-premises Exchange servers, legacy setups

**Note**: Basic Auth is being deprecated by Microsoft for Office 365. Use OAuth2 for production Office 365 environments.

```bash
cat > .env <<EOF
# Exchange Server
EWS_SERVER_URL=https://mail.company.com/EWS/Exchange.asmx
EWS_EMAIL=user@company.com
EWS_AUTODISCOVER=false

# Basic Authentication
EWS_AUTH_TYPE=basic
EWS_USERNAME=user@company.com
EWS_PASSWORD=your-password

# Server Configuration
LOG_LEVEL=INFO
EOF
```

### OAuth2 Authentication (Recommended for Office 365)

**Best for**: Office 365/Microsoft 365, production environments, modern security

1. **Register Application in Azure AD**:
   - Go to Azure Portal > Azure Active Directory > App registrations
   - Click "New registration"
   - Name: "EWS MCP Server"
   - Supported account types: "Accounts in this organizational directory only"
   - Click "Register"

2. **Configure API Permissions**:
   - Go to "API permissions"
   - Add "Office 365 Exchange Online" > Application permissions
   - Add: `full_access_as_app` or specific permissions
   - Click "Grant admin consent"

3. **Create Client Secret**:
   - Go to "Certificates & secrets"
   - Create new client secret
   - **Copy the secret value immediately**

4. **Update .env**:
   ```bash
   EWS_SERVER_URL=https://outlook.office365.com/EWS/Exchange.asmx
   EWS_EMAIL=user@company.com
   EWS_AUTH_TYPE=oauth2
   EWS_CLIENT_ID=<your-client-id>
   EWS_CLIENT_SECRET=<your-client-secret>
   EWS_TENANT_ID=<your-tenant-id>
   ```

## Usage with Claude Desktop

Add to your Claude Desktop configuration file:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

### Using Pre-built Image from GHCR (Recommended)

```json
{
  "mcpServers": {
    "ews": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "--env-file",
        "/absolute/path/to/.env",
        "ghcr.io/azizmazrou/ews-mcp:latest"
      ]
    }
  }
}
```

### Using Locally Built Image

```json
{
  "mcpServers": {
    "ews": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "--env-file",
        "/path/to/ews-mcp/.env",
        "ews-mcp-server"
      ]
    }
  }
}
```

## Available Tools

**Total: 44 base tools across 9 categories**

### Contact Intelligence Tools (3 tools)

- **find_person**: Search for contacts across GAL, email history, and domains
  - **v3.0 Enhanced**: Multi-strategy search that never returns 0 results
  - Multi-source search with intelligent deduplication
  - Ranking by communication frequency and recency
  - Domain-wide search (find all @example.com contacts)
  - Arabic language support (UTF-8)

- **get_communication_history**: Analyze communication with a specific contact
  - Email statistics (sent, received, total)
  - Monthly timeline visualization
  - Topic extraction from subjects
  - Recent emails preview

- **analyze_network**: Professional network analysis
  - Top contacts by volume
  - Domain/organization grouping
  - Dormant relationship detection
  - VIP contact identification
  - Comprehensive overview mode

### Email Tools (9 tools)

- **send_email**: Send emails with attachments and CC/BCC
- **read_emails**: Read emails from specified folder
- **search_emails**: Search with advanced filters
- **get_email_details**: Get full email details
- **delete_email**: Delete or permanently remove emails
- **move_email**: Move emails between folders
- **copy_email**: Copy emails to folders while preserving originals
- **update_email**: Update email properties (read status, flags, categories, importance)
- **list_attachments**: List all attachments for an email message

### Attachment Tools (5 tools)

- **list_attachments**: List all attachments in an email
- **download_attachment**: Download attachment as base64 or save to file
- **add_attachment**: Add attachments to draft emails
- **delete_attachment**: Remove attachments from emails
- **read_attachment**: Extract text from PDF, DOCX, XLSX, PPTX, TXT, CSV, HTML, ZIP files
  - Supports Arabic (UTF-8) text
  - Table extraction from documents
  - Page limits for large PDFs
  - Returns structured text content

### Calendar Tools (6 tools)

- **create_appointment**: Schedule meetings with attendees
- **get_calendar**: Retrieve calendar events
- **update_appointment**: Modify existing appointments
- **delete_appointment**: Cancel appointments/meetings
- **respond_to_meeting**: Accept/decline meeting invitations
- **check_availability**: Get free/busy information for users
- **find_meeting_times**: AI-powered meeting time finder

### Contact Tools (6 tools)

- **create_contact**: Add new contacts
- **search_contacts**: Find contacts by name/email
- **get_contacts**: List all contacts
- **update_contact**: Modify contact information
- **delete_contact**: Remove contacts
- **resolve_names**: Resolve partial names/emails to full contact information

### Task Tools (5 tools)

- **create_task**: Create new tasks
- **get_tasks**: List tasks (filter by status)
- **update_task**: Modify task details
- **complete_task**: Mark tasks as complete
- **delete_task**: Remove tasks

### Search Tools (3 tools)

- **advanced_search**: Complex multi-criteria searches across folders
- **search_by_conversation**: Find all emails in a conversation thread
- **full_text_search**: Full-text search with case-sensitive and exact phrase options

### Folder Tools (5 tools)

- **list_folders**: Get mailbox folder hierarchy with details and item counts
- **create_folder**: Create new mailbox folders
- **delete_folder**: Delete folders (soft or permanent)
- **rename_folder**: Rename existing folders
- **move_folder**: Move folders to new parent locations

### Out-of-Office Tools (2 tools)

- **set_oof_settings**: Configure automatic replies (Enabled/Scheduled/Disabled)
- **get_oof_settings**: Retrieve current OOF settings with active status

## Architecture

### v3.0 Person-Centric Architecture

```
EWS MCP Server v3.0
├── MCP Protocol Layer (stdio/SSE)
├── Tool Registry
│   ├── Contact Intelligence Tools (PersonService)
│   ├── Email Tools (EmailService)
│   ├── Calendar Tools
│   ├── Contact Tools
│   └── Task Tools
├── Service Layer (NEW in v3.0)
│   ├── PersonService (orchestrates person discovery)
│   ├── EmailService (email operations)
│   ├── ThreadService (conversation threading)
│   └── AttachmentService (all format support)
├── Adapter Layer (NEW in v3.0)
│   ├── GALAdapter (multi-strategy search)
│   └── CacheAdapter (intelligent caching)
├── Core Models (NEW in v3.0)
│   ├── Person (first-class entity)
│   ├── EmailMessage
│   ├── ConversationThread
│   └── Attachment
├── EWS Client (exchangelib wrapper)
├── Authentication (OAuth2/Basic/NTLM)
├── Middleware (Rate Limiting, Error Handling, Audit)
└── Exchange Web Services API
```

### Key Design Principles

1. **Person-First** - Everything revolves around Person objects
2. **Separation of Concerns** - Models, services, adapters, tools
3. **Fail Gracefully** - Never crash, always return something
4. **Cache Aggressively** - Reduce Exchange load
5. **Rank Intelligently** - Best results first
6. **Merge Smartly** - Combine data from multiple sources

## Docker Images

Pre-built Docker images are automatically published to GitHub Container Registry:

```bash
# Pull latest version
docker pull ghcr.io/azizmazrou/ews-mcp:latest

# Pull specific version
docker pull ghcr.io/azizmazrou/ews-mcp:3.0.0

# Pull development version
docker pull ghcr.io/azizmazrou/ews-mcp:main
```

**Available Tags:**
- `latest` - Latest stable release
- `v*.*.*` - Specific version (e.g., `v3.0.0`)
- `main` - Latest commit on main branch
- `sha-<commit>` - Specific commit

**Multi-platform Support:**
- `linux/amd64` - x86_64 systems
- `linux/arm64` - ARM64 systems (Apple Silicon, ARM servers)

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src --cov-report=html

# Run specific test file
pytest tests/test_email_tools.py

# Run only unit tests (skip integration)
pytest -m "not integration"
```

## Development

```bash
# Install development dependencies
pip install -r requirements-dev.txt

# Run linter
ruff check src/

# Format code
black src/

# Type checking
mypy src/

# Security check
bandit -r src/
```

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for common issues and solutions.

## Documentation

- [Setup Guide](docs/SETUP.md) - Step-by-step setup instructions
- [Deployment Guide](docs/DEPLOYMENT.md) - Deploy to various platforms
- [GHCR Guide](docs/GHCR.md) - Using pre-built Docker images
- [API Documentation](docs/API.md) - Complete tool reference
- [Troubleshooting](docs/TROUBLESHOOTING.md) - Common issues and solutions
- [Architecture Overview](docs/ARCHITECTURE.md) - Technical deep dive
- [v3.0 Implementation Summary](docs/V3_IMPLEMENTATION_SUMMARY.md) - What's new in v3.0

## Previous Versions

### v2.1 - Contact Intelligence

Added 3 Contact Intelligence tools:
- `find_person` - Multi-source contact search
- `get_communication_history` - Relationship analysis
- `analyze_network` - Professional network analysis

### v2.0 - Enterprise Features

Expanded from 28 to 40 tools:
- Folder Management (4 tools)
- Enhanced Attachments (2 tools)
- Advanced Search (2 tools)
- Out-of-Office (2 tools)
- AI Meeting Time Finder
- Copy Email

## License

MIT License - See LICENSE file for details

## Contributing

Contributions are welcome! Please read the contributing guidelines before submitting PRs.

## Support

For issues and feature requests, please use the GitHub issue tracker.
