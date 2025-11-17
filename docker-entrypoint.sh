#!/bin/bash
set -e

# Ensure log directories exist with proper permissions
echo "Setting up log directories..."

# Create main logs directory if it doesn't exist
if [ ! -d "/app/logs" ]; then
    echo "Creating /app/logs directory..."
    mkdir -p /app/logs
fi

# Create analysis subdirectory if it doesn't exist
if [ ! -d "/app/logs/analysis" ]; then
    echo "Creating /app/logs/analysis directory..."
    mkdir -p /app/logs/analysis
fi

# Set ownership to mcp user (UID 1000, GID 1000)
echo "Setting ownership on /app/logs to mcp:mcp..."
chown -R mcp:mcp /app/logs 2>/dev/null || echo "Warning: Could not change ownership (may not be running as root)"

# Ensure proper permissions
echo "Setting permissions on /app/logs..."
chmod -R 755 /app/logs 2>/dev/null || echo "Warning: Could not set permissions"

echo "Log directories ready."
echo "Starting EWS MCP Server as user 'mcp'..."

# Switch to mcp user and execute the main command
exec gosu mcp "$@"
