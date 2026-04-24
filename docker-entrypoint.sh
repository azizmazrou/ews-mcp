#!/bin/bash
set -e

# Enterprise-level entrypoint: minimal output, detailed logging in files
# All troubleshooting info goes to application logs

# Setup log directories silently
mkdir -p /app/logs/analysis 2>/dev/null || true
chown -R mcp:mcp /app/logs 2>/dev/null || true
chmod -R 755 /app/logs 2>/dev/null || true

# Single startup message for monitoring.
# Goes to stderr: MCP stdio transport reserves stdout for JSON-RPC, and any
# pre-handshake byte on stdout breaks strict clients (e.g. Claude Desktop).
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] EWS-MCP v3.4 starting" >&2

# Switch to non-root user and start application
# All runtime logs go to /app/logs via Python logging
exec gosu mcp "$@"
