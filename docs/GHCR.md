# GitHub Container Registry (GHCR) Guide

Complete guide for using pre-built Docker images from GitHub Container Registry.

## Overview

EWS MCP Server automatically publishes Docker images to GitHub Container Registry (GHCR) for easy deployment without building from source.

**Registry URL**: `ghcr.io/azizmazrou/ews-mcp`

## Available Images

### Image Tags

| Tag | Description | Use Case |
|-----|-------------|----------|
| `latest` | Latest stable release | Production |
| `v1.0.0` | Specific version | Production (version pinning) |
| `main` | Latest commit on main branch | Testing latest features |
| `main-sha-<hash>` | Specific commit | Debugging/rollback |

### Platform Support

All images support multiple architectures:
- **linux/amd64** - Intel/AMD x86_64 systems
- **linux/arm64** - ARM64 systems (Apple Silicon M1/M2, AWS Graviton, etc.)

Docker automatically pulls the correct architecture for your system.

## Quick Start

### 1. Pull the Image

```bash
# Pull latest version
docker pull ghcr.io/azizmazrou/ews-mcp:latest

# Pull specific version (recommended for production)
docker pull ghcr.io/azizmazrou/ews-mcp:v1.0.0

# Pull development version
docker pull ghcr.io/azizmazrou/ews-mcp:main
```

### 2. Create Configuration

Create a `.env` file with your Exchange credentials:

```bash
cat > .env <<EOF
EWS_EMAIL=user@company.com
EWS_AUTH_TYPE=oauth2
EWS_CLIENT_ID=your-client-id
EWS_CLIENT_SECRET=your-client-secret
EWS_TENANT_ID=your-tenant-id
LOG_LEVEL=INFO
EOF
```

### 3. Run the Container

```bash
docker run -d \
  --name ews-mcp-server \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  ghcr.io/azizmazrou/ews-mcp:latest
```

### 4. Verify It's Running

```bash
# Check container status
docker ps | grep ews-mcp

# View logs
docker logs -f ews-mcp-server
```

## Using with Docker Compose

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
    stdin_open: true
    tty: true
    volumes:
      - ./logs:/app/logs:rw
```

Run with:

```bash
docker-compose up -d
```

## Claude Desktop Integration

### Configuration File Location

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
**Linux**: `~/.config/Claude/claude_desktop_config.json`

### Configuration

```json
{
  "mcpServers": {
    "ews": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "--env-file",
        "/absolute/path/to/.env",
        "ghcr.io/azizmazrou/ews-mcp:latest"
      ]
    }
  }
}
```

**Important**: Use absolute paths for the `.env` file!

## Version Pinning (Recommended for Production)

Always pin to a specific version in production:

```bash
# In docker-compose.yml
image: ghcr.io/azizmazrou/ews-mcp:v1.0.0

# In Claude Desktop config
"ghcr.io/azizmazrou/ews-mcp:v1.0.0"
```

## Automated Updates

Images are automatically built and published when:
- A new tag is pushed (e.g., `v1.0.0`)
- Code is merged to main branch
- Manual workflow dispatch is triggered

### Build Process

1. **Trigger**: Push to main or new tag
2. **Build**: Multi-platform build (amd64 + arm64)
3. **Test**: Automated tests run
4. **Publish**: Push to GHCR
5. **Attestation**: Generate provenance attestation

### Build Status

Check build status on GitHub Actions:
- Go to repository → Actions tab
- View "Build and Publish Docker Image" workflow

## Authentication (If Required)

GHCR images for this project are public and don't require authentication. However, if you need to authenticate:

```bash
# Create personal access token with read:packages scope
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin

# Pull image
docker pull ghcr.io/azizmazrou/ews-mcp:latest
```

## Updating Images

### Check for Updates

```bash
# Check current image version
docker images ghcr.io/azizmazrou/ews-mcp

# Pull latest
docker pull ghcr.io/azizmazrou/ews-mcp:latest

# Check for updates
docker image inspect ghcr.io/azizmazrou/ews-mcp:latest | grep Created
```

### Update Running Container

```bash
# Pull new image
docker pull ghcr.io/azizmazrou/ews-mcp:latest

# Stop and remove old container
docker stop ews-mcp-server
docker rm ews-mcp-server

# Start new container
docker run -d \
  --name ews-mcp-server \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  ghcr.io/azizmazrou/ews-mcp:latest
```

### Update with Docker Compose

```bash
# Pull new image
docker-compose pull

# Restart with new image
docker-compose up -d
```

## Image Information

### View Image Details

```bash
# Inspect image
docker inspect ghcr.io/azizmazrou/ews-mcp:latest

# View image labels
docker inspect ghcr.io/azizmazrou/ews-mcp:latest | jq '.[0].Config.Labels'

# View image layers
docker history ghcr.io/azizmazrou/ews-mcp:latest
```

### Image Size

Approximate sizes:
- **Compressed**: ~40-50 MB
- **Extracted**: ~120-150 MB

Minimal size achieved through:
- Multi-stage build
- Alpine Linux base
- Optimized dependencies

## Troubleshooting

### Problem: "Failed to pull image"

**Solutions**:
1. Check internet connectivity
2. Verify image name is correct
3. Check Docker is running
4. Try pulling manually:
   ```bash
   docker pull ghcr.io/azizmazrou/ews-mcp:latest
   ```

### Problem: "Manifest not found"

**Solutions**:
1. Verify tag exists: Check GitHub Packages
2. Try without tag (defaults to latest):
   ```bash
   docker pull ghcr.io/azizmazrou/ews-mcp
   ```
3. Check if image is published: Visit GitHub repository → Packages

### Problem: "Wrong architecture"

**Solutions**:
```bash
# Check your system architecture
uname -m

# Pull specific platform
docker pull --platform linux/amd64 ghcr.io/azizmazrou/ews-mcp:latest
# or
docker pull --platform linux/arm64 ghcr.io/azizmazrou/ews-mcp:latest
```

### Problem: "Image always uses old version"

**Solutions**:
```bash
# Force pull new image
docker pull ghcr.io/azizmazrou/ews-mcp:latest

# Clear Docker cache
docker system prune -a

# Use specific version tag
docker pull ghcr.io/azizmazrou/ews-mcp:v1.0.0
```

## Best Practices

### Production Deployment

1. **Pin Versions**: Use specific version tags
   ```yaml
   image: ghcr.io/azizmazrou/ews-mcp:v1.0.0
   ```

2. **Set Resource Limits**:
   ```yaml
   deploy:
     resources:
       limits:
         cpus: '1.0'
         memory: 512M
   ```

3. **Configure Health Checks**:
   ```yaml
   healthcheck:
     test: ["CMD", "python", "-c", "import sys; sys.exit(0)"]
     interval: 30s
     timeout: 10s
     retries: 3
   ```

4. **Enable Restart Policy**:
   ```yaml
   restart: unless-stopped
   ```

### Development

1. **Use Main Tag**: Get latest features
   ```bash
   docker pull ghcr.io/azizmazrou/ews-mcp:main
   ```

2. **Mount Logs**: For debugging
   ```bash
   -v $(pwd)/logs:/app/logs
   ```

3. **Set Debug Logging**:
   ```bash
   -e LOG_LEVEL=DEBUG
   ```

## Comparison: GHCR vs Local Build

| Feature | GHCR Image | Local Build |
|---------|-----------|-------------|
| **Setup Time** | Instant | 5-10 minutes |
| **Disk Space** | ~150 MB | ~500 MB+ |
| **Updates** | Pull latest | Rebuild required |
| **Customization** | Limited | Full control |
| **Build Tools** | Not needed | Docker, git required |
| **Use Case** | Production | Development |

## Security

### Image Verification

Images include build attestations:

```bash
# View attestation (requires Docker 4.24+)
docker buildx imagetools inspect \
  ghcr.io/azizmazrou/ews-mcp:latest \
  --format "{{json .Provenance}}"
```

### Scanning for Vulnerabilities

```bash
# Scan with Docker Scout
docker scout cves ghcr.io/azizmazrou/ews-mcp:latest

# Scan with Trivy
trivy image ghcr.io/azizmazrou/ews-mcp:latest
```

## Advanced Usage

### Multi-Container Setup

```yaml
version: '3.8'

services:
  ews-user1:
    image: ghcr.io/azizmazrou/ews-mcp:latest
    env_file: .env.user1

  ews-user2:
    image: ghcr.io/azizmazrou/ews-mcp:latest
    env_file: .env.user2
```

### Using with Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ews-mcp-server
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
        image: ghcr.io/azizmazrou/ews-mcp:v1.0.0
        envFrom:
        - secretRef:
            name: ews-credentials
```

## Support

For issues with GHCR images:
1. Check GitHub Actions for build status
2. Verify image tag exists in Packages
3. Try pulling with explicit tag
4. Report issues on GitHub repository

## Links

- **GitHub Repository**: https://github.com/azizmazrou/ews-mcp
- **GHCR Package**: https://github.com/azizmazrou/ews-mcp/pkgs/container/ews-mcp
- **Dockerfile**: https://github.com/azizmazrou/ews-mcp/blob/main/Dockerfile
- **GitHub Actions**: https://github.com/azizmazrou/ews-mcp/actions
