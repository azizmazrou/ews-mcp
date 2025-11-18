# Multi-stage build for minimal image size
FROM python:3.11-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Upgrade pip and install build tools
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy requirements
COPY requirements.txt .

# Install Python dependencies to /opt/venv
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

# Runtime stage
FROM python:3.11-slim

# Install runtime dependencies including gosu for user switching
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    ca-certificates \
    tzdata \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -g 1000 mcp && \
    useradd -r -u 1000 -g mcp -m -s /bin/bash mcp

# Set working directory
WORKDIR /app

# Copy Python virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Create logs directory with proper permissions (before switching user)
RUN mkdir -p /app/logs/analysis && chown -R mcp:mcp /app/logs

# Copy application code
COPY --chown=mcp:mcp src/ ./src/

# CRITICAL: Remove any .pyc files that might have been copied despite .dockerignore
# This ensures we're running from fresh .py source files only
RUN find /app/src -type f -name "*.pyc" -delete && \
    find /app/src -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Verify critical code exists (build will fail if code is missing)
RUN echo "=== Verifying deployed code ===" && \
    grep -q "VERSION: 2025-11-18-GAL-ONLY-FIX" /app/src/tools/contact_intelligence_tools.py && echo "✓ Correct version deployed" || \
    (echo "✗ ERROR: Wrong version! Docker is using cached old code!" && exit 1)
RUN grep -q "Method 3: Trying wildcard resolve_names" /app/src/tools/contact_intelligence_tools.py && echo "✓ Method 3 found" || \
    (echo "✗ ERROR: Method 3 code not found in container!" && exit 1)
RUN grep -q "Method 4: Trying ActiveDirectoryContacts" /app/src/tools/contact_intelligence_tools.py && echo "✓ Method 4 found" || \
    (echo "✗ ERROR: Method 4 code not found in container!" && exit 1)
RUN grep -q "include_personal_contacts" /app/src/tools/contact_intelligence_tools.py && echo "✓ GAL-only fix found" || \
    (echo "✗ ERROR: GAL-only fix not found in container!" && exit 1)
RUN ! grep -q "Exchange Server may not support Unicode" /app/src/tools/contact_intelligence_tools.py && echo "✓ Old warning not found" || \
    (echo "✗ ERROR: Old warning found in container! Docker cached old code!" && exit 1)

# Copy scripts
COPY --chown=mcp:mcp scripts/ ./scripts/

# Copy entrypoint script (keep as root for now)
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set environment
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Note: Container starts as root to allow entrypoint script to:
# - Create log directories in mounted volumes
# - Set proper ownership (mcp:mcp)
# - Then switch to mcp user using gosu before starting the application

# Expose port for HTTP/SSE transport (optional, only used when MCP_TRANSPORT=sse)
EXPOSE 8000

# Set entrypoint (runs as root, switches to mcp user internally)
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Run server (use CMD for easy override in tests)
CMD ["python", "-m", "src.main"]
