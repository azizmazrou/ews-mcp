#!/bin/bash
# Docker Rebuild Script - Fix FolderId Import Error
# Run this script to rebuild your Docker container with the fixed code

set -e

echo "🔧 EWS MCP Server - Docker Rebuild Script"
echo "=========================================="
echo ""

# Step 1: Stop and remove old container
echo "Step 1: Stopping old container..."
docker stop ews-mcp-server 2>/dev/null || echo "Container not running"
docker rm ews-mcp-server 2>/dev/null || echo "Container not found"
echo "✓ Old container removed"
echo ""

# Step 2: Remove old image to force fresh build
echo "Step 2: Removing old image..."
docker rmi ews-mcp-server 2>/dev/null || echo "Image not found"
echo "✓ Old image removed"
echo ""

# Step 3: Build new image with no cache
echo "Step 3: Building new image (this may take a few minutes)..."
docker build --no-cache -t ews-mcp-server .
echo "✓ New image built"
echo ""

# Step 4: Start new container
echo "Step 4: Starting new container..."
docker run -d \
  --name ews-mcp-server \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  ews-mcp-server

echo "✓ Container started"
echo ""

# Step 5: Wait a moment for startup
echo "Step 5: Waiting for server to start..."
sleep 3
echo ""

# Step 6: Show logs
echo "Step 6: Checking logs..."
echo "=========================================="
docker logs ews-mcp-server
echo "=========================================="
echo ""

# Check if successful
if docker logs ews-mcp-server 2>&1 | grep -q "Successfully connected to Exchange"; then
    echo "✅ SUCCESS! Server started successfully"
    echo ""
    echo "You can now test custom folder access:"
    echo "  read_emails(folder=\"CC\", max_results=10)"
    echo "  read_emails(folder=\"Inbox/CC\", max_results=10)"
else
    echo "⚠️  Warning: Check logs above for errors"
    echo ""
    echo "To view live logs, run:"
    echo "  docker logs -f ews-mcp-server"
fi
