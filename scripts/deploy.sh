#!/bin/bash
set -e

echo "Deploying EWS MCP Server..."

# Check if .env exists
if [ ! -f .env ]; then
    echo "Error: .env file not found"
    echo "Please create .env from .env.example"
    exit 1
fi

# Build image
echo "Building Docker image..."
./scripts/build.sh

# Deploy with docker-compose
echo "Deploying with docker-compose..."
docker-compose down
docker-compose up -d

# Wait for startup
echo "Waiting for server to start..."
sleep 5

# Check status
echo "Checking server status..."
docker-compose ps

# Show logs
echo ""
echo "Server deployed! Viewing logs (Ctrl+C to exit)..."
docker-compose logs -f
