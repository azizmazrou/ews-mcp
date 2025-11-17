# EWS MCP Server Connection Guide

This guide explains how to connect to the EWS MCP Server using different transport methods.

## Available Transports

The EWS MCP Server supports two transport methods:

1. **STDIO** (Standard Input/Output) - Default, for local/CLI usage
2. **SSE** (Server-Sent Events over HTTP) - For web applications and remote access

## Transport Configuration

Set the transport type using the `MCP_TRANSPORT` environment variable:

- `MCP_TRANSPORT=stdio` (default)
- `MCP_TRANSPORT=sse`

## 1. STDIO Transport (Default)

### Use Case
- Claude Desktop integration
- Local command-line tools
- Process-based communication

### Configuration

**For Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "ews": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "--env-file",
        "/absolute/path/to/ews.env",
        "ghcr.io/azizmazrou/ews-mcp-server:latest"
      ]
    }
  }
}
```

**Environment file** (`ews.env`):
```bash
EWS_EMAIL=your@email.com
EWS_AUTH_TYPE=basic
EWS_USERNAME=your@email.com
EWS_PASSWORD=yourpassword
MCP_TRANSPORT=stdio
```

### Direct CLI Usage

```bash
docker run --rm -i \
  -e EWS_EMAIL=your@email.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=your@email.com \
  -e EWS_PASSWORD=yourpassword \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

## 2. SSE/HTTP Transport

### Use Case
- n8n integration
- Web applications
- Remote API access
- Multiple concurrent clients

### Configuration

**Run as HTTP server:**
```bash
docker run -d \
  -p 8000:8000 \
  -e EWS_EMAIL=your@email.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=your@email.com \
  -e EWS_PASSWORD=yourpassword \
  -e MCP_TRANSPORT=sse \
  -e MCP_HOST=0.0.0.0 \
  -e MCP_PORT=8000 \
  --name ews-mcp-server \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

### Endpoints

When running in SSE mode, the server exposes:

- **SSE Endpoint**: `http://localhost:8000/sse`
  - Long-lived connection for receiving events
  - Connect here to receive MCP protocol messages

- **Messages Endpoint**: `http://localhost:8000/messages`
  - POST endpoint for sending messages
  - Send MCP protocol requests here

### Custom Port

Change the port using `MCP_PORT`:

```bash
docker run -d \
  -p 3000:3000 \
  -e MCP_TRANSPORT=sse \
  -e MCP_PORT=3000 \
  -e EWS_EMAIL=your@email.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=your@email.com \
  -e EWS_PASSWORD=yourpassword \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

Server will be available at: `http://localhost:3000`

## 3. Integration Examples

### n8n Integration

1. **Add Docker node** in n8n workflow

2. **Configure the node**:
   - **Command**: `docker`
   - **Arguments**:
     ```
     run
     -d
     -p 8000:8000
     --env-file
     /path/to/ews.env
     ghcr.io/azizmazrou/ews-mcp-server:latest
     ```

3. **Create environment file** (`/path/to/ews.env`):
   ```bash
   EWS_EMAIL=user@company.com
   EWS_AUTH_TYPE=basic
   EWS_USERNAME=user@company.com
   EWS_PASSWORD=yourpassword
   MCP_TRANSPORT=sse
   MCP_HOST=0.0.0.0
   MCP_PORT=8000
   ```

4. **Connect with HTTP Request node**:
   - **SSE Connection**: GET `http://localhost:8000/sse`
   - **Send Commands**: POST `http://localhost:8000/messages`

### Python Client Example

```python
import httpx
import json

# Connect to SSE endpoint
async with httpx.AsyncClient() as client:
    # Send a request
    response = await client.post(
        "http://localhost:8000/messages",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "read_emails",
                "arguments": {
                    "folder": "inbox",
                    "limit": 10
                }
            },
            "id": 1
        }
    )

    result = response.json()
    print(result)
```

### JavaScript/Node.js Client Example

```javascript
const EventSource = require('eventsource');

// Connect to SSE endpoint
const sse = new EventSource('http://localhost:8000/sse');

sse.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Received:', data);
};

// Send a message
fetch('http://localhost:8000/messages', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    jsonrpc: '2.0',
    method: 'tools/call',
    params: {
      name: 'read_emails',
      arguments: { folder: 'inbox', limit: 10 }
    },
    id: 1
  })
});
```

### cURL Example

```bash
# Test the SSE endpoint
curl -N http://localhost:8000/sse

# Send a command
curl -X POST http://localhost:8000/messages \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/list",
    "id": 1
  }'
```

## Environment Variables Reference

### Transport Configuration

| Variable | Values | Default | Description |
|----------|--------|---------|-------------|
| `MCP_TRANSPORT` | `stdio`, `sse` | `stdio` | Transport type |
| `MCP_HOST` | IP address | `0.0.0.0` | Bind address (SSE only) |
| `MCP_PORT` | Port number | `8000` | HTTP port (SSE only) |

### Authentication (Required)

| Variable | Description |
|----------|-------------|
| `EWS_EMAIL` | Your Exchange email address |
| `EWS_AUTH_TYPE` | `basic`, `oauth2`, or `ntlm` |

**For BASIC/NTLM:**
| Variable | Description |
|----------|-------------|
| `EWS_USERNAME` | Username |
| `EWS_PASSWORD` | Password |

**For OAuth2:**
| Variable | Description |
|----------|-------------|
| `EWS_CLIENT_ID` | Azure AD Application ID |
| `EWS_CLIENT_SECRET` | Azure AD Application Secret |
| `EWS_TENANT_ID` | Azure AD Tenant ID |

## Docker Compose Example

For production deployments:

```yaml
version: '3.8'

services:
  ews-mcp-server:
    image: ghcr.io/azizmazrou/ews-mcp-server:latest
    ports:
      - "8000:8000"
    environment:
      # Transport
      MCP_TRANSPORT: sse
      MCP_HOST: 0.0.0.0
      MCP_PORT: 8000

      # Exchange Config
      EWS_EMAIL: user@company.com
      EWS_AUTH_TYPE: basic
      EWS_USERNAME: user@company.com
      EWS_PASSWORD: ${EWS_PASSWORD}

      # Optional
      LOG_LEVEL: INFO
      ENABLE_CACHE: true
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/sse"]
      interval: 30s
      timeout: 10s
      retries: 3
```

Run with:
```bash
EWS_PASSWORD=yourpassword docker-compose up -d
```

## Troubleshooting

### STDIO Mode Issues

**Problem**: "No response from server"
- **Solution**: Make sure you're using `-i` (interactive) flag with Docker
- **Solution**: Check that Claude Desktop config has proper environment variables

### SSE/HTTP Mode Issues

**Problem**: "Connection refused"
- **Solution**: Verify port mapping: `-p 8000:8000`
- **Solution**: Check `MCP_HOST` is set to `0.0.0.0` not `127.0.0.1`

**Problem**: "404 Not Found"
- **Solution**: Ensure you're using correct endpoints: `/sse` and `/messages`
- **Solution**: Verify `MCP_TRANSPORT=sse` is set

**Problem**: "Server not starting"
- **Solution**: Check logs: `docker logs ews-mcp-server`
- **Solution**: Verify all required credentials are provided

## Security Considerations

### STDIO Mode
- Credentials only visible to local process
- No network exposure
- ✅ Recommended for desktop use

### SSE/HTTP Mode
- **WARNING**: Credentials sent over network
- Use HTTPS in production (reverse proxy required)
- Consider firewall rules to limit access
- Use strong passwords
- Consider OAuth2 for production

### Production Deployment with HTTPS

Use nginx or traefik as reverse proxy:

```nginx
server {
    listen 443 ssl;
    server_name mcp.company.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

## Next Steps

- See [README.md](../README.md) for general setup
- See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) for common issues
- See [API.md](./API.md) for available tools and methods
