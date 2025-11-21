# Open WebUI Integration Guide

## Overview

EWS MCP Server now includes **built-in OpenAPI/REST support**, eliminating the need for external adapters like MCPO. This unified architecture provides both MCP SSE protocol and REST API endpoints on a single port (8000).

## Architecture

### Unified Server Design

```
┌─────────────────────────────────────────────────────────┐
│           EWS MCP Server (Port 8000)                    │
│                                                         │
│  ┌───────────────┐        ┌────────────────────────┐   │
│  │  MCP SSE      │        │  OpenAPI/REST Adapter  │   │
│  │  Transport    │        │  (Built-in)            │   │
│  │               │        │                        │   │
│  │  GET /sse     │        │  GET  /openapi.json    │   │
│  │  POST /messages│       │  POST /api/tools/{name}│   │
│  └───────┬───────┘        └────────┬───────────────┘   │
│          │                         │                   │
│          └─────────┬───────────────┘                   │
│                    │                                   │
│         ┌──────────▼──────────┐                        │
│         │   43+ Exchange      │                        │
│         │   Tools             │                        │
│         │                     │                        │
│         │  • Email (8)        │                        │
│         │  • Calendar (7)     │                        │
│         │  • Contacts (9)     │                        │
│         │  • Tasks (5)        │                        │
│         │  • Attachments (5)  │                        │
│         │  • Search (3)       │                        │
│         │  • Folders (5)      │                        │
│         │  • Out-of-Office (2)│                        │
│         └─────────────────────┘                        │
└─────────────────────────────────────────────────────────┘
           │                          │
           ▼                          ▼
    Claude Desktop              Open WebUI
    (MCP SSE)                   (REST API)
```

### Key Benefits

- **Single Port**: Both MCP and REST on port 8000
- **No External Dependencies**: Built-in OpenAPI adapter
- **Auto-Generated Schema**: OpenAPI schema generated from MCP tools
- **Type Safe**: Full TypeScript/JSON Schema validation
- **Hot Reload**: Schema updates automatically when tools change

## Quick Start

### 1. Setup and Start Server

```bash
# Clone the repository
git clone https://github.com/azizmazrou/ews-mcp.git
cd ews-mcp

# Create .env file
cat > .env << EOF
EWS_EMAIL=your-email@company.com
EWS_PASSWORD=your-password
EWS_SERVER_URL=https://your-exchange-server/EWS/Exchange.asmx
EWS_AUTH_TYPE=basic
TIMEZONE=Asia/Riyadh
EOF

# Start the server
./start-openwebui.sh
```

### 2. Verify Server is Running

```bash
# Check health
curl http://localhost:8000/health

# View OpenAPI schema
curl http://localhost:8000/openapi.json | jq

# List all available tools
curl http://localhost:8000/openapi.json | jq '.paths | keys'
```

### 3. Test REST API

```bash
# Read emails
curl -X POST http://localhost:8000/api/tools/read_emails \
  -H 'Content-Type: application/json' \
  -d '{"max_results": 5}' | jq

# Get calendar events
curl -X POST http://localhost:8000/api/tools/get_calendar \
  -H 'Content-Type: application/json' \
  -d '{"days": 7}' | jq

# Search contacts
curl -X POST http://localhost:8000/api/tools/search_contacts \
  -H 'Content-Type: application/json' \
  -d '{"search_term": "John"}' | jq
```

## Open WebUI Integration

### Method 1: External API Configuration (Recommended)

1. **Open Open WebUI** in your browser (http://localhost:3000)

2. **Navigate to Admin Settings**:
   - Click your profile → Admin Settings
   - Go to: Functions → External APIs

3. **Add EWS MCP as External API**:
   - Click "+ Add External API"
   - Fill in the details:
     ```
     API Base URL: http://ews-mcp:8000
     Name: Exchange Web Services
     Description: Access to Exchange emails, calendar, contacts, and tasks
     ```
   - Click "Save"

4. **Auto-Discovery**:
   - Open WebUI will automatically fetch `/openapi.json`
   - All 43+ tools will be discovered and made available
   - Tools will appear in the function picker during chats

5. **Start Using**:
   - Create a new chat
   - Type a message that requires Exchange data
   - Open WebUI will automatically call the appropriate tools

### Method 2: Docker Compose (All-in-One)

Uncomment the Open WebUI section in `docker-compose.openwebui.yml`:

```yaml
services:
  ews-mcp:
    # ... (already configured)

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    ports:
      - "3000:8080"
    volumes:
      - open-webui-data:/app/backend/data
    environment:
      - OLLAMA_BASE_URL=http://host.docker.internal:11434
      - ENABLE_OAUTH_SIGNUP=false
      - ENABLE_SIGNUP=true
      - EXTERNAL_FUNCTIONS=http://ews-mcp:8000/openapi.json
    networks:
      - mcp-network
    restart: unless-stopped
    depends_on:
      ews-mcp:
        condition: service_healthy
```

Then restart:
```bash
docker-compose -f docker-compose.openwebui.yml up -d
```

## Available Endpoints

### MCP SSE Endpoints (for Claude Desktop)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/sse` | SSE transport for MCP protocol |
| POST | `/messages` | MCP message handling |

### REST API Endpoints (for Open WebUI)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check with tool count |
| GET | `/openapi.json` | OpenAPI 3.0 schema |
| POST | `/api/tools/{tool_name}` | Execute specific tool |

## REST API Examples

### Email Tools

#### Send Email
```bash
curl -X POST http://localhost:8000/api/tools/send_email \
  -H 'Content-Type: application/json' \
  -d '{
    "to_recipients": ["user@example.com"],
    "subject": "Test Email",
    "body": "This is a test email",
    "body_type": "text"
  }' | jq
```

#### Read Emails
```bash
curl -X POST http://localhost:8000/api/tools/read_emails \
  -H 'Content-Type: application/json' \
  -d '{
    "folder_name": "Inbox",
    "max_results": 10,
    "include_body": false
  }' | jq
```

#### Search Emails
```bash
curl -X POST http://localhost:8000/api/tools/search_emails \
  -H 'Content-Type: application/json' \
  -d '{
    "search_query": "subject:meeting",
    "max_results": 20
  }' | jq
```

#### Move Email
```bash
curl -X POST http://localhost:8000/api/tools/move_email \
  -H 'Content-Type: application/json' \
  -d '{
    "message_id": "AAMkAD...",
    "target_folder": "Archive"
  }' | jq
```

#### Delete Email (Soft Delete - moves to trash)
```bash
curl -X POST http://localhost:8000/api/tools/delete_email \
  -H 'Content-Type: application/json' \
  -d '{
    "message_id": "AAMkAD..."
  }' | jq
```

#### Update Email
```bash
curl -X POST http://localhost:8000/api/tools/update_email \
  -H 'Content-Type: application/json' \
  -d '{
    "message_id": "AAMkAD...",
    "is_read": true,
    "flag_status": "Flagged"
  }' | jq
```

### Calendar Tools

#### Create Appointment
```bash
curl -X POST http://localhost:8000/api/tools/create_appointment \
  -H 'Content-Type: application/json' \
  -d '{
    "subject": "Team Meeting",
    "start": "2025-01-15T10:00:00",
    "end": "2025-01-15T11:00:00",
    "location": "Conference Room A",
    "body": "Quarterly planning meeting",
    "required_attendees": ["team@example.com"]
  }' | jq
```

#### Get Calendar Events
```bash
curl -X POST http://localhost:8000/api/tools/get_calendar \
  -H 'Content-Type: application/json' \
  -d '{
    "days": 7,
    "start_date": "2025-01-15"
  }' | jq
```

#### Update Appointment
```bash
curl -X POST http://localhost:8000/api/tools/update_appointment \
  -H 'Content-Type: application/json' \
  -d '{
    "appointment_id": "AAMkAD...",
    "subject": "Updated Meeting Title",
    "start": "2025-01-15T14:00:00"
  }' | jq
```

#### Respond to Meeting
```bash
curl -X POST http://localhost:8000/api/tools/respond_to_meeting \
  -H 'Content-Type: application/json' \
  -d '{
    "appointment_id": "AAMkAD...",
    "response": "accept",
    "message": "I will attend"
  }' | jq
```

#### Check Availability
```bash
curl -X POST http://localhost:8000/api/tools/check_availability \
  -H 'Content-Type: application/json' \
  -d '{
    "attendees": ["user1@example.com", "user2@example.com"],
    "start_time": "2025-01-15T09:00:00",
    "end_time": "2025-01-15T17:00:00",
    "duration_minutes": 60
  }' | jq
```

### Contact Tools

#### Create Contact
```bash
curl -X POST http://localhost:8000/api/tools/create_contact \
  -H 'Content-Type: application/json' \
  -d '{
    "given_name": "John",
    "surname": "Doe",
    "email_address": "john.doe@example.com",
    "company_name": "Acme Corp",
    "phone_number": "+1234567890",
    "job_title": "Software Engineer"
  }' | jq
```

#### Search Contacts
```bash
curl -X POST http://localhost:8000/api/tools/search_contacts \
  -H 'Content-Type: application/json' \
  -d '{
    "search_term": "John",
    "max_results": 20
  }' | jq
```

#### Get Contacts
```bash
curl -X POST http://localhost:8000/api/tools/get_contacts \
  -H 'Content-Type: application/json' \
  -d '{
    "max_results": 50
  }' | jq
```

#### Update Contact
```bash
curl -X POST http://localhost:8000/api/tools/update_contact \
  -H 'Content-Type: application/json' \
  -d '{
    "contact_id": "AAMkAD...",
    "phone_number": "+9876543210",
    "job_title": "Senior Engineer"
  }' | jq
```

#### Resolve Names (GAL Search)
```bash
curl -X POST http://localhost:8000/api/tools/resolve_names \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "john.doe"
  }' | jq
```

### Task Tools

#### Create Task
```bash
curl -X POST http://localhost:8000/api/tools/create_task \
  -H 'Content-Type: application/json' \
  -d '{
    "subject": "Complete project documentation",
    "body": "Write comprehensive docs",
    "due_date": "2025-01-20T17:00:00",
    "importance": "high"
  }' | jq
```

#### Get Tasks
```bash
curl -X POST http://localhost:8000/api/tools/get_tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "max_results": 20,
    "include_completed": false
  }' | jq
```

#### Complete Task
```bash
curl -X POST http://localhost:8000/api/tools/complete_task \
  -H 'Content-Type: application/json' \
  -d '{
    "task_id": "AAMkAD..."
  }' | jq
```

### Attachment Tools

#### List Attachments
```bash
curl -X POST http://localhost:8000/api/tools/list_attachments \
  -H 'Content-Type: application/json' \
  -d '{
    "item_id": "AAMkAD..."
  }' | jq
```

#### Download Attachment
```bash
curl -X POST http://localhost:8000/api/tools/download_attachment \
  -H 'Content-Type: application/json' \
  -d '{
    "attachment_id": "AAMkAD...",
    "save_path": "/tmp/document.pdf"
  }' | jq
```

#### Add Attachment
```bash
curl -X POST http://localhost:8000/api/tools/add_attachment \
  -H 'Content-Type: application/json' \
  -d '{
    "item_id": "AAMkAD...",
    "file_path": "/path/to/file.pdf"
  }' | jq
```

### Search Tools

#### Advanced Search
```bash
curl -X POST http://localhost:8000/api/tools/advanced_search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "project proposal",
    "item_types": ["email", "appointment"],
    "max_results": 50
  }' | jq
```

#### Full Text Search
```bash
curl -X POST http://localhost:8000/api/tools/full_text_search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "quarterly report",
    "max_results": 30
  }' | jq
```

### Folder Tools

#### List Folders
```bash
curl -X POST http://localhost:8000/api/tools/list_folders \
  -H 'Content-Type: application/json' \
  -d '{}' | jq
```

#### Create Folder
```bash
curl -X POST http://localhost:8000/api/tools/create_folder \
  -H 'Content-Type: application/json' \
  -d '{
    "folder_name": "Project Archive",
    "parent_folder": "Inbox"
  }' | jq
```

#### Rename Folder
```bash
curl -X POST http://localhost:8000/api/tools/rename_folder \
  -H 'Content-Type: application/json' \
  -d '{
    "folder_name": "Old Name",
    "new_folder_name": "New Name"
  }' | jq
```

### Out-of-Office Tools

#### Set Out-of-Office
```bash
curl -X POST http://localhost:8000/api/tools/set_oof_settings \
  -H 'Content-Type: application/json' \
  -d '{
    "state": "enabled",
    "internal_reply": "I am out of office",
    "external_reply": "I am currently unavailable",
    "start_time": "2025-01-20T00:00:00",
    "end_time": "2025-01-25T23:59:59"
  }' | jq
```

#### Get Out-of-Office Settings
```bash
curl -X POST http://localhost:8000/api/tools/get_oof_settings \
  -H 'Content-Type: application/json' \
  -d '{}' | jq
```

## Response Format

All REST API endpoints return JSON responses in this format:

### Success Response
```json
{
  "success": true,
  "data": {
    // Tool-specific response data
  },
  "message": "Operation completed successfully"
}
```

### Error Response
```json
{
  "success": false,
  "error": "Error message describing what went wrong",
  "tool": "tool_name",
  "status": 500
}
```

## Migration from MCPO

If you were previously using MCPO, here's how to migrate:

### Before (with MCPO)
```
Open WebUI → MCPO (port 9000) → EWS MCP (port 8001)
```

### After (unified server)
```
Open WebUI → EWS MCP (port 8000)
```

### Migration Steps

1. **Stop old services**:
   ```bash
   docker-compose down
   ```

2. **Update to latest code**:
   ```bash
   git pull origin main
   ```

3. **Update Open WebUI configuration**:
   - Remove old MCPO API configuration
   - Add new EWS MCP API: `http://ews-mcp:8000`

4. **Restart services**:
   ```bash
   ./start-openwebui.sh
   ```

5. **Verify**:
   - Check that Open WebUI discovers all tools
   - Test a few operations to ensure they work

## Troubleshooting

### Server won't start

```bash
# Check logs
docker-compose -f docker-compose.openwebui.yml logs ews-mcp

# Verify .env file exists and has correct credentials
cat .env

# Test Exchange connection manually
docker-compose -f docker-compose.openwebui.yml exec ews-mcp python -c "
from src.ews_client import EWSClient
from src.config import get_settings
client = EWSClient(get_settings())
print(client.test_connection())
"
```

### Open WebUI can't discover tools

```bash
# Verify OpenAPI endpoint is accessible
curl http://localhost:8000/openapi.json | jq

# Check if running in Docker network
docker network ls | grep mcp-network

# Verify Open WebUI can reach EWS MCP
docker-compose -f docker-compose.openwebui.yml exec open-webui curl http://ews-mcp:8000/health
```

### Tool execution fails

```bash
# Check tool availability
curl http://localhost:8000/health

# Test tool directly via REST API
curl -X POST http://localhost:8000/api/tools/read_emails \
  -H 'Content-Type: application/json' \
  -d '{"max_results": 1}' | jq

# Check server logs for detailed error
docker-compose -f docker-compose.openwebui.yml logs -f ews-mcp
```

### Performance issues

```bash
# Check resource usage
docker stats ews-mcp-server

# Reduce tool load by disabling categories
# In .env:
ENABLE_AI_TOOLS=false

# Restart
docker-compose -f docker-compose.openwebui.yml restart ews-mcp
```

## Advanced Configuration

### Custom Port

Edit `docker-compose.openwebui.yml`:

```yaml
services:
  ews-mcp:
    ports:
      - "9000:8000"  # Map external port 9000 to internal 8000
    environment:
      - MCP_PORT=8000  # Keep internal port as 8000
```

### Enable AI Tools

Add to `.env`:

```env
ENABLE_AI_TOOLS=true
AI_PROVIDER=openai
AI_API_KEY=sk-...
```

### Custom Timezone

Add to `.env`:

```env
TIMEZONE=America/New_York
```

### Logging Level

Add to `.env`:

```env
LOG_LEVEL=DEBUG  # DEBUG, INFO, WARNING, ERROR
```

## Security Considerations

1. **Never expose port 8000 to the public internet** without proper authentication
2. **Use HTTPS** in production with a reverse proxy (nginx, Caddy)
3. **Rotate credentials** regularly in your `.env` file
4. **Monitor logs** for suspicious activity
5. **Use network isolation** - keep EWS MCP in private Docker network

## Production Deployment

### With Nginx Reverse Proxy

```nginx
server {
    listen 443 ssl http2;
    server_name ews-api.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support
        proxy_buffering off;
        proxy_cache off;
    }
}
```

### With Authentication

Consider adding API key authentication at the reverse proxy level or implementing OAuth2.

## Support

- **GitHub Issues**: [https://github.com/azizmazrou/ews-mcp/issues](https://github.com/azizmazrou/ews-mcp/issues)
- **Documentation**: See README.md for general MCP usage
- **OpenAPI Spec**: `http://localhost:8000/openapi.json`

## License

MIT License - see LICENSE file for details.
