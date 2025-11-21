#!/bin/bash

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== EWS MCP + MCPO + Open WebUI Setup ===${NC}\n"

# Check if .env file exists
if [ ! -f .env ]; then
    echo -e "${RED}Error: .env file not found!${NC}"
    echo "Please create a .env file with your Exchange credentials:"
    echo ""
    echo "EWS_EMAIL=your-email@company.com"
    echo "EWS_PASSWORD=your-password"
    echo "EWS_SERVER_URL=https://your-exchange-server/EWS/Exchange.asmx"
    echo "EWS_AUTH_TYPE=basic"
    echo "MCPO_API_KEY=your-secret-key"
    echo ""
    exit 1
fi

# Generate MCPO API key if not set
if ! grep -q "MCPO_API_KEY" .env; then
    echo -e "${YELLOW}Generating MCPO API key...${NC}"
    MCPO_KEY=$(openssl rand -hex 32)
    echo "MCPO_API_KEY=${MCPO_KEY}" >> .env
    echo -e "${GREEN}✓ Generated MCPO API key${NC}\n"
fi

# Build and start services
echo -e "${YELLOW}Building and starting services...${NC}"
docker-compose -f docker-compose.openwebui.yml up --build -d

echo ""
echo -e "${GREEN}=== Services Started ===${NC}\n"

# Wait for services to be healthy
echo "Waiting for services to start..."
sleep 5

# Check EWS MCP
if curl -s http://localhost:8001/sse > /dev/null 2>&1; then
    echo -e "${GREEN}✓ EWS MCP Server: http://localhost:8001${NC}"
else
    echo -e "${RED}✗ EWS MCP Server: Failed to start${NC}"
fi

# Check MCPO
if curl -s http://localhost:9000/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓ MCPO Proxy: http://localhost:9000${NC}"
else
    echo -e "${RED}✗ MCPO Proxy: Failed to start${NC}"
fi

# Show available tools
echo ""
echo -e "${YELLOW}Fetching available tools...${NC}"
if curl -s http://localhost:9000/openapi.json | jq -r '.paths | keys[]' 2>/dev/null; then
    echo -e "${GREEN}✓ MCPO is serving tools${NC}"
else
    echo -e "${YELLOW}Note: Install 'jq' to see available tools${NC}"
fi

# Instructions
echo ""
echo -e "${GREEN}=== Next Steps ===${NC}"
echo ""
echo "1. Configure Open WebUI:"
echo "   - Navigate to Admin Settings → Connections"
echo "   - Add External API:"
echo "     • API Base URL: http://localhost:9000"
echo "     • API Key: $(grep MCPO_API_KEY .env | cut -d= -f2)"
echo "     • Name: Exchange Web Services"
echo ""
echo "2. View logs:"
echo "   docker-compose -f docker-compose.openwebui.yml logs -f"
echo ""
echo "3. Stop services:"
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
