# Deployment Guide

Complete guide for deploying EWS MCP Server v3.0 in various environments.

> **v3.0 Features:** Person-centric architecture, multi-strategy GAL search, enterprise logging (console for monitoring, files for troubleshooting), and intelligent caching.

## Pre-built Docker Image (Easiest)

The fastest way to deploy is using pre-built images from GitHub Container Registry (GHCR).

### Pull and Run

```bash
# Pull latest image
docker pull ghcr.io/azizmazrou/ews-mcp:latest

# Create .env file
cat > .env <<EOF
EWS_EMAIL=user@company.com
EWS_AUTH_TYPE=oauth2
EWS_CLIENT_ID=your-client-id
EWS_CLIENT_SECRET=your-client-secret
EWS_TENANT_ID=your-tenant-id
EOF

# Run container
docker run -d \
  --name ews-mcp-server \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  ghcr.io/azizmazrou/ews-mcp:latest

# Check logs
docker logs -f ews-mcp-server
```

### Using Docker Compose with GHCR

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  ews-mcp-server:
    image: ghcr.io/azizmazrou/ews-mcp:latest
    container_name: ews-mcp-server
    env_file:
      - .env
    restart: unless-stopped
    volumes:
      - ./logs:/app/logs:rw
```

Run:

```bash
docker-compose up -d
```

**See [GHCR Guide](GHCR.md) for more details on using pre-built images.**

## Docker Deployment from Source

### Standalone Container

```bash
# Build image
docker build -t ews-mcp-server:latest .

# Run with environment file
docker run -d \
  --name ews-mcp-server \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  ews-mcp-server:latest

# Run with inline environment variables
docker run -d \
  --name ews-mcp-server \
  -e EWS_EMAIL=user@company.com \
  -e EWS_AUTH_TYPE=oauth2 \
  -e EWS_CLIENT_ID=your-id \
  -e EWS_CLIENT_SECRET=your-secret \
  -e EWS_TENANT_ID=your-tenant \
  ews-mcp-server:latest

# View logs
docker logs -f ews-mcp-server

# Stop and remove
docker stop ews-mcp-server
docker rm ews-mcp-server
```

### Docker Compose

```bash
# Start services
docker-compose up -d

# View logs
docker-compose logs -f

# Restart
docker-compose restart

# Stop
docker-compose down

# Rebuild and restart
docker-compose up --build -d
```

## Kubernetes Deployment

### Prerequisites
- Kubernetes cluster (1.19+)
- kubectl configured
- Container registry access

### Step 1: Build and Push Image

```bash
# Build image
docker build -t your-registry/ews-mcp-server:1.0.0 .

# Push to registry
docker push your-registry/ews-mcp-server:1.0.0
```

### Step 2: Create Secret

```bash
# Create secret from .env file
kubectl create secret generic ews-credentials \
  --from-literal=ews-email=user@company.com \
  --from-literal=ews-auth-type=oauth2 \
  --from-literal=ews-client-id=your-id \
  --from-literal=ews-client-secret=your-secret \
  --from-literal=ews-tenant-id=your-tenant

# Or from file
kubectl create secret generic ews-credentials --from-env-file=.env
```

### Step 3: Create Deployment

Create `k8s/deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ews-mcp-server
  labels:
    app: ews-mcp-server
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ews-mcp-server
  template:
    metadata:
      labels:
        app: ews-mcp-server
    spec:
      containers:
      - name: ews-mcp-server
        image: your-registry/ews-mcp-server:1.0.0
        envFrom:
        - secretRef:
            name: ews-credentials
        resources:
          requests:
            memory: "256Mi"
            cpu: "100m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        volumeMounts:
        - name: logs
          mountPath: /app/logs
      volumes:
      - name: logs
        emptyDir: {}
```

### Step 4: Deploy

```bash
kubectl apply -f k8s/deployment.yaml
kubectl get pods -l app=ews-mcp-server
kubectl logs -f deployment/ews-mcp-server
```

## Cloud Deployments

### AWS ECS/Fargate

#### Prerequisites
- AWS CLI configured
- ECR repository created

#### Deploy

```bash
# Build and push to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
docker build -t ews-mcp-server .
docker tag ews-mcp-server:latest <account>.dkr.ecr.us-east-1.amazonaws.com/ews-mcp-server:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/ews-mcp-server:latest

# Create task definition (task-definition.json)
{
  "family": "ews-mcp-server",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256",
  "memory": "512",
  "containerDefinitions": [{
    "name": "ews-mcp-server",
    "image": "<account>.dkr.ecr.us-east-1.amazonaws.com/ews-mcp-server:latest",
    "secrets": [
      {"name": "EWS_EMAIL", "valueFrom": "arn:aws:secretsmanager:..."},
      {"name": "EWS_CLIENT_ID", "valueFrom": "arn:aws:secretsmanager:..."}
    ]
  }]
}

# Register task
aws ecs register-task-definition --cli-input-json file://task-definition.json

# Create service
aws ecs create-service \
  --cluster your-cluster \
  --service-name ews-mcp-server \
  --task-definition ews-mcp-server \
  --desired-count 1 \
  --launch-type FARGATE
```

### Azure Container Instances

```bash
# Create resource group
az group create --name ews-mcp-rg --location eastus

# Create container
az container create \
  --resource-group ews-mcp-rg \
  --name ews-mcp-server \
  --image your-registry/ews-mcp-server:1.0.0 \
  --secure-environment-variables \
    EWS_EMAIL=user@company.com \
    EWS_CLIENT_ID=your-id \
    EWS_CLIENT_SECRET=your-secret \
    EWS_TENANT_ID=your-tenant \
  --cpu 1 \
  --memory 1

# View logs
az container logs --resource-group ews-mcp-rg --name ews-mcp-server --follow
```

### Google Cloud Run

```bash
# Build and push to GCR
gcloud builds submit --tag gcr.io/PROJECT-ID/ews-mcp-server

# Deploy
gcloud run deploy ews-mcp-server \
  --image gcr.io/PROJECT-ID/ews-mcp-server \
  --platform managed \
  --region us-central1 \
  --set-secrets=EWS_EMAIL=ews-email:latest \
  --set-secrets=EWS_CLIENT_ID=ews-client-id:latest \
  --set-secrets=EWS_CLIENT_SECRET=ews-client-secret:latest \
  --set-secrets=EWS_TENANT_ID=ews-tenant-id:latest
```

## Local Development

### Python Virtual Environment

```bash
# Create venv
python -m venv venv

# Activate
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements-dev.txt

# Configure
cp .env.example .env
# Edit .env

# Run
python -m src.main
```

### Using Poetry

```bash
# Install Poetry
curl -sSL https://install.python-poetry.org | python3 -

# Install dependencies
poetry install

# Run
poetry run python -m src.main
```

## Production Considerations

### Security

1. **Secrets Management**
   - Use Azure Key Vault / AWS Secrets Manager
   - Never commit secrets to git
   - Rotate credentials regularly

2. **Network Security**
   - Use private networks
   - Implement firewall rules
   - Enable HTTPS only

3. **Access Control**
   - Principle of least privilege
   - Dedicated service account
   - Audit access logs

### High Availability

1. **Multiple Instances**
   - Run 2+ replicas
   - Use load balancer
   - Implement health checks

2. **Failover**
   - Automatic restart on failure
   - Circuit breaker pattern
   - Graceful degradation

### Monitoring

1. **Logging**
   - Centralized logging (ELK, Splunk)
   - Log aggregation
   - Retention policies

2. **Metrics**
   - Request rate
   - Error rate
   - Response time
   - Resource usage

3. **Alerts**
   - Authentication failures
   - High error rate
   - Resource exhaustion

### Backup and Recovery

1. **Configuration Backup**
   - Version control for configs
   - Regular backups
   - Documented restore procedure

2. **Disaster Recovery**
   - Runbook for incidents
   - Recovery time objective (RTO)
   - Recovery point objective (RPO)

## Performance Tuning

### Connection Pooling

```bash
CONNECTION_POOL_SIZE=20
REQUEST_TIMEOUT=60
```

### Rate Limiting

```bash
RATE_LIMIT_REQUESTS_PER_MINUTE=100
```

### Caching

```bash
ENABLE_CACHE=true
CACHE_TTL=600
```

## Scaling

### Horizontal Scaling

```yaml
# Kubernetes HPA
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ews-mcp-server
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ews-mcp-server
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
```

### Vertical Scaling

Adjust resource limits based on usage:

```yaml
resources:
  requests:
    memory: "512Mi"
    cpu: "250m"
  limits:
    memory: "1Gi"
    cpu: "1000m"
```

## Health Checks

### Liveness Probe

```yaml
livenessProbe:
  exec:
    command:
    - python
    - -c
    - "from src.main import EWSMCPServer; s = EWSMCPServer(); exit(0 if s.ews_client.test_connection() else 1)"
  initialDelaySeconds: 30
  periodSeconds: 60
```

### Readiness Probe

```yaml
readinessProbe:
  exec:
    command:
    - python
    - -c
    - "import sys; sys.exit(0)"
  initialDelaySeconds: 10
  periodSeconds: 5
```

## Troubleshooting Deployment

### Check Container Logs

```bash
# Docker
docker logs ews-mcp-server

# Kubernetes
kubectl logs deployment/ews-mcp-server

# AWS ECS
aws ecs describe-tasks --cluster your-cluster --tasks task-id
```

### Verify Environment Variables

```bash
# Docker
docker exec ews-mcp-server env | grep EWS

# Kubernetes
kubectl exec deployment/ews-mcp-server -- env | grep EWS
```

### Test Connectivity

```bash
# From container
docker exec -it ews-mcp-server sh
curl -v https://outlook.office365.com/EWS/Exchange.asmx
```

## Rollback Procedures

### Docker

```bash
# Tag previous version
docker tag ews-mcp-server:1.0.0 ews-mcp-server:rollback

# Deploy
docker-compose up -d
```

### Kubernetes

```bash
# Rollback to previous revision
kubectl rollout undo deployment/ews-mcp-server

# Rollback to specific revision
kubectl rollout undo deployment/ews-mcp-server --to-revision=2

# Check rollout status
kubectl rollout status deployment/ews-mcp-server
```

## Maintenance

### Update Dependencies

```bash
# Update requirements
pip install --upgrade -r requirements.txt
pip freeze > requirements.txt

# Rebuild image
docker build -t ews-mcp-server:latest .

# Deploy
docker-compose up -d --build
```

### Certificate Rotation

```bash
# Update certificates
kubectl create secret tls ews-tls --cert=cert.pem --key=key.pem --dry-run=client -o yaml | kubectl apply -f -

# Restart deployment
kubectl rollout restart deployment/ews-mcp-server
```
