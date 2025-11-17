#!/bin/bash
set -e

echo "====================================="
echo "EWS MCP Server - Basic Auth Setup"
echo "====================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: Docker is not installed${NC}"
    echo "Please install Docker first: https://docs.docker.com/get-docker/"
    exit 1
fi

echo -e "${GREEN}✓${NC} Docker is installed"
echo ""

# Prompt for configuration
echo "Please provide your Exchange server details:"
echo ""

read -p "Exchange server URL (e.g., mail.company.com): " SERVER
if [ -z "$SERVER" ]; then
    echo -e "${RED}Error: Server URL is required${NC}"
    exit 1
fi

read -p "Your email address: " EMAIL
if [ -z "$EMAIL" ]; then
    echo -e "${RED}Error: Email is required${NC}"
    exit 1
fi

read -p "Username (press Enter to use email): " USERNAME
if [ -z "$USERNAME" ]; then
    USERNAME=$EMAIL
fi

read -sp "Password: " PASSWORD
echo ""
if [ -z "$PASSWORD" ]; then
    echo -e "${RED}Error: Password is required${NC}"
    exit 1
fi

# Construct EWS URL if not full URL provided
if [[ ! $SERVER == http* ]]; then
    EWS_URL="https://${SERVER}/EWS/Exchange.asmx"
else
    EWS_URL=$SERVER
fi

echo ""
echo "Creating .env file..."

# Create .env file
cat > .env <<EOF
# Exchange Server Configuration
EWS_SERVER_URL=${EWS_URL}
EWS_EMAIL=${EMAIL}
EWS_AUTODISCOVER=false

# Basic Authentication
EWS_AUTH_TYPE=basic
EWS_USERNAME=${USERNAME}
EWS_PASSWORD=${PASSWORD}

# Server Configuration
MCP_SERVER_NAME=ews-mcp-server
MCP_TRANSPORT=stdio
LOG_LEVEL=INFO

# Performance
ENABLE_CACHE=true
CACHE_TTL=300
CONNECTION_POOL_SIZE=10
REQUEST_TIMEOUT=30

# Rate Limiting
RATE_LIMIT_ENABLED=true
RATE_LIMIT_REQUESTS_PER_MINUTE=25

# Features
ENABLE_EMAIL=true
ENABLE_CALENDAR=true
ENABLE_CONTACTS=true
ENABLE_TASKS=true
ENABLE_FOLDERS=true
ENABLE_ATTACHMENTS=true

# Security
ENABLE_AUDIT_LOG=true
MAX_ATTACHMENT_SIZE=157286400
EOF

echo -e "${GREEN}✓${NC} Configuration file created: .env"
echo ""

# Pull Docker image
echo "Pulling Docker image from GitHub Container Registry..."
docker pull ghcr.io/azizmazrou/ews-mcp:latest

echo ""
echo -e "${GREEN}✓${NC} Docker image pulled successfully"
echo ""

# Ask if user wants to run now
read -p "Do you want to start the server now? (y/n): " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    echo "Starting EWS MCP Server..."

    # Stop and remove existing container if exists
    docker stop ews-mcp-server 2>/dev/null || true
    docker rm ews-mcp-server 2>/dev/null || true

    # Run container
    docker run -d \
        --name ews-mcp-server \
        --env-file .env \
        -v $(pwd)/logs:/app/logs \
        ghcr.io/azizmazrou/ews-mcp:latest

    echo ""
    echo -e "${GREEN}✓${NC} Server started successfully!"
    echo ""
    echo "Container name: ews-mcp-server"
    echo ""
    echo "Waiting for server to initialize..."
    sleep 3

    echo ""
    echo "Recent logs:"
    echo "-----------------------------------"
    docker logs --tail 20 ews-mcp-server
    echo "-----------------------------------"
    echo ""

    # Check if connection was successful
    if docker logs ews-mcp-server 2>&1 | grep -q "Successfully connected"; then
        echo -e "${GREEN}✓ Successfully connected to Exchange!${NC}"
    else
        echo -e "${YELLOW}⚠ Check logs for connection status${NC}"
    fi

    echo ""
    echo "Useful commands:"
    echo "  View logs:    docker logs -f ews-mcp-server"
    echo "  Stop server:  docker stop ews-mcp-server"
    echo "  Start server: docker start ews-mcp-server"
    echo "  Remove:       docker rm -f ews-mcp-server"
else
    echo ""
    echo "Setup complete! To start the server later, run:"
    echo "  docker run -d --name ews-mcp-server --env-file .env ghcr.io/azizmazrou/ews-mcp:latest"
fi

echo ""
echo "====================================="
echo "Setup Complete!"
echo "====================================="
echo ""
echo "Configuration file: .env"
echo "Docker image: ghcr.io/azizmazrou/ews-mcp:latest"
echo ""
echo "For Claude Desktop integration, see: docs/SETUP.md"
echo ""
