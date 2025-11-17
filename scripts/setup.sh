#!/bin/bash
set -e

echo "Setting up EWS MCP Server..."

# Create virtual environment
echo "Creating virtual environment..."
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements-dev.txt

# Create .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "Creating .env file from template..."
    cp .env.example .env
    echo "⚠ Please edit .env with your Exchange credentials"
fi

# Create logs directory
mkdir -p logs

echo "✓ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your Exchange credentials"
echo "  2. Activate virtual environment: source venv/bin/activate"
echo "  3. Run the server: python -m src.main"
