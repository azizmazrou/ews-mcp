# Open WebUI Integration Guide for EWS MCP Server

## Overview

The EWS MCP Server now has **built-in OpenAPI/REST support**, eliminating the need for MCPO as a separate bridge. The server exposes both MCP SSE endpoints (for MCP clients like Claude Desktop) and REST API endpoints (for Open WebUI and other HTTP clients) on the same port.

## Architecture

```
Open WebUI → EWS MCP Server (Port 8000)
             ├─ GET  /sse                    (MCP SSE connection)
             ├─ POST /messages               (MCP messages)
             ├─ GET  /openapi.json           (OpenAPI 3.0 schema)
             ├─ POST /api/tools/{tool_name}  (REST tool execution)
             └─ GET  /health                 (Health check)
```

**Benefits:**
- ✅ Single server for both MCP and REST protocols
- ✅ No additional proxy needed
- ✅ Simplified deployment
- ✅ Direct tool invocation via REST API
- ✅ Auto-generated OpenAPI schema

## Quick Start

### Using the Automated Script (Easiest)

```bash
# Run the setup script
./start-openwebui.sh
```

This will:
1. Validate your `.env` file
2. Build and start the EWS MCP server
3. Display available endpoints
4. Show next steps for Open WebUI configuration

### Manual Setup with Docker Compose

Use the provided `docker-compose.openwebui.yml`:

```bash
# Start the server
docker-compose -f docker-compose.openwebui.yml up -d

# Check health
curl http://localhost:8000/health

# View OpenAPI schema
curl http://localhost:8000/openapi.json

# View logs
docker-compose -f docker-compose.openwebui.yml logs -f
```

### Using Existing Docker Setup

If you already have the server running:

```bash
# The server now exposes REST endpoints by default
# No configuration change needed!

# Test OpenAPI endpoint
curl http://localhost:8000/openapi.json | jq
```

## Configure Open WebUI

### 1. Access Open WebUI Admin Panel
- Navigate to your Open WebUI instance
- Go to **Admin Settings** → **Connections** → **External APIs**

### 2. Add EWS MCP Server
- **API Base URL**: `http://localhost:8000` (or your server URL)
- **Name**: `Exchange Web Services`
- **Description**: `Microsoft Exchange operations via MCP`

### 3. Auto-Discovery
Open WebUI will automatically:
- Fetch the OpenAPI schema from `/openapi.json`
- Discover all 44 Exchange tools
- Make them available in the chat interface

### 4. Test the Connection
Try a simple query like:
```
"Read my last 5 emails"
"Show me today's calendar"
"Search my contacts for John"
```

## Available Endpoints

### MCP Protocol (for Claude Desktop, etc.)
- `GET  /sse` - Server-Sent Events connection
- `POST /messages` - Message handling

### REST API (for Open WebUI, etc.)
- `GET  /openapi.json` - OpenAPI 3.0 schema
- `POST /api/tools/send_email` - Send email
- `POST /api/tools/read_emails` - Read emails
- `POST /api/tools/search_emails` - Search emails
- `POST /api/tools/get_calendar` - Get calendar events
- `POST /api/tools/create_contact` - Create contact
- ... (44 tools total)

### Utility
- `GET  /health` - Health check endpoint

## Testing the REST API

### List Available Tools
```bash
# Get OpenAPI schema
curl http://localhost:8000/openapi.json | jq '.paths | keys'
```

### Read Emails
```bash
curl -X POST http://localhost:8000/api/tools/read_emails \
  -H 'Content-Type: application/json' \
  -d '{
    "folder": "inbox",
    "max_results": 5,
    "unread_only": false
  }'
```

### Search Emails
```bash
curl -X POST http://localhost:8000/api/tools/search_emails \
  -H 'Content-Type: application/json' \
  -d '{
    "subject_contains": "meeting",
    "max_results": 10
  }'
```

### Get Calendar
```bash
curl -X POST http://localhost:8000/api/tools/get_calendar \
  -H 'Content-Type: application/json' \
  -d '{
    "days_ahead": 7,
    "max_results": 20
  }'
```

### Create Contact
```bash
curl -X POST http://localhost:8000/api/tools/create_contact \
  -H 'Content-Type: application/json' \
  -d '{
    "given_name": "John",
    "surname": "Doe",
    "email_address": "john.doe@example.com",
    "phone_number": "+1234567890"
  }'
```

## Available Tools (44 Total)

### Email Tools (8)
- `send_email` - Send email with attachments
- `read_emails` - Read emails from folder
- `search_emails` - Search with filters
- `get_email_details` - Get full email details
- `delete_email` - Delete or move to trash
- `move_email` - Move between folders
- `update_email` - Update flags/categories
- `copy_email` - Copy to another folder

### Calendar Tools (7)
- `create_appointment` - Create calendar event
- `get_calendar` - Retrieve events
- `update_appointment` - Modify event
- `delete_appointment` - Delete event
- `respond_to_meeting` - Accept/decline
- `check_availability` - Check free/busy
- `find_meeting_times` - Find available slots

### Contact Tools (9)
- `create_contact` - Create new contact
- `search_contacts` - Search contacts
- `get_contacts` - List contacts
- `update_contact` - Modify contact
- `delete_contact` - Remove contact
- `resolve_names` - Resolve email addresses
- `find_person` - Smart person search
- `get_communication_history` - Email history
- `analyze_network` - Contact relationships

### Task Tools (5)
- `create_task` - Create task
- `get_tasks` - List tasks
- `update_task` - Modify task
- `complete_task` - Mark complete
- `delete_task` - Remove task

### Attachment Tools (5)
- `list_attachments` - List email attachments
- `download_attachment` - Download file
- `add_attachment` - Add to email
- `delete_attachment` - Remove attachment
- `read_attachment` - Read attachment content

### Search Tools (3)
- `advanced_search` - Multi-folder search
- `search_by_conversation` - Conversation threads
- `full_text_search` - Full-text search

### Folder Tools (5)
- `list_folders` - List all folders
- `create_folder` - Create new folder
- `delete_folder` - Delete folder
- `rename_folder` - Rename folder
- `move_folder` - Move folder

### Out-of-Office Tools (2)
- `set_oof_settings` - Set OOF message
- `get_oof_settings` - Get OOF status

## Troubleshooting

### OpenAPI Schema Not Loading

**Problem**: Open WebUI can't fetch schema
```bash
# Check if server is running
curl http://localhost:8000/health

# Check OpenAPI endpoint
curl http://localhost:8000/openapi.json
```

**Solution**: Ensure server is running and accessible from Open WebUI container

### Tools Not Appearing in Open WebUI

**Problem**: Tools don't show up after adding connection

**Check**:
1. Verify OpenAPI schema is valid: `curl http://localhost:8000/openapi.json | jq`
2. Check Open WebUI logs for errors
3. Restart Open WebUI to force re-fetch

### Network Connectivity

If using Docker networks:
```bash
# Check connectivity from Open WebUI container
docker exec open-webui curl http://ews-mcp:8000/health

# Or use host network mode
docker run --network host ...
```

### REST API Returns 404

**Problem**: `/api/tools/tool_name` returns 404

**Check**:
- Tool name is correct (use `/openapi.json` to list available tools)
- Using POST method (not GET)
- Server is fully started and tools are registered

## Security Considerations

### Authentication

Currently, the REST API doesn't require authentication. For production:

```python
# Add API key middleware
# In src/main.py, check for Authorization header
```

### Network Security

1. **Use TLS/SSL**: Put server behind reverse proxy with TLS
2. **Firewall**: Only allow connections from Open WebUI
3. **Rate Limiting**: Enable rate limiting in config

Example Nginx config:
```nginx
server {
    listen 443 ssl;
    server_name ews-mcp.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## Performance Tips

### Connection Pooling
The server reuses Exchange connections. Monitor with:
```bash
curl http://localhost:8000/health
```

### Request Timeout
Adjust timeout for large operations:
```env
REQUEST_TIMEOUT=300  # 5 minutes
```

### Concurrent Requests
The async server handles concurrent requests efficiently. No special configuration needed.

## Monitoring

### Health Check
```bash
# Simple health check
curl http://localhost:8000/health

# Should return:
# {"status":"ok","tools":44}
```

### Server Logs
```bash
# View logs
docker-compose -f docker-compose.openwebui.yml logs -f ews-mcp

# Filter for errors
docker-compose -f docker-compose.openwebui.yml logs ews-mcp | grep ERROR
```

### Metrics
Enable structured logging in `.env`:
```env
LOG_LEVEL=INFO
ENABLE_AUDIT_LOG=true
```

## Migration from MCPO

If you were using MCPO before:

### What Changed
- ✅ No separate MCPO container needed
- ✅ Port changed from 9000 → 8000
- ✅ Direct connection to EWS MCP
- ✅ Same functionality, simpler setup

### Update Open WebUI
1. Remove old MCPO connection
2. Add new connection pointing to `http://localhost:8000`
3. Tools will auto-discover

### Update Docker Compose
```bash
# Stop old setup
docker-compose -f docker-compose.openwebui.yml down

# Pull latest code
git pull

# Start new setup
./start-openwebui.sh
```

## Support

### Resources
- [MCP Protocol Specification](https://spec.modelcontextprotocol.io/)
- [Open WebUI Documentation](https://docs.openwebui.com/)
- [EWS MCP GitHub](https://github.com/azizmazrou/ews-mcp)

### Common Issues
See [Troubleshooting](#troubleshooting) section above

## Next Steps

1. **Test the REST API** with curl commands above
2. **Configure Open WebUI** to connect to the server
3. **Try sample queries** in Open WebUI chat
4. **Enable monitoring** with health checks and logs
5. **Set up TLS** for production deployments

---

**Note**: The server now provides both MCP SSE and REST API on port 8000. No separate MCPO proxy is needed!
