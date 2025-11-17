#!/bin/bash
set -e

echo "Running tests for EWS MCP Server..."

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Run linter
echo "Running linter..."
ruff check src/ || true

# Run type checker
echo "Running type checker..."
mypy src/ || true

# Run tests with coverage
echo "Running tests..."
pytest --cov=src --cov-report=html --cov-report=term-missing

echo "✓ Tests complete!"
echo "  Coverage report: htmlcov/index.html"
