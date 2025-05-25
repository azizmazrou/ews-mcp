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
