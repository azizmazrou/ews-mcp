# EWS MCP Server Architecture

This document captures the refined design after applying feedback around security, operational concerns and technical nudges. The focus for the initial milestones (M01 & M02) is on a robust core with Basic authentication, the OAuth App flow, reliable retries and basic scaling through stateless processes.

## Principles

- **Separation of concerns** via clear module boundaries
- **Testability** with unit and integration tests plus SOAP snapshots
- **Scalability** using Node's event loop, Redis for shared state and an optional worker pool
- **Security by design** including sanitisation, secret management and audit logging
- **Resilience** through retries, exponential backoff and graceful degradation
- **Observability** with structured logs, OpenTelemetry traces and health endpoints

## Project Structure Overview

```
ews-mcp-server/
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── src/
│   ├── main.ts
│   ├── server.ts
│   ├── config/
│   ├── ews/
│   ├── tools/
│   ├── mcp/
│   ├── security/
│   └── shared/
├── tests/
└── docs/
```

The `src/ews` folder houses the SOAP builder, parser and typed client with retry logic. Authentication strategies are pluggable (Basic or OAuth App for M01/M02). `src/tools` contains versioned MCP tools starting with email operations. `src/security` offers request auth, secret providers and an audit service.

## Key Technical Nudges

- **OpenTelemetry integration** — traces are exported via OTLP and the logger attaches trace/span IDs.
- **Worker pool** — heavy XML parsing can be offloaded to a Piscina worker.
- **Config validation** — loaded from layered files and environment variables then validated with Joi.
- **Backoff & retry** — the EWS client retries on `ErrorServerBusy` and relevant HTTP codes using exponential backoff with jitter.

## Security & Compliance

- **Secret providers** allow credentials to come from environment variables or external vaults.
- **Audit service** writes immutable logs of tool execution with sensitive data redacted.
- **Sanitiser middleware** and PII‑aware logging help keep logs clean.
- **RBAC hooks** are planned for future milestones.

## Operational Drill‑downs

- **docker-compose** includes Redis but encourages a Sentinel or managed Redis setup for production.
- **scripts/deploy.sh** tags images with the git SHA for traceable deployments.
- **Health checks** call out to Redis and perform a lightweight EWS request to verify connectivity.
- **Observability endpoints** expose `/metrics` for Prometheus and structured JSON logs by default.

## Incremental Delivery Plan (extract)

1. **M01** – BasicAuth support, core email tool, retry logic, single-node runtime.
2. **M02** – OAuth App auth strategy, PM2 clustering, Redis Sentinel and a circuit breaker.
3. **M03** – Calendar and contacts tools, prediction cache and Grafana dashboards.
4. **M04** – RBAC, OAuth On‑Behalf‑Of, attachment streaming and helm chart packaging.

The project aims to deliver a minimal but production‑ready service early, growing capabilities in later milestones without breaking existing integrations.

