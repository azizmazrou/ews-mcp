#!/bin/bash
set -e

# Enterprise-level entrypoint: minimal output, detailed logging in files
# All troubleshooting info goes to application logs

# Setup log directories silently
mkdir -p /app/logs/analysis 2>/dev/null || true
chown -R mcp:mcp /app/logs 2>/dev/null || true
chmod -R 755 /app/logs 2>/dev/null || true

# Single startup message for monitoring
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] EWS-MCP v3.0 starting"

# Switch to non-root user and start application
# All runtime logs go to /app/logs via Python logging
exec gosu mcp "$@"
