# EWS MCP Server

Basic Node.js server exposing Exchange Web Services (EWS) operations through the Model Context Protocol (MCP).

## Development

```bash
npm install
npm run dev
```

## Building

```bash
npm run build
```

## Docker

```bash
docker compose -f docker/docker-compose.yml up --build
```

Exposes health endpoint at `/health` and metrics at `/metrics`.

## MCP Client Configuration

Any MCP compatible client can connect via WebSocket to this server. The
default WebSocket endpoint is `ws://localhost:${MCP_PORT}/mcp` (port 3000 in
dev). The example below uses the `@anthropic/mcp` client to invoke the
`search_emails` tool:

```ts
import { createWebSocketClient } from '@anthropic/mcp';

const client = createWebSocketClient({
  url: 'ws://localhost:3000/mcp',
});

const result = await client.invoke('search_emails', { query: 'invoices' });
console.log(result);
```

To use the official GitHub MCP server, simply point the client `url` to the
GitHub server's `/mcp` endpoint instead of the local URL.
