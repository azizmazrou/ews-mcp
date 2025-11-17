# EWS MCP Server - Quick Start Guide

Fast setup examples for running the EWS MCP Server.

## 🚀 Quick Start - Choose Your Method

### Option 1: STDIO Mode (Claude Desktop, CLI)

**One-liner with all credentials:**
```bash
docker run --rm -i \
  -e EWS_EMAIL=john.doe@company.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=john.doe@company.com \
  -e EWS_PASSWORD=YourPassword123 \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

### Option 2: HTTP/SSE Mode (n8n, Web Apps, APIs)

**One-liner for HTTP server:**
```bash
docker run -d \
  --name ews-mcp \
  -p 8000:8000 \
  -e MCP_TRANSPORT=sse \
  -e EWS_EMAIL=john.doe@company.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=john.doe@company.com \
  -e EWS_PASSWORD=YourPassword123 \
  ghcr.io/azizmazrou/ews-mcp-server:latest

# Test it:
curl http://localhost:8000/sse
```

### Option 3: Environment File (Recommended for Production)

**Create `.env` file:**
```bash
cat > ews-mcp.env << 'EOF'
# Exchange Configuration
EWS_EMAIL=john.doe@company.com
EWS_AUTH_TYPE=basic
EWS_USERNAME=john.doe@company.com
EWS_PASSWORD=YourPassword123

# Transport Mode
MCP_TRANSPORT=sse
MCP_HOST=0.0.0.0
MCP_PORT=8000

# Timezone (important for correct email timestamps!)
TIMEZONE=America/New_York

# Optional Settings
LOG_LEVEL=INFO
ENABLE_EMAIL=true
ENABLE_CALENDAR=true
ENABLE_CONTACTS=true
ENABLE_TASKS=true
EOF

# Run with env file:
docker run -d \
  --name ews-mcp \
  -p 8000:8000 \
  --env-file ews-mcp.env \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

---

## 📋 All Environment Variables

### ✅ REQUIRED Variables

| Variable | Example | Description |
|----------|---------|-------------|
| `EWS_EMAIL` | `john.doe@company.com` | Your Exchange email |
| `EWS_AUTH_TYPE` | `basic`, `oauth2`, `ntlm` | Authentication type |

### 🔐 Authentication Variables

#### For BASIC Authentication:
```bash
EWS_EMAIL=john.doe@company.com
EWS_AUTH_TYPE=basic
EWS_USERNAME=john.doe@company.com
EWS_PASSWORD=YourPassword123
```

#### For NTLM Authentication:
```bash
EWS_EMAIL=john.doe@company.com
EWS_AUTH_TYPE=ntlm
EWS_USERNAME=DOMAIN\\john.doe
EWS_PASSWORD=YourPassword123
```

#### For OAuth2 Authentication:
```bash
EWS_EMAIL=john.doe@company.com
EWS_AUTH_TYPE=oauth2
EWS_CLIENT_ID=12345678-1234-1234-1234-123456789abc
EWS_CLIENT_SECRET=your-client-secret-here
EWS_TENANT_ID=87654321-4321-4321-4321-cba987654321
```

### 🌐 Transport Variables

| Variable | Default | Options | Description |
|----------|---------|---------|-------------|
| `MCP_TRANSPORT` | `stdio` | `stdio`, `sse` | Connection type |
| `MCP_HOST` | `0.0.0.0` | IP address | Bind address (SSE only) |
| `MCP_PORT` | `8000` | Port number | HTTP port (SSE only) |

### ⚙️ Optional Variables

| Variable | Default | Options | Description |
|----------|---------|---------|-------------|
| `EWS_SERVER_URL` | (auto) | URL | Exchange server URL |
| `TIMEZONE` | `UTC` | Any TZ name | Timezone for email timestamps (e.g., America/New_York, Europe/London, Asia/Tokyo) |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR | Logging level |
| `ENABLE_EMAIL` | `true` | true/false | Enable email tools |
| `ENABLE_CALENDAR` | `true` | true/false | Enable calendar tools |
| `ENABLE_CONTACTS` | `true` | true/false | Enable contact tools |
| `ENABLE_TASKS` | `true` | true/false | Enable task tools |
| `ENABLE_CACHE` | `true` | true/false | Enable caching |
| `RATE_LIMIT_ENABLED` | `true` | true/false | Enable rate limiting |

---

## 🎯 Complete Examples by Use Case

### 1. Claude Desktop Integration

**File: `claude_desktop_config.json`**
```json
{
  "mcpServers": {
    "ews": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-e", "EWS_EMAIL=john.doe@company.com",
        "-e", "EWS_AUTH_TYPE=basic",
        "-e", "EWS_USERNAME=john.doe@company.com",
        "-e", "EWS_PASSWORD=YourPassword123",
        "ghcr.io/azizmazrou/ews-mcp-server:latest"
      ]
    }
  }
}
```

**OR with environment file (more secure):**
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
        "/Users/yourusername/.ews-mcp.env",
        "ghcr.io/azizmazrou/ews-mcp-server:latest"
      ]
    }
  }
}
```

**Create `/Users/yourusername/.ews-mcp.env`:**
```bash
EWS_EMAIL=john.doe@company.com
EWS_AUTH_TYPE=basic
EWS_USERNAME=john.doe@company.com
EWS_PASSWORD=YourPassword123
```

### 2. n8n Workflow

**Environment file: `ews-n8n.env`**
```bash
# Exchange Auth
EWS_EMAIL=john.doe@company.com
EWS_AUTH_TYPE=basic
EWS_USERNAME=john.doe@company.com
EWS_PASSWORD=YourPassword123

# HTTP Mode
MCP_TRANSPORT=sse
MCP_HOST=0.0.0.0
MCP_PORT=8000
```

**Docker command for n8n:**
```bash
docker run -d \
  --name ews-mcp-n8n \
  -p 8000:8000 \
  --env-file ews-n8n.env \
  --restart unless-stopped \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

**In n8n HTTP Request Node:**
- **Method**: POST
- **URL**: `http://localhost:8000/messages`
- **JSON Body**:
```json
{
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
```

### 3. Docker Compose (Production)

**File: `docker-compose.yml`**
```yaml
version: '3.8'

services:
  ews-mcp-server:
    image: ghcr.io/azizmazrou/ews-mcp-server:latest
    container_name: ews-mcp-server
    ports:
      - "8000:8000"
    environment:
      # Exchange Configuration
      EWS_EMAIL: ${EWS_EMAIL}
      EWS_AUTH_TYPE: ${EWS_AUTH_TYPE:-basic}
      EWS_USERNAME: ${EWS_USERNAME}
      EWS_PASSWORD: ${EWS_PASSWORD}

      # Transport
      MCP_TRANSPORT: sse
      MCP_HOST: 0.0.0.0
      MCP_PORT: 8000

      # Optional
      LOG_LEVEL: INFO
      ENABLE_EMAIL: true
      ENABLE_CALENDAR: true
      ENABLE_CONTACTS: true
      ENABLE_TASKS: true
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/sse"]
      interval: 30s
      timeout: 10s
      retries: 3
```

**Create `.env` file in same directory:**
```bash
EWS_EMAIL=john.doe@company.com
EWS_AUTH_TYPE=basic
EWS_USERNAME=john.doe@company.com
EWS_PASSWORD=YourPassword123
```

**Run:**
```bash
docker-compose up -d
```

### 4. Development/Testing

**Full debug mode:**
```bash
docker run -d \
  --name ews-mcp-dev \
  -p 8000:8000 \
  -e MCP_TRANSPORT=sse \
  -e EWS_EMAIL=john.doe@company.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=john.doe@company.com \
  -e EWS_PASSWORD=YourPassword123 \
  -e LOG_LEVEL=DEBUG \
  -e ENABLE_EMAIL=true \
  -e ENABLE_CALENDAR=true \
  -e ENABLE_CONTACTS=true \
  -e ENABLE_TASKS=true \
  ghcr.io/azizmazrou/ews-mcp-server:latest

# View logs:
docker logs -f ews-mcp-dev
```

---

## 🌍 Timezone Configuration

**Why it matters:** Exchange email timestamps are affected by timezone. If you see incorrect times, set your local timezone.

### Common Timezones

```bash
# United States
TIMEZONE=America/New_York      # Eastern Time
TIMEZONE=America/Chicago       # Central Time
TIMEZONE=America/Denver        # Mountain Time
TIMEZONE=America/Los_Angeles   # Pacific Time

# Europe
TIMEZONE=Europe/London         # UK
TIMEZONE=Europe/Paris          # France/Spain/Germany
TIMEZONE=Europe/Berlin         # Central Europe
TIMEZONE=Europe/Moscow         # Russia

# Asia
TIMEZONE=Asia/Tokyo            # Japan
TIMEZONE=Asia/Shanghai         # China
TIMEZONE=Asia/Dubai            # UAE
TIMEZONE=Asia/Riyadh           # Saudi Arabia (tested and verified)
TIMEZONE=Asia/Kolkata          # India

# Other
TIMEZONE=Australia/Sydney      # Australia
TIMEZONE=Pacific/Auckland      # New Zealand
```

### Example with Timezone

```bash
docker run -d \
  --name ews-mcp \
  -p 8000:8000 \
  -e MCP_TRANSPORT=sse \
  -e EWS_EMAIL=john.doe@company.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=john.doe@company.com \
  -e EWS_PASSWORD=YourPassword123 \
  -e TIMEZONE=America/New_York \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

### List All Available Timezones

```bash
docker run --rm ghcr.io/azizmazrou/ews-mcp-server:latest \
  ls /usr/share/zoneinfo/
```

---

## 🧪 Testing Commands

### Test STDIO Mode
```bash
docker run --rm -i \
  -e EWS_EMAIL=test@test.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=test@test.com \
  -e EWS_PASSWORD=test123 \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

### Test HTTP Mode
```bash
# Start server
docker run -d --name ews-test -p 8000:8000 \
  -e MCP_TRANSPORT=sse \
  -e EWS_EMAIL=test@test.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=test@test.com \
  -e EWS_PASSWORD=test123 \
  ghcr.io/azizmazrou/ews-mcp-server:latest

# Test SSE endpoint
curl -N http://localhost:8000/sse

# List available tools
curl -X POST http://localhost:8000/messages \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'

# Call a tool (read emails)
curl -X POST http://localhost:8000/messages \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "read_emails",
      "arguments": {
        "folder": "inbox",
        "limit": 5
      }
    },
    "id": 1
  }'

# Stop test server
docker stop ews-test && docker rm ews-test
```

---

## 🔧 Management Commands

### View Logs
```bash
# Real-time logs
docker logs -f ews-mcp

# Last 100 lines
docker logs --tail 100 ews-mcp

# Since specific time
docker logs --since 10m ews-mcp
```

### Stop/Start/Restart
```bash
# Stop
docker stop ews-mcp

# Start
docker start ews-mcp

# Restart
docker restart ews-mcp

# Remove
docker rm -f ews-mcp
```

### Update to Latest Version
```bash
# Pull latest image
docker pull ghcr.io/azizmazrou/ews-mcp-server:latest

# Stop and remove old container
docker stop ews-mcp && docker rm ews-mcp

# Start with new image
docker run -d --name ews-mcp -p 8000:8000 \
  --env-file ews-mcp.env \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

---

## 🆘 Troubleshooting

### Problem: Validation Error "auth requires ews_username and ews_password"
**Solution:** Make sure ALL required variables are set:
```bash
# Check what Docker sees:
docker run --rm \
  -e EWS_EMAIL=test@test.com \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=test@test.com \
  -e EWS_PASSWORD=test123 \
  ghcr.io/azizmazrou/ews-mcp-server:latest \
  env | grep EWS_
```

### Problem: Connection Refused (HTTP mode)
**Solution:** Check port mapping and transport mode:
```bash
# Correct:
docker run -d -p 8000:8000 -e MCP_TRANSPORT=sse ...

# Wrong (missing transport):
docker run -d -p 8000:8000 ...  # defaults to stdio!
```

### Problem: ImportError or Module Not Found
**Solution:** Pull the latest image with all fixes:
```bash
docker pull ghcr.io/azizmazrou/ews-mcp-server:latest
docker images | grep ews-mcp-server  # Check you have latest
```

### Problem: Server starts but can't connect to Exchange
**Solutions:**
1. **Check credentials are correct**
2. **Test manually:**
   ```bash
   docker run --rm -i \
     -e EWS_EMAIL=your@email.com \
     -e EWS_AUTH_TYPE=basic \
     -e EWS_USERNAME=your@email.com \
     -e EWS_PASSWORD=yourpassword \
     -e LOG_LEVEL=DEBUG \
     ghcr.io/azizmazrou/ews-mcp-server:latest
   ```
3. **Check logs:** `docker logs ews-mcp`

---

## 📚 Next Steps

- **Full Documentation**: See [CONNECTION_GUIDE.md](docs/CONNECTION_GUIDE.md)
- **Troubleshooting**: See [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- **API Reference**: See [README.md](README.md) for available tools

---

## 🚨 Quick Reference Card

### Minimum Required (BASIC Auth):
```bash
EWS_EMAIL=your@email.com
EWS_AUTH_TYPE=basic
EWS_USERNAME=your@email.com
EWS_PASSWORD=yourpassword
```

### For HTTP/API Mode, ADD:
```bash
MCP_TRANSPORT=sse
MCP_PORT=8000
```

### Docker Command Template:
```bash
docker run -d \
  --name ews-mcp \
  -p 8000:8000 \
  -e EWS_EMAIL=... \
  -e EWS_AUTH_TYPE=basic \
  -e EWS_USERNAME=... \
  -e EWS_PASSWORD=... \
  -e MCP_TRANSPORT=sse \
  ghcr.io/azizmazrou/ews-mcp-server:latest
```

**Test URL:** `http://localhost:8000/sse`
