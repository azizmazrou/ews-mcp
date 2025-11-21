#!/bin/bash

# EWS MCP + Open WebUI Setup Script
# This script starts the unified EWS MCP server with built-in OpenAPI/REST support
# No MCPO or external adapters required!

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║       EWS MCP Server with OpenAPI/REST Support Setup         ║${NC}"
echo -e "${BLUE}║         Unified Architecture - No MCPO Required               ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════╝${NC}\n"

# Check if .env file exists
if [ ! -f .env ]; then
    echo -e "${RED}Error: .env file not found!${NC}"
    echo ""
    echo "Please create a .env file with your Exchange credentials:"
    echo ""
    echo -e "${YELLOW}# Exchange Server Configuration${NC}"
    echo "EWS_EMAIL=your-email@company.com"
    echo "EWS_PASSWORD=your-password"
    echo "EWS_SERVER_URL=https://your-exchange-server/EWS/Exchange.asmx"
    echo "EWS_AUTH_TYPE=basic"
    echo ""
    echo -e "${YELLOW}# Optional: Timezone Configuration${NC}"
    echo "TIMEZONE=Asia/Riyadh"
    echo ""
    echo -e "${YELLOW}# Optional: Logging${NC}"
    echo "LOG_LEVEL=INFO"
    echo ""
    exit 1
fi

# Check if docker is running
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}Error: Docker is not running!${NC}"
    echo "Please start Docker and try again."
    exit 1
fi

# Build and start services
echo -e "${YELLOW}Building and starting EWS MCP Server...${NC}"
echo ""
docker-compose -f docker-compose.openwebui.yml up --build -d

if [ $? -ne 0 ]; then
    echo -e "${RED}Error: Failed to start services${NC}"
    echo "Check docker-compose logs for details:"
    echo "  docker-compose -f docker-compose.openwebui.yml logs"
    exit 1
fi

echo ""
echo -e "${GREEN}✓ Docker containers started${NC}\n"

# Wait for services to be healthy
echo -e "${YELLOW}Waiting for services to initialize...${NC}"
MAX_RETRIES=30
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -s http://localhost:8000/health > /dev/null 2>&1; then
        echo -e "${GREEN}✓ EWS MCP Server is healthy${NC}\n"
        break
    fi

    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
        echo -e "${RED}✗ Timeout waiting for server to start${NC}"
        echo "Check logs: docker-compose -f docker-compose.openwebui.yml logs ews-mcp"
        exit 1
    fi

    echo -n "."
    sleep 2
done

# Display server status and endpoints
echo -e "${BLUE}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                      Server Status                            ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════╝${NC}\n"

# Get health status
HEALTH_RESPONSE=$(curl -s http://localhost:8000/health)
TOOL_COUNT=$(echo $HEALTH_RESPONSE | grep -o '"tools":[0-9]*' | grep -o '[0-9]*')

echo -e "${GREEN}✓ EWS MCP Server Running${NC}"
echo -e "  Base URL: ${BLUE}http://localhost:8000${NC}"
echo -e "  Tools Available: ${YELLOW}${TOOL_COUNT:-44}${NC}"
echo ""

echo -e "${GREEN}Available Endpoints:${NC}"
echo -e "  ${BLUE}GET${NC}  /health              - Health check"
echo -e "  ${BLUE}GET${NC}  /sse                 - MCP SSE transport (for Claude Desktop)"
echo -e "  ${BLUE}POST${NC} /messages            - MCP messages endpoint"
echo -e "  ${BLUE}GET${NC}  /openapi.json        - OpenAPI 3.0 schema"
echo -e "  ${BLUE}POST${NC} /api/tools/{tool}    - REST API tool execution"
echo ""

# Show available tools (if jq is installed)
if command -v jq &> /dev/null; then
    echo -e "${BLUE}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║                     Available Tool Categories                 ║${NC}"
    echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════╝${NC}\n"

    OPENAPI_SCHEMA=$(curl -s http://localhost:8000/openapi.json)

    echo -e "${GREEN}Email Tools:${NC}"
    echo "$OPENAPI_SCHEMA" | jq -r '.tags[] | select(.name == "Email") | .description'

    echo -e "\n${GREEN}Calendar Tools:${NC}"
    echo "$OPENAPI_SCHEMA" | jq -r '.tags[] | select(.name == "Calendar") | .description'

    echo -e "\n${GREEN}Contact Tools:${NC}"
    echo "$OPENAPI_SCHEMA" | jq -r '.tags[] | select(.name == "Contacts") | .description'

    echo -e "\n${GREEN}Task Tools:${NC}"
    echo "$OPENAPI_SCHEMA" | jq -r '.tags[] | select(.name == "Tasks") | .description'

    echo -e "\n${GREEN}Other Categories:${NC}"
    echo "  • Attachments, Search, Folders, Out-of-Office"
    echo ""
else
    echo -e "${YELLOW}Tip: Install 'jq' to see detailed tool information${NC}\n"
fi

# Display examples
echo -e "${BLUE}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                       Quick Start Examples                     ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════╝${NC}\n"

echo -e "${GREEN}1. View OpenAPI Schema:${NC}"
echo -e "   ${YELLOW}curl http://localhost:8000/openapi.json | jq${NC}"
echo ""

echo -e "${GREEN}2. Test REST API - Read Emails:${NC}"
echo -e "   ${YELLOW}curl -X POST http://localhost:8000/api/tools/read_emails \\${NC}"
echo -e "   ${YELLOW}     -H 'Content-Type: application/json' \\${NC}"
echo -e "   ${YELLOW}     -d '{\"max_results\": 5}' | jq${NC}"
echo ""

echo -e "${GREEN}3. Get Calendar Events:${NC}"
echo -e "   ${YELLOW}curl -X POST http://localhost:8000/api/tools/get_calendar \\${NC}"
echo -e "   ${YELLOW}     -H 'Content-Type: application/json' \\${NC}"
echo -e "   ${YELLOW}     -d '{\"days\": 7}' | jq${NC}"
echo ""

echo -e "${GREEN}4. Search Contacts:${NC}"
echo -e "   ${YELLOW}curl -X POST http://localhost:8000/api/tools/search_contacts \\${NC}"
echo -e "   ${YELLOW}     -H 'Content-Type: application/json' \\${NC}"
echo -e "   ${YELLOW}     -d '{\"search_term\": \"John\"}' | jq${NC}"
echo ""

# Open WebUI integration instructions
echo -e "${BLUE}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                  Open WebUI Integration                       ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════╝${NC}\n"

echo -e "${GREEN}To connect Open WebUI to EWS MCP:${NC}"
echo ""
echo "1. Open Open WebUI in your browser"
echo "2. Navigate to: ${YELLOW}Admin Settings → Functions → External APIs${NC}"
echo "3. Click ${YELLOW}+ Add External API${NC}"
echo "4. Configure:"
echo "   • ${GREEN}API Base URL:${NC} http://ews-mcp:8000 (or http://localhost:8000)"
echo "   • ${GREEN}Name:${NC} Exchange Web Services"
echo "   • ${GREEN}Description:${NC} Access to Exchange emails, calendar, contacts"
echo "5. Click ${YELLOW}Save${NC}"
echo "6. The OpenAPI schema will be auto-discovered from /openapi.json"
echo "7. All 44 tools will be available in your chats!"
echo ""

# Management commands
echo -e "${BLUE}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                    Management Commands                        ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════╝${NC}\n"

echo -e "${GREEN}View Logs:${NC}"
echo -e "  ${YELLOW}docker-compose -f docker-compose.openwebui.yml logs -f ews-mcp${NC}"
echo ""

echo -e "${GREEN}Restart Server:${NC}"
echo -e "  ${YELLOW}docker-compose -f docker-compose.openwebui.yml restart ews-mcp${NC}"
echo ""

echo -e "${GREEN}Stop All Services:${NC}"
echo -e "  ${YELLOW}docker-compose -f docker-compose.openwebui.yml down${NC}"
echo ""

echo -e "${GREEN}Rebuild After Code Changes:${NC}"
echo -e "  ${YELLOW}docker-compose -f docker-compose.openwebui.yml up --build -d${NC}"
echo ""

# Architecture info
echo -e "${BLUE}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                        Architecture                           ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════╝${NC}\n"

echo -e "${GREEN}Unified Server Architecture:${NC}"
echo "  • Single server on port 8000"
echo "  • Built-in OpenAPI/REST adapter"
echo "  • No MCPO or external proxies needed"
echo "  • Supports both MCP SSE and REST protocols"
echo "  • Auto-generates OpenAPI 3.0 schema from MCP tools"
echo ""

echo -e "${GREEN}Documentation:${NC}"
echo "  • See OPENWEBUI_SETUP.md for detailed setup guide"
echo "  • See README.md for general MCP usage"
echo ""

echo -e "${GREEN}✓ Setup complete! Server is ready to use.${NC}\n"
