#!/bin/bash
# Force rebuild Docker container with latest code
# This script removes ALL caches and rebuilds from scratch

set -e

echo "🔧 Force rebuilding Docker container with latest code..."
echo ""

# Stop and remove everything
echo "1. Stopping and removing container..."
docker-compose down -v 2>/dev/null || true

# Remove ALL Docker build cache
echo "2. Removing Docker build cache..."
docker builder prune -f

# Remove the specific image
echo "3. Removing old image..."
docker rmi ews-mcp-ews-mcp-server 2>/dev/null || true
docker rmi ews-mcp-server 2>/dev/null || true

# Build with no cache
echo "4. Building with --no-cache..."
docker-compose build --no-cache --progress=plain

# Start container
echo "5. Starting container..."
docker-compose up -d

# Wait for startup
echo "6. Waiting for startup..."
sleep 5

# Show logs
echo "7. Container logs:"
echo "=========================================="
docker-compose logs --tail 50
echo "=========================================="

echo ""
echo "✅ Rebuild complete! Test with find_person again."
