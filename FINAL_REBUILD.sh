#!/bin/bash
# Final rebuild after removing Python bytecode cache
# This ensures Docker uses fresh .py source files, not cached .pyc bytecode

set -e

echo "🔧 FINAL REBUILD - Clean Python cache + Docker rebuild"
echo "======================================================="
echo ""

# 1. Remove all Python bytecode cache
echo "1. Removing Python bytecode cache..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true
find . -name "*.pyo" -delete 2>/dev/null || true
find . -name "*.pyd" -delete 2>/dev/null || true
echo "   ✓ Python cache removed"

# 2. Verify source code is correct
echo ""
echo "2. Verifying source code..."
if grep -q "Method 3: Trying wildcard resolve_names" src/tools/contact_intelligence_tools.py; then
    echo "   ✓ Method 3 code found"
else
    echo "   ✗ ERROR: Method 3 code not found!"
    exit 1
fi

if grep -q "Method 4: Trying ActiveDirectoryContacts" src/tools/contact_intelligence_tools.py; then
    echo "   ✓ Method 4 code found"
else
    echo "   ✗ ERROR: Method 4 code not found!"
    exit 1
fi

if grep -q "Exchange Server may not support Unicode" src/tools/contact_intelligence_tools.py; then
    echo "   ✗ ERROR: Old warning still in source!"
    exit 1
else
    echo "   ✓ Old warning removed (correct)"
fi

# 3. Stop container
echo ""
echo "3. Stopping container..."
docker-compose down

# 4. Remove images
echo ""
echo "4. Removing old images..."
docker images | grep -E 'ews-mcp' | awk '{print $3}' | xargs docker rmi -f 2>/dev/null || echo "   No images to remove"

# 5. Clean Docker build cache
echo ""
echo "5. Cleaning Docker build cache..."
docker builder prune -f

# 6. Build with no cache and PYTHONDONTWRITEBYTECODE
echo ""
echo "6. Building fresh container (no cache, no bytecode)..."
DOCKER_BUILDKIT=1 docker-compose build --no-cache --progress=plain 2>&1 | tail -30

# 7. Start container
echo ""
echo "7. Starting container..."
docker-compose up -d

# 8. Wait for startup
echo ""
echo "8. Waiting for server to start..."
sleep 8

# 9. Show recent logs
echo ""
echo "9. Container logs:"
echo "================================================================"
docker-compose logs --tail 40
echo "================================================================"

echo ""
echo "✅ FINAL REBUILD COMPLETE!"
echo ""
echo "Test with: find_person(query='[test]', search_scope='gal')"
echo ""
echo "Expected log output:"
echo "  - Method 1: Trying resolve_names API"
echo "  - Method 2: Trying Contacts folder search"
echo "  - Method 3: Trying wildcard resolve_names    ← MUST appear"
echo "  - Trying wildcard query: '*...*'             ← MUST appear"
echo "  - Method 4: Trying ActiveDirectoryContacts   ← MUST appear"
echo ""
echo "The old warning about Unicode should NOT appear."
