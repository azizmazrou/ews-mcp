# Setup Guide

Step-by-step guide to set up EWS MCP Server.

## Prerequisites

### Required
- Python 3.11 or higher
- Microsoft Exchange account (Office 365 or on-premises)
- Exchange Web Services (EWS) enabled

### Optional (for Docker)
- Docker 20.10+
- Docker Compose 2.0+

### Optional (for OAuth2)
- Azure AD tenant with admin access
- Permissions to register applications

## Installation Methods

### Method 1: Docker (Recommended for Production)

#### Step 1: Clone Repository

```bash
git clone <repository-url>
cd ews-mcp-server
```

#### Step 2: Configure Environment

```bash
# Copy example environment file
cp .env.example .env

# Edit .env with your credentials
nano .env  # or use your preferred editor
```

See [Configuration](#configuration) section for details.

#### Step 3: Build and Run

```bash
# Build Docker image
docker build -t ews-mcp-server .

# Run with docker-compose (recommended)
docker-compose up -d

# Or run directly
docker run -d --name ews-mcp-server --env-file .env ews-mcp-server

# View logs
docker-compose logs -f
```

### Method 2: Local Python (Recommended for Development)

#### Step 1: Clone Repository

```bash
git clone <repository-url>
cd ews-mcp-server
```

#### Step 2: Run Setup Script

```bash
# Make script executable (Linux/Mac)
chmod +x scripts/setup.sh

# Run setup
./scripts/setup.sh
```

Or manually:

```bash
# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# For development
pip install -r requirements-dev.txt
```

#### Step 3: Configure Environment

```bash
# Copy example environment file
cp .env.example .env

# Edit with your credentials
nano .env
```

#### Step 4: Run Server

```bash
# Activate virtual environment if not already active
source venv/bin/activate

# Run server
python -m src.main
```

## Configuration

### OAuth2 Setup (Recommended for Office 365)

#### Step 1: Register Application in Azure AD

1. Go to [Azure Portal](https://portal.azure.com)
2. Navigate to **Azure Active Directory** → **App registrations**
3. Click **New registration**
   - Name: `EWS MCP Server`
   - Supported account types: **Accounts in this organizational directory only**
   - Redirect URI: Leave blank
4. Click **Register**

#### Step 2: Configure API Permissions

1. Go to **API permissions**
2. Click **Add a permission**
3. Select **APIs my organization uses**
4. Search for **Office 365 Exchange Online**
5. Select **Application permissions**
6. Add these permissions:
   - `full_access_as_app` (for full access)

   Or specific permissions:
   - `Mail.ReadWrite`
   - `Mail.Send`
   - `Calendars.ReadWrite`
   - `Contacts.ReadWrite`
   - `Tasks.ReadWrite`

7. Click **Add permissions**
8. Click **Grant admin consent for [Your Organization]**
9. Confirm by clicking **Yes**

#### Step 3: Create Client Secret

1. Go to **Certificates & secrets**
2. Click **New client secret**
3. Description: `EWS MCP Server Secret`
4. Expires: Select duration (recommend: 24 months)
5. Click **Add**
6. **Copy the secret VALUE immediately** (you won't see it again!)

#### Step 4: Get Required IDs

From the **Overview** page, copy:
- **Application (client) ID**
- **Directory (tenant) ID**

#### Step 5: Configure .env

Update your `.env` file:

```bash
# Exchange Server
EWS_SERVER_URL=https://outlook.office365.com/EWS/Exchange.asmx
EWS_EMAIL=your.email@company.com
EWS_AUTODISCOVER=true

# OAuth2 Authentication
EWS_AUTH_TYPE=oauth2
EWS_CLIENT_ID=<your-client-id>
EWS_CLIENT_SECRET=<your-client-secret>
EWS_TENANT_ID=<your-tenant-id>
```

### Basic Authentication Setup (On-Premises Exchange)

Update your `.env` file:

```bash
# Exchange Server
EWS_SERVER_URL=https://mail.company.com/EWS/Exchange.asmx
EWS_EMAIL=user@company.com
EWS_AUTODISCOVER=false

# Basic Authentication
EWS_AUTH_TYPE=basic
EWS_USERNAME=user@company.com
EWS_PASSWORD=your-password
```

**Note:** Basic authentication is being deprecated by Microsoft. Use OAuth2 when possible.

### NTLM Authentication Setup

Update your `.env` file:

```bash
# Exchange Server
EWS_SERVER_URL=https://mail.company.com/EWS/Exchange.asmx
EWS_EMAIL=DOMAIN\username
EWS_AUTODISCOVER=false

# NTLM Authentication
EWS_AUTH_TYPE=ntlm
EWS_USERNAME=DOMAIN\username
EWS_PASSWORD=your-password
```

## Claude Desktop Integration

### Step 1: Locate Configuration File

**macOS:**
```bash
~/Library/Application Support/Claude/claude_desktop_config.json
```

**Windows:**
```
%APPDATA%\Claude\claude_desktop_config.json
```

**Linux:**
```bash
~/.config/Claude/claude_desktop_config.json
```

### Step 2: Add MCP Server Configuration

#### For Docker Deployment

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
        "/absolute/path/to/ews-mcp/.env",
        "ews-mcp-server"
      ]
    }
  }
}
```

**Important:** Replace `/absolute/path/to/ews-mcp/.env` with the actual absolute path.

#### For Local Python Deployment

```json
{
  "mcpServers": {
    "ews": {
      "command": "python",
      "args": ["-m", "src.main"],
      "cwd": "/absolute/path/to/ews-mcp",
      "env": {
        "EWS_SERVER_URL": "https://outlook.office365.com/EWS/Exchange.asmx",
        "EWS_EMAIL": "your.email@company.com",
        "EWS_AUTH_TYPE": "oauth2",
        "EWS_CLIENT_ID": "your-client-id",
        "EWS_CLIENT_SECRET": "your-secret",
        "EWS_TENANT_ID": "your-tenant",
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Step 3: Restart Claude Desktop

Completely quit and restart Claude Desktop application.

### Step 4: Verify Integration

In Claude Desktop, type:
```
Can you check my unread emails?
```

If working, Claude will use the EWS MCP Server to fetch your emails.

## Verification

### Test Connection

```bash
# From Python
python -c "
from src.config import settings
from src.auth import AuthHandler
from src.ews_client import EWSClient

auth = AuthHandler(settings)
client = EWSClient(settings, auth)

if client.test_connection():
    print('✓ Connection successful!')
    print(f'Inbox count: {client.account.inbox.total_count}')
else:
    print('✗ Connection failed')
"
```

### Run Tests

```bash
# Activate virtual environment
source venv/bin/activate

# Run tests
pytest

# Run with coverage
pytest --cov=src
```

### Check Logs

```bash
# Docker
docker-compose logs -f

# Local
tail -f logs/ews-mcp.log
```

## Troubleshooting Setup

### Problem: "Module not found" errors

**Solution:**
```bash
# Ensure virtual environment is activated
source venv/bin/activate

# Reinstall dependencies
pip install -r requirements.txt
```

### Problem: "Authentication failed"

**Solutions:**
1. Verify all credentials are correct
2. Check OAuth2 app permissions
3. Ensure admin consent granted
4. Try basic auth for testing

### Problem: "Cannot connect to Exchange"

**Solutions:**
1. Verify EWS_SERVER_URL is correct
2. Check network connectivity
3. Try autodiscovery: `EWS_AUTODISCOVER=true`
4. Test with curl:
   ```bash
   curl https://outlook.office365.com/EWS/Exchange.asmx
   ```

### Problem: Claude Desktop doesn't see the server

**Solutions:**
1. Verify configuration file location
2. Check JSON syntax (use a JSON validator)
3. Ensure paths are absolute, not relative
4. Restart Claude Desktop completely
5. Check Docker container is running:
   ```bash
   docker ps | grep ews-mcp
   ```

## Optional Configuration

### Enable Features

```bash
# In .env file

# Disable specific features
ENABLE_EMAIL=true
ENABLE_CALENDAR=true
ENABLE_CONTACTS=true
ENABLE_TASKS=true
```

### Performance Tuning

```bash
# Connection settings
CONNECTION_POOL_SIZE=10
REQUEST_TIMEOUT=30

# Caching (future feature)
ENABLE_CACHE=true
CACHE_TTL=300
```

### Rate Limiting

```bash
RATE_LIMIT_ENABLED=true
RATE_LIMIT_REQUESTS_PER_MINUTE=25
```

### Logging

```bash
# Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_LEVEL=INFO

# Audit logging
ENABLE_AUDIT_LOG=true
```

## Next Steps

1. Review [API Documentation](API.md) for available tools
2. Check [Usage Examples](../examples/usage_examples.md) for common patterns
3. Read [Troubleshooting Guide](TROUBLESHOOTING.md) for common issues
4. See [Deployment Guide](DEPLOYMENT.md) for production deployment

## Security Best Practices

1. **Never commit .env file** - It's in .gitignore
2. **Rotate secrets regularly** - Especially OAuth2 client secrets
3. **Use OAuth2 when possible** - More secure than basic auth
4. **Limit permissions** - Only grant needed permissions
5. **Monitor access logs** - Enable audit logging
6. **Use HTTPS only** - Never use HTTP for Exchange
7. **Secure the host** - Keep OS and dependencies updated

## Support

For issues during setup:
1. Check [Troubleshooting Guide](TROUBLESHOOTING.md)
2. Enable DEBUG logging: `LOG_LEVEL=DEBUG`
3. Review logs for detailed errors
4. Create GitHub issue with:
   - Steps to reproduce
   - Error messages (redact credentials)
   - Environment details
