# GitHub MCP Toolkit — Windows PowerShell Setup Script

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host " Setting up GitHub MCP Toolkit Development Env" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan

# Check Python installation
$pythonPath = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonPath) {
    Write-Error "Python is not installed or not in PATH."
    exit 1
}

# Create virtual environment if it doesn't exist
if (-not (Test-Path "venv")) {
    Write-Host "Creating virtual environment in .\venv..." -ForegroundColor Yellow
    python -m venv venv
}

# Activate virtual environment
Write-Host "Activating virtual environment..." -ForegroundColor Yellow
.\venv\Scripts\Activate.ps1

# Upgrade pip and install dependencies
Write-Host "Installing dependencies..." -ForegroundColor Yellow
python -m pip install --upgrade pip
pip install -r requirements.txt

# Copy .env.example if .env does not exist
if (-not (Test-Path ".env")) {
    Write-Host "Creating .env from .env.example..." -ForegroundColor Yellow
    Copy-Item .env.example .env
    Write-Host "Please set your GITHUB_TOKEN in .env" -ForegroundColor Yellow
}

Write-Host "====================================================" -ForegroundColor Green
Write-Host " Setup Complete! Running verification tests..." -ForegroundColor Green
Write-Host "====================================================" -ForegroundColor Green

pytest tests/ -v
