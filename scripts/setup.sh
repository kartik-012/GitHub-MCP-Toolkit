#!/usr/bin/env bash
# GitHub MCP Toolkit — Development Environment Setup Script

set -e

echo "===================================================="
echo " Setting up GitHub MCP Toolkit Development Env"
echo "===================================================="

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed."
    exit 1
fi

PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Detected Python version: $PYTHON_VER"

# Create virtual environment if not exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment in ./venv..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Upgrade pip and install dependencies
echo "Installing dependencies from requirements.txt..."
pip install --upgrade pip
pip install -r requirements.txt

# Copy .env.example if .env does not exist
if [ ! -f ".env" ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo "Please set your GITHUB_TOKEN in .env"
fi

echo "===================================================="
echo " Setup Complete! Running verification test suite..."
echo "===================================================="

pytest tests/ -v
