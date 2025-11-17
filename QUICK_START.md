# Quick Start Guide - EWS MCP Server

Get started with EWS MCP Server in under 5 minutes!

**⚠️ Note**: Pre-built images from GHCR will be available once merged to main. For now, build locally (adds 2-3 minutes).

## Choose Your Setup Method

### 🚀 Super Quick (3-4 minutes) - Basic Auth

**Best for**: Testing, on-premises Exchange, demos

```bash
# 1. Clone and build
git clone https://github.com/azizmazrou/ews-mcp.git
cd ews-mcp
docker build -t ews-mcp-server:latest .

# 2. Create config
cat > .env <<EOF
EWS_SERVER_URL=https://mail.company.com/EWS/Exchange.asmx
EWS_EMAIL=user@company.com
EWS_AUTODISCOVER=false
EWS_AUTH_TYPE=basic
EWS_USERNAME=user@company.com
EWS_PASSWORD=your-password
LOG_LEVEL=INFO
EOF

# 3. Run
docker run -d --name ews-mcp --env-file .env ews-mcp-server:latest

# 4. Check
docker logs ews-mcp
```

✅ **Done!** Look for "✓ Successfully connected to Exchange"

### 🎯 Interactive Setup (3-4 minutes) - Guided

```bash
# Clone if not done
git clone https://github.com/azizmazrou/ews-mcp.git
cd ews-mcp

# Build once
docker build -t ews-mcp-server:latest .

# Run interactive setup
./scripts/setup-basic-auth.sh
```

The script will:
1. ✅ Prompt for your Exchange details
2. ✅ Create `.env` file automatically
3. ✅ Pull Docker image
4. ✅ Start the server
5. ✅ Verify connection

### 🔒 Secure Setup (10 minutes) - OAuth2

**Best for**: Office 365, production

```bash
# 1. Copy OAuth2 template
cp .env.oauth2.example .env

# 2. Register app in Azure AD (see detailed guide)
# 3. Update .env with Azure credentials
# 4. Run
docker run -d --env-file .env ghcr.io/azizmazrou/ews-mcp:latest
```

See [OAuth2 Setup Guide](docs/SETUP.md#oauth2-setup) for detailed Azure AD registration steps.

## Verify It's Working

```bash
# Check logs
docker logs ews-mcp

# Should see:
# ✓ Successfully connected to Exchange
# Registered 21 tools
# Server ready - listening on stdio
```

## Use with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ews": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "--env-file", "/absolute/path/to/.env",
        "ghcr.io/azizmazrou/ews-mcp:latest"
      ]
    }
  }
}
```

**Configuration file locations**:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

## Test It

In Claude Desktop, try:
```
Check my unread emails
```

or

```
What meetings do I have today?
```

## Common Issues

### "Authentication failed"
✅ Check username/password are correct
✅ Try logging into Outlook Web Access first

### "Connection refused"
✅ Verify server URL: `curl https://mail.company.com/EWS/Exchange.asmx`
✅ Check firewall/network

### "Cannot find Exchange server"
✅ Try autodiscovery: Set `EWS_AUTODISCOVER=true` in `.env`

## Next Steps

- 📖 [Complete Documentation](README.md)
- 🛠️ [Detailed Setup Guide](docs/SETUP.md)
- 🔧 [Troubleshooting](docs/TROUBLESHOOTING.md)
- 📚 [API Reference](docs/API.md)
- 🏗️ [Architecture](docs/ARCHITECTURE.md)

## Available Tools

✉️ **Email**: send, read, search, delete, move (6 tools)
📅 **Calendar**: create, update, respond to meetings (5 tools)
👤 **Contacts**: CRUD operations (5 tools)
✅ **Tasks**: create, complete, manage (5 tools)

**Total**: 21 tools for complete Exchange automation

## Configuration Templates

Three pre-configured templates included:

1. `.env.basic.example` - Basic Authentication
2. `.env.oauth2.example` - OAuth2 (Office 365)
3. `.env.example` - General template

Choose the one that matches your setup!

## Need Help?

- 💬 [GitHub Issues](https://github.com/azizmazrou/ews-mcp/issues)
- 📖 [Troubleshooting Guide](docs/TROUBLESHOOTING.md)
- 🔍 [FAQs](docs/SETUP.md)

---

**Get started in under a minute with Basic Auth!** 🚀
