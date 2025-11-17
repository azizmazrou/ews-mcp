#!/bin/bash
set -e

echo "Building EWS MCP Server..."

# Get version from setup.py
VERSION=$(grep "version=" setup.py | cut -d'"' -f2)

# Build Docker image
echo "Building Docker image..."
docker build -t ews-mcp-server:latest .

# Tag with version
echo "Tagging image with version $VERSION..."
docker tag ews-mcp-server:latest ews-mcp-server:$VERSION

echo "✓ Build complete!"
echo "  - ews-mcp-server:latest"
echo "  - ews-mcp-server:$VERSION"
