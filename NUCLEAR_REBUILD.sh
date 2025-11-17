#!/bin/bash
# NUCLEAR OPTION: Completely destroy and rebuild everything
# Use this when Docker caching is completely broken

set -e

echo "💣 NUCLEAR REBUILD - Destroying everything and starting fresh"
echo "================================================================"
echo ""

# 1. Stop ALL containers
echo "1. Stopping ALL containers..."
docker stop $(docker ps -aq) 2>/dev/null || echo "No containers running"

# 2. Remove ALL containers
echo "2. Removing ALL containers..."
docker rm $(docker ps -aq) 2>/dev/null || echo "No containers to remove"

# 3. Remove ALL images for this project
echo "3. Removing project images..."
docker images | grep -E '(ews-mcp|ews_mcp)' | awk '{print $3}' | xargs docker rmi -f 2>/dev/null || echo "No project images found"

# 4. Prune ALL Docker build cache
echo "4. Pruning ALL build cache..."
docker builder prune --all --force

# 5. Prune system (removes unused data)
echo "5. Pruning Docker system..."
docker system prune -f

# 6. Verify Python source file has correct code
echo "6. Verifying source code..."
echo "   Checking for Method 3 logging..."
if grep -q "Method 3: Trying wildcard resolve_names" src/tools/contact_intelligence_tools.py; then
    echo "   ✓ Method 3 code found"
else
    echo "   ✗ ERROR: Method 3 code not found in source!"
    exit 1
fi

echo "   Checking for Method 4 logging..."
if grep -q "Method 4: Trying ActiveDirectoryContacts" src/tools/contact_intelligence_tools.py; then
    echo "   ✓ Method 4 code found"
else
    echo "   ✗ ERROR: Method 4 code not found in source!"
    exit 1
fi

echo "   Checking old warning is gone..."
if grep -q "Exchange Server may not support Unicode" src/tools/contact_intelligence_tools.py; then
    echo "   ✗ ERROR: Old warning still in source code!"
    exit 1
else
    echo "   ✓ Old warning not found (correct)"
fi

# 7. Build with absolutely no cache
echo ""
echo "7. Building with ZERO cache..."
DOCKER_BUILDKIT=1 docker-compose build --no-cache --progress=plain --pull 2>&1 | tail -50

# 8. Start container
echo ""
echo "8. Starting fresh container..."
docker-compose up -d

# 9. Wait for startup
echo ""
echo "9. Waiting for server to start..."
sleep 8

# 10. Verify the running container has new code
echo ""
echo "10. Verifying deployed code in container..."
docker exec ews-mcp-server grep -q "Method 3: Trying wildcard" /app/src/tools/contact_intelligence_tools.py && echo "   ✓ Method 3 deployed" || echo "   ✗ Method 3 NOT deployed"
docker exec ews-mcp-server grep -q "Method 4: Trying ActiveDirectoryContacts" /app/src/tools/contact_intelligence_tools.py && echo "   ✓ Method 4 deployed" || echo "   ✗ Method 4 NOT deployed"
docker exec ews-mcp-server grep -q "Exchange Server may not support Unicode" /app/src/tools/contact_intelligence_tools.py && echo "   ✗ Old code still deployed!" || echo "   ✓ Old code removed (correct)"

# 11. Show logs
echo ""
echo "11. Container logs:"
echo "================================================================"
docker-compose logs --tail 30
echo "================================================================"

echo ""
echo "✅ NUCLEAR REBUILD COMPLETE!"
echo ""
echo "Now test with: find_person(query='[test_query]', search_scope='gal')"
echo "You should see Methods 1, 2, 3, and 4 in the logs."
