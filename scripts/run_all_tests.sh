#!/bin/bash
# Comprehensive Test Runner for EWS MCP Server
# Runs all tests with coverage reporting and formatted output

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  EWS MCP Server v2.0.0 - Comprehensive Test Suite${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo -e "${RED}✗ pytest not found. Installing test dependencies...${NC}"
    pip install -r requirements-dev.txt
fi

# Create coverage report directory
mkdir -p coverage_reports

echo -e "${BLUE}📊 Running Unit Tests with Coverage Analysis...${NC}"
echo ""

# Run pytest with coverage
pytest \
    --verbose \
    --tb=short \
    --cov=src \
    --cov-report=term-missing \
    --cov-report=html:coverage_reports/html \
    --cov-report=xml:coverage_reports/coverage.xml \
    --junit-xml=coverage_reports/junit.xml \
    tests/

# Capture exit code
TEST_EXIT_CODE=$?

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  Test Results Summary${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Parse coverage percentage
COVERAGE_PERCENT=$(pytest --cov=src --cov-report=term-missing tests/ 2>&1 | grep "TOTAL" | awk '{print $NF}' | sed 's/%//')

if [ -z "$COVERAGE_PERCENT" ]; then
    COVERAGE_PERCENT=0
fi

echo -e "${BLUE}📈 Coverage Report:${NC}"
echo -e "   Location: coverage_reports/html/index.html"
echo -e "   Coverage: ${COVERAGE_PERCENT}%"
echo ""

# Check test results
if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}✓ ALL TESTS PASSED${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    # Check coverage threshold
    COVERAGE_INT=$(echo "$COVERAGE_PERCENT" | cut -d'.' -f1)
    if [ "$COVERAGE_INT" -ge 80 ]; then
        echo -e "${GREEN}✓ Coverage threshold met (${COVERAGE_PERCENT}% >= 80%)${NC}"
        exit 0
    else
        echo -e "${YELLOW}⚠ Coverage below threshold (${COVERAGE_PERCENT}% < 80%)${NC}"
        echo -e "${YELLOW}  Consider adding more tests to reach 80% coverage${NC}"
        exit 0  # Still exit 0 since tests passed
    fi
else
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}✗ SOME TESTS FAILED${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "${YELLOW}Please review the test output above for details.${NC}"
    exit 1
fi
