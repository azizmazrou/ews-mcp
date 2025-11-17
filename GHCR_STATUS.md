# GitHub Container Registry Status

## Current Status: ✅ Available

Pre-built Docker images are **now available** at `ghcr.io/azizmazrou/ews-mcp`!

**Latest Version:** v2.1.0
**Published:** November 2025
**Downloads:** 86+ (as of Nov 2025)
**Multi-platform Support:** linux/amd64, linux/arm64

## Quick Start

Pull and run the latest image:

```bash
# Pull the latest image
docker pull ghcr.io/azizmazrou/ews-mcp:latest

# Run with your configuration
docker run -d \
  --name ews-mcp-server \
  --env-file .env \
  ghcr.io/azizmazrou/ews-mcp:latest
```

## Available Tags

- `latest` - Latest stable release (v2.1.0)
- `v2.1.0` - Contact Intelligence release
- `v2.0.0` - Enterprise tools release
- `main` - Latest commit on main branch
- `sha-<commit>` - Specific commit

## Build from Source (Optional)

If you prefer to build locally or need a custom build:

```bash
# Clone repository
git clone https://github.com/azizmazrou/ews-mcp.git
cd ews-mcp

# Build image
docker build -t ews-mcp-server:latest .

# Run
docker run -d --name ews-mcp --env-file .env ews-mcp-server:latest
```

### Using Docker Compose

```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f
```

## Verify Images

Check available images and tags:

```bash
# View all available tags
docker pull ghcr.io/azizmazrou/ews-mcp:latest
docker images ghcr.io/azizmazrou/ews-mcp

# Check package page
# https://github.com/azizmazrou/ews-mcp/pkgs/container/ews-mcp
```

## Build Status

GitHub Actions automatically builds and publishes images on:
- Push to `main` or `master` branch
- Version tags (`v2.1.0`, etc.)
- Manual workflow dispatch

**Workflow Status:** https://github.com/azizmazrou/ews-mcp/actions

## Local Build Time

Building from source locally takes:
- **First build**: ~3-5 minutes
- **Subsequent builds**: ~1-2 minutes (with cache)

## Multi-platform Support

Images are built for:
- **linux/amd64** - Intel/AMD x86_64 systems
- **linux/arm64** - ARM64 systems (Apple Silicon, ARM servers)

Docker will automatically pull the correct image for your platform.
