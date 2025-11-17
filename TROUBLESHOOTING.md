# Troubleshooting Guide

## Permission Denied: /app/logs/analysis

### Problem
You may encounter the following error when running the EWS MCP Server in Docker:
```
Fatal error: [Errno 13] Permission denied: '/app/logs/analysis'
```

### Root Cause
This error occurs when:
1. The `docker-compose.yml` mounts `./logs:/app/logs` as a volume
2. The host `./logs` directory doesn't exist or lacks the `analysis` subdirectory
3. The Docker container (running as user `mcp`) cannot create the directory

### Solution
The server now includes multiple fixes to handle this gracefully:

1. **Graceful Fallback**: The logging system will now fall back to `/tmp/ews_mcp_logs` if it cannot write to `/app/logs`

2. **Permission Handling**: The entrypoint script (`docker-entrypoint.sh`) runs as root to:
   - Create log directories in mounted volumes
   - Set proper ownership to `mcp:mcp` (UID/GID 1000)
   - Switch to the non-root `mcp` user using `gosu` before starting the application

3. **Host Directory Creation**: The `logs/analysis` directory structure is now tracked in git with `.gitkeep` files

### Manual Fix (if needed)
If you encounter this issue with an existing setup:

```bash
# Create the logs directory structure on the host
mkdir -p logs/analysis

# Rebuild and restart the container
docker compose down
docker compose build
docker compose up -d
```

### Verification
Check the logs to verify the server started successfully:
```bash
docker compose logs -f ews-mcp-server
```

You should see:
- "Setting up log directories..."
- "Log directories ready."
- "Starting EWS MCP Server..."
- No permission denied errors

## Other Common Issues

### Connection Failed
If you see "Failed to connect to Exchange server":
1. Verify your credentials in `.env`
2. Check `EWS_SERVER_URL` is correct (or empty for autodiscover)
3. Ensure network connectivity to Exchange server
4. Check firewall rules

### Rate Limiting
If operations are being rate limited:
- Adjust `RATE_LIMIT_REQUESTS_PER_MINUTE` in `.env`
- Or disable rate limiting with `RATE_LIMIT_ENABLED=false`

### SSL/TLS Errors
For self-signed certificates:
- Set appropriate SSL verification settings in your Exchange configuration
- Consult `exchangelib` documentation for certificate handling
