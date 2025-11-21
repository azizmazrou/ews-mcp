#!/bin/bash

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== EWS MCP + Open WebUI Setup ===${NC}\n"

# Check if .env file exists
if [ ! -f .env ]; then
    echo -e "${RED}Error: .env file not found!${NC}"
    echo "Please create a .env file with your Exchange credentials:"
    echo ""
    echo "EWS_EMAIL=your-email@company.com"
    echo "EWS_PASSWORD=your-password"
    echo "EWS_SERVER_URL=https://your-exchange-server/EWS/Exchange.asmx"
    echo "EWS_AUTH_TYPE=basic"
    echo ""
    exit 1
fi

# Build and start services
echo -e "${YELLOW}Building and starting services...${NC}"
docker-compose -f docker-compose.openwebui.yml up --build -d

echo ""
echo -e "${GREEN}=== Services Started ===${NC}\n"

# Wait for services to be healthy
echo "Waiting for services to start..."
sleep 5

# Check EWS MCP Server
if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓ EWS MCP Server: http://localhost:8000${NC}"
    echo -e "${GREEN}  • MCP SSE: http://localhost:8000/sse${NC}"
    echo -e "${GREEN}  • REST API: http://localhost:8000/api/tools/{tool_name}${NC}"
    echo -e "${GREEN}  • OpenAPI: http://localhost:8000/openapi.json${NC}"
else
    echo -e "${RED}✗ EWS MCP Server: Failed to start${NC}"
fi

# Show available tools
echo ""
echo -e "${YELLOW}Fetching available tools...${NC}"
if curl -s http://localhost:8000/openapi.json | jq -r '.paths | keys[]' 2>/dev/null; then
    echo -e "${GREEN}✓ Server is serving tools${NC}"
else
    echo -e "${YELLOW}Note: Install 'jq' to see available tools${NC}"
fi

# Instructions
echo ""
echo -e "${GREEN}=== Next Steps ===${NC}"
echo ""
echo "1. Test REST API:"
echo "   curl http://localhost:8000/openapi.json"
echo "   curl -X POST http://localhost:8000/api/tools/read_emails \\"
echo "        -H 'Content-Type: application/json' \\"
echo "        -d '{\"max_results\": 5}'"
echo ""
echo "2. Configure Open WebUI:"
echo "   - Navigate to Admin Settings → Connections"
echo "   - Add External API:"
echo "     • API Base URL: http://localhost:8000"
echo "     • Name: Exchange Web Services"
echo "   - OpenAPI schema will be auto-discovered"
echo ""
echo "3. View logs:"
echo "   docker-compose -f docker-compose.openwebui.yml logs -f"
echo ""
echo "4. Stop services:"
echo "   docker-compose -f docker-compose.openwebui.yml down"
echo ""
echo -e "${GREEN}=== Available Tools ===${NC}"
echo "44 Exchange tools available for:"
echo "  • Email (8 tools)"
echo "  • Calendar (7 tools)"
echo "  • Contacts (9 tools)"
echo "  • Tasks (5 tools)"
echo "  • Attachments (5 tools)"
echo "  • Search (3 tools)"
echo "  • Folders (5 tools)"
echo "  • Out-of-Office (2 tools)"
echo ""
