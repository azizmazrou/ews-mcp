# Open WebUI Integration Guide for EWS MCP Server

## Overview

Open WebUI doesn't natively support SSE-based MCP servers. You need to use **MCPO** (MCP-to-OpenAPI proxy) as a bridge to translate your SSE-based EWS MCP server into OpenAPI-compatible endpoints.

## Architecture

```
Open WebUI → MCPO (Port 8000) → EWS MCP Server (Port 8000 internally)
```

## Setup Options

### Option 1: Using Docker Compose (Recommended)

Create `docker-compose.openwebui.yml`:

```yaml
version: '3.8'

services:
  # Your EWS MCP Server
  ews-mcp:
    build: .
    container_name: ews-mcp-server
    ports:
      - "8001:8000"  # Expose on port 8001 externally
    environment:
      - EWS_EMAIL=${EWS_EMAIL}
      - EWS_PASSWORD=${EWS_PASSWORD}
      - EWS_SERVER_URL=${EWS_SERVER_URL}
      - EWS_AUTH_TYPE=${EWS_AUTH_TYPE}
      - MCP_TRANSPORT=sse
      - MCP_HOST=0.0.0.0
      - MCP_PORT=8000
    networks:
      - mcp-network
    restart: unless-stopped

  # MCPO Proxy (Bridge to Open WebUI)
  mcpo:
    image: openwebui/mcpo:latest
    container_name: mcpo-proxy
    ports:
      - "9000:8000"  # MCPO on port 9000
    command: >
      --port 8000
      --api-key "${MCPO_API_KEY:-your-secret-key}"
      --server-type "sse"
      --
      http://ews-mcp:8000/sse
    networks:
      - mcp-network
    depends_on:
      - ews-mcp
    restart: unless-stopped

  # Open WebUI (Optional - if you don't have it running)
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    ports:
      - "3000:8080"
    volumes:
      - open-webui-data:/app/backend/data
    environment:
      - OLLAMA_BASE_URL=http://host.docker.internal:11434
    networks:
      - mcp-network
    restart: unless-stopped

networks:
  mcp-network:
    driver: bridge

volumes:
  open-webui-data:
```

**Start the stack:**
```bash
docker-compose -f docker-compose.openwebui.yml up -d
```

### Option 2: Manual Setup with MCPO

#### 1. Start your EWS MCP Server
```bash
# Make sure your server is running on port 8001
docker-compose up -d
```

#### 2. Run MCPO
```bash
# Using Docker
docker run -d \
  --name mcpo-proxy \
  --network host \
  -p 9000:8000 \
  openwebui/mcpo:latest \
  --port 8000 \
  --api-key "your-secret-key" \
  --server-type "sse" \
  -- \
  http://localhost:8001/sse

# Or using npx (if you have Node.js)
npx @openwebui/mcpo \
  --port 9000 \
  --api-key "your-secret-key" \
  --server-type "sse" \
  -- \
  http://localhost:8001/sse
```

### Option 3: MCPO with Configuration File

Create `mcpo-config.json`:

```json
{
  "mcpServers": {
    "ews-exchange": {
      "type": "sse",
      "url": "http://localhost:8001/sse",
      "headers": {
        "Authorization": "Bearer your-token-if-needed"
      }
    }
  }
}
```

Run MCPO with config:
```bash
docker run -d \
  --name mcpo-proxy \
  --network host \
  -v $(pwd)/mcpo-config.json:/app/config.json \
  -p 9000:8000 \
  openwebui/mcpo:latest \
  --config /app/config.json \
  --port 8000 \
  --api-key "your-secret-key"
```

## Configure Open WebUI

### 1. Access Open WebUI Admin Panel
- Navigate to: `http://localhost:3000` (or your Open WebUI URL)
- Go to **Admin Settings** → **Connections**

### 2. Add MCPO as an External API
- **API Base URL**: `http://localhost:9000` (or your MCPO URL)
- **API Key**: `your-secret-key` (same as MCPO_API_KEY)
- **Name**: `Exchange Web Services`

### 3. Test the Connection
Open WebUI will fetch the OpenAPI schema from MCPO and discover all 44 EWS tools.

## Available Tools in Open WebUI

Once configured, you'll have access to 44 Exchange tools:

### Email Tools (8)
- send_email
- read_emails
- search_emails
- get_email_details
- delete_email
- move_email
- update_email
- copy_email

### Calendar Tools (7)
- create_appointment
- get_calendar
- update_appointment
- delete_appointment
- respond_to_meeting
- check_availability
- find_meeting_times

### Contact Tools (9)
- create_contact
- search_contacts
- get_contacts
- update_contact
- delete_contact
- resolve_names
- find_person
- get_communication_history
- analyze_network

### Task Tools (5)
- create_task
- get_tasks
- update_task
- complete_task
- delete_task

### Other Tools (15)
- Attachments (5 tools)
- Search (3 tools)
- Folders (5 tools)
- Out-of-Office (2 tools)

## Troubleshooting

### MCPO Connection Issues

**Problem**: MCPO can't connect to EWS MCP server
```bash
# Check EWS MCP is running
curl http://localhost:8001/sse

# Check MCPO logs
docker logs mcpo-proxy
```

**Problem**: Open WebUI can't connect to MCPO
```bash
# Check MCPO is running and accessible
curl http://localhost:9000/openapi.json

# Should return OpenAPI schema with all 44 tools
```

### Network Issues

If using Docker networks, ensure all services are on the same network:
```bash
# Check network connectivity
docker exec mcpo-proxy ping ews-mcp
```

### Authentication Issues

If your EWS server requires authentication headers:
```json
{
  "mcpServers": {
    "ews-exchange": {
      "type": "sse",
      "url": "http://ews-mcp:8000/sse",
      "headers": {
        "Authorization": "Bearer ${YOUR_TOKEN}"
      }
    }
  }
}
```

## Security Considerations

1. **API Key**: Use a strong API key for MCPO
   ```bash
   export MCPO_API_KEY=$(openssl rand -hex 32)
   ```

2. **Network Isolation**: Run all services in a private Docker network

3. **TLS/SSL**: For production, put MCPO behind a reverse proxy with TLS:
   ```nginx
   server {
       listen 443 ssl;
       server_name mcpo.yourdomain.com;

       ssl_certificate /path/to/cert.pem;
       ssl_certificate_key /path/to/key.pem;

       location / {
           proxy_pass http://localhost:9000;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
       }
   }
   ```

## Monitoring

Check MCPO metrics and health:
```bash
# Health check
curl http://localhost:9000/health

# Metrics (if enabled)
curl http://localhost:9000/metrics
```

## Next Steps

1. Start your EWS MCP server
2. Start MCPO with your configuration
3. Configure Open WebUI to connect to MCPO
4. Test a simple tool like `read_emails` or `get_calendar`
5. Monitor logs for any issues

## Resources

- [Open WebUI MCP Documentation](https://docs.openwebui.com/features/mcp/)
- [MCPO GitHub Repository](https://github.com/open-webui/mcpo)
- [MCP Protocol Specification](https://spec.modelcontextprotocol.io/)
