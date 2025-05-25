
# EWS-MCP Server 🚀

For a detailed architecture document see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

A production-ready **Model Context Protocol (MCP)** gateway for **Microsoft Exchange Web Services**.  
It exposes smart, versioned MCP tools (email, calendar …) over HTTP & WebSocket while handling EWS SOAP, OAuth, caching, retries and observability under the hood.

---

## ✨ Features

| Category | Highlights |
|----------|------------|
| **EWS Integration** | 84 SOAP ops via a typed client, Basic + OAuth App auth, back-off & retries |
| **MCP Tools** | `email_ops` v1 (search / read / send), pluggable tool registry, JSON schema validation |
| **Scalability** | Stateless Node 20, PM2 cluster + Piscina worker pool for heavy XML parsing |
| **Resilience** | Exponential back-off on `ErrorServerBusy`, circuit-breaker, Redis Sentinel HA |
| **Security** | JWT / API-key auth, per-tool RBAC hooks, secret providers (env / Vault / AWS SM) |
| **Observability** | OTEL traces + Prometheus metrics, Winston logs with PII redaction, `/health` |
| **CI-ready** | Snapshot tests for SOAP XML, mock-EWS integration tests, GitHub Actions stub |
| **Docker** | Multi-stage image, compose stack (app + Redis); helm chart forthcoming |

---

## 🗺 Architecture

```mermaid
graph TD
    A[Client (Claude / n8n / curl)] -->|HTTP/WS (MCP)| B(MCP Gateway)
    B --> C[Tool Executor]
    C --> D[EWS Client]
    C --> E[Intelligent Cache<br>(Redis)]
    D -->|SOAP| F[Exchange Web Services]
    C --> G[Audit Log]
    B --> H(OTEL Exporter)
    E <--> I[Redis Sentinel / Cluster]
```

---

## 📦 Project Structure (excerpt)

```
ews-mcp-server/
├─ docker/
│  ├─ Dockerfile
│  └─ docker-compose.yml
├─ src/
│  ├─ main.ts            # bootstrap + graceful shutdown
│  ├─ server.ts          # Express + WS endpoints
│  ├─ config/            # Joi-validated, typed config loader
│  ├─ shared/            # logger, errors, worker pool
│  ├─ security/          # auth, RBAC, audit, secrets
│  ├─ ews/               # SOAP builder, parser, client, auth strategies
│  ├─ tools/             # AbstractTool + email_ops v1
│  └─ mcp/               # MCP handler & middleware
└─ tests/                # unit, integration, snapshots
```

---

## ⚡ Quick Start (Docker)

```bash
git clone https://github.com/your-org/ews-mcp-server.git
cd ews-mcp-server
cp .env.example .env             # edit Exchange & Redis creds
docker compose   -f docker/docker-compose.yml   up --build
# → Health check
curl http://localhost:3000/health
```

---

## 🛠 Local Dev

```bash
# Requirements: Node 20+, pnpm or npm, Docker (for Redis)
pnpm i           # or npm ci
pnpm run build   # tsc compile
pnpm run test    # jest + snapshot tests
pnpm run dev     # ts-node with live reload
```

---

## 🔑 Configuration (.env)

```dotenv
EWS_URL=https://mail.example.com/EWS/Exchange.asmx
EWS_AUTH_TYPE=OAuthApp          # Basic | OAuthApp
EWS_USERNAME=svc@example.com    # if Basic
EWS_PASSWORD=********           # if Basic
REDIS_URL=redis://localhost:6379/0
JWT_SECRET=change-me
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Full schema is documented in **`src/config/schema.ts`**.

---

## 🧪 Testing

| Command | What it does |
|---------|--------------|
| `pnpm run test` | Unit tests + SOAP snapshot comparison |
| `pnpm run test:int` | Spins up a mock-EWS docker image for integration tests |
| `npm run lint` | ESLint + prettier check |

CI template (`.github/workflows/ci.yml`) runs the same steps and pushes coverage to Codecov.

---

## 📈 Observability

* **/metrics** — Prometheus scrape endpoint  
* **OTEL traces** — export to Jaeger / Grafana Tempo via OTLP  
* **Logs** — JSON to stdout (PII-redacted), optional Loki driver in `docker-compose.yml`

Dashboards and alert examples live in **`docs/OPERATIONS.md`**.

---

## 🚀 Deployment

```bash
# Build & push image
VERSION=$(git rev-parse --short HEAD)
docker build -t ghcr.io/your-org/ews-mcp-server:$VERSION .
docker push ghcr.io/your-org/ews-mcp-server:$VERSION

# Kubernetes example
kubectl apply -f k8s/             # manifests include HPA & PodDisruptionBudget
```

Blue-green and rollback steps are covered in **`scripts/deploy.sh`**.

---

## 🗓 Roadmap

* **M01** – BasicAuth, email tool v1, retries, single-node runtime  
* **M02** – OAuth App, PM2 cluster, Redis Sentinel, circuit-breaker  
* **M03** – Calendar & contacts tools, prediction cache, Grafana dashboards  
* **M04** – RBAC, OBO flow, attachment streaming, helm chart  

---

## 🤝 Contributing

PRs are welcome!  Please read **`docs/CONTRIBUTING.md`** for style guide and commit rules.
Issues tagged **good first issue** are perfect entry points.

---

## 📜 License

MIT © 2025 Your Company / Your Name
