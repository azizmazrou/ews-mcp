#!/usr/bin/env bash
#
# wire-ai-networking.sh — one-shot fix for the AI Docker bridge issue
#
# Symptom this fixes:
#   semantic_search_emails returns 500 with
#     ToolExecutionError: Embedding provider error: Embedding endpoint
#     unreachable at http://<HOST_LAN_IP>:11434/v1/embeddings:
#     ConnectError: All connection attempts failed
#
# What it does (idempotent — safe to re-run):
#   1. Creates the external `claude-shared` Docker network if missing.
#   2. Detects the Ollama container by image (ollama/ollama) or by port 11434.
#   3. Connects Ollama to claude-shared with the network alias "ollama"
#      (skipped if already attached).
#   4. Locates the ews-mcp compose directory and patches `.env` so
#      AI_BASE_URL=http://ollama:11434/v1 (with backup of the prior value).
#   5. Recreates the ews-mcp container with the new compose so it picks
#      up the new env var and joins the shared network.
#
# Run on the NAS host:
#   ssh qnap
#   cd /share/CACHEDEV1_DATA/Container/ews-mcp   # or wherever your compose is
#   bash scripts/wire-ai-networking.sh
#
# Override autodetection if needed:
#   OLLAMA_CONTAINER=my-ollama bash scripts/wire-ai-networking.sh
#   EWS_COMPOSE_DIR=/path/to/ews-mcp bash scripts/wire-ai-networking.sh
#
set -euo pipefail

# QNAP Container Station ships docker at this path; PATH-installed docker
# (Linux Docker Desktop, etc.) wins if present.
DOCKER="${DOCKER:-$(command -v docker || echo /share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker)}"
[[ -x "$DOCKER" ]] || { echo "ERR: docker not found (set DOCKER=...)"; exit 2; }

NETWORK="${NETWORK:-claude-shared}"
ALIAS="${ALIAS:-ollama}"
NEW_URL="${AI_BASE_URL_NEW:-http://${ALIAS}:11434/v1}"
EWS_SERVICE="${EWS_SERVICE:-ews-mcp-server}"

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# ---------------------------------------------------------------------------
# 1. external network
# ---------------------------------------------------------------------------
if "$DOCKER" network inspect "$NETWORK" >/dev/null 2>&1; then
  log "network '$NETWORK' already exists — leaving it alone"
else
  log "creating external network '$NETWORK'"
  "$DOCKER" network create "$NETWORK" >/dev/null
fi

# ---------------------------------------------------------------------------
# 2. find Ollama container
# ---------------------------------------------------------------------------
if [[ -n "${OLLAMA_CONTAINER:-}" ]]; then
  log "using OLLAMA_CONTAINER=$OLLAMA_CONTAINER (env override)"
else
  # Prefer image match; fall back to "publishes 11434" search.
  OLLAMA_CONTAINER="$("$DOCKER" ps --format '{{.Names}}\t{{.Image}}' \
    | awk -F'\t' 'tolower($2) ~ /ollama/ {print $1; exit}')"
  if [[ -z "$OLLAMA_CONTAINER" ]]; then
    OLLAMA_CONTAINER="$("$DOCKER" ps --format '{{.Names}}\t{{.Ports}}' \
      | awk -F'\t' '$2 ~ /11434/ {print $1; exit}')"
  fi
fi
[[ -n "$OLLAMA_CONTAINER" ]] || {
  echo "ERR: could not autodetect the Ollama container."
  echo "     Either start it, or run with OLLAMA_CONTAINER=<name>."
  exit 3
}
log "Ollama container: $OLLAMA_CONTAINER"

# ---------------------------------------------------------------------------
# 3. attach Ollama to the shared network with alias
# ---------------------------------------------------------------------------
attached="$("$DOCKER" inspect -f \
  "{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}" \
  "$OLLAMA_CONTAINER")"
if grep -qw "$NETWORK" <<<"$attached"; then
  log "$OLLAMA_CONTAINER already on '$NETWORK' — leaving it"
else
  log "connecting $OLLAMA_CONTAINER to '$NETWORK' as alias '$ALIAS'"
  "$DOCKER" network connect --alias "$ALIAS" "$NETWORK" "$OLLAMA_CONTAINER"
fi

# Probe DNS from inside Ollama to itself — sanity check that the alias
# resolves before we touch ews-mcp.
if "$DOCKER" exec "$OLLAMA_CONTAINER" sh -c 'getent hosts '"$ALIAS"' || true' \
   | grep -q "$ALIAS"; then
  log "DNS '$ALIAS' resolves inside Ollama — good"
else
  log "WARN: '$ALIAS' did not resolve via getent; continuing (may resolve only from peers)"
fi

# ---------------------------------------------------------------------------
# 4. patch ews-mcp .env
# ---------------------------------------------------------------------------
EWS_COMPOSE_DIR="${EWS_COMPOSE_DIR:-$PWD}"
ENV_FILE="$EWS_COMPOSE_DIR/.env"
COMPOSE_FILE="$EWS_COMPOSE_DIR/docker-compose-ghcr.yml"
[[ -f "$COMPOSE_FILE" ]] || COMPOSE_FILE="$EWS_COMPOSE_DIR/docker-compose.yml"
[[ -f "$COMPOSE_FILE" ]] || {
  echo "ERR: no docker-compose*.yml in $EWS_COMPOSE_DIR"
  echo "     cd into the ews-mcp compose dir, or set EWS_COMPOSE_DIR=..."
  exit 4
}
[[ -f "$ENV_FILE" ]] || {
  echo "ERR: no .env in $EWS_COMPOSE_DIR (compose at $COMPOSE_FILE)"
  exit 5
}

log "patching $ENV_FILE → AI_BASE_URL=$NEW_URL"
cp -p "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
if grep -q '^AI_BASE_URL=' "$ENV_FILE"; then
  # In-place replace (BSD/GNU sed compatible — no -i'' arg)
  awk -v new="AI_BASE_URL=$NEW_URL" \
      'BEGIN{done=0} /^AI_BASE_URL=/{print new; done=1; next} {print} END{if(!done)print new}' \
      "$ENV_FILE" >"$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
else
  echo "AI_BASE_URL=$NEW_URL" >>"$ENV_FILE"
fi
grep '^AI_BASE_URL=' "$ENV_FILE"

# ---------------------------------------------------------------------------
# 5. recreate ews-mcp so it picks up new env + new network
# ---------------------------------------------------------------------------
log "recreating $EWS_SERVICE with new compose ($COMPOSE_FILE)"
( cd "$EWS_COMPOSE_DIR" && \
  "$DOCKER" compose -f "$(basename "$COMPOSE_FILE")" up -d --force-recreate "$EWS_SERVICE" )

# ---------------------------------------------------------------------------
# 6. quick post-flight: ews-mcp can now resolve 'ollama' from inside
# ---------------------------------------------------------------------------
EWS_CTR="$("$DOCKER" ps --filter "name=$EWS_SERVICE" --format '{{.Names}}' | head -1)"
if [[ -n "$EWS_CTR" ]]; then
  log "post-flight DNS check from $EWS_CTR:"
  "$DOCKER" exec "$EWS_CTR" sh -c "getent hosts $ALIAS || nslookup $ALIAS 2>/dev/null || echo 'no DNS tool available'" || true
fi

log "done. Smoke test:"
echo "    curl -sS -H 'Authorization: Bearer \$TOKEN' \\"
echo "         -H 'Content-Type: application/json' \\"
echo "         -d '{\"query\":\"meeting tomorrow\",\"max_results\":3,\"threshold\":0.0}' \\"
echo "         http://localhost:8000/api/tools/semantic_search_emails | jq ."
