#!/bin/bash

# Development startup script for CTF Agent

set -e

echo "🔧 Starting CTF Agent in Development Mode..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Please create one with your OpenAI API key"
    echo "Copy .env.example to .env and edit it"
    exit 1
fi

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "⚠️  uv not found. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source $HOME/.cargo/env
fi

# Install backend dependencies
echo "📦 Installing backend dependencies..."
uv pip install -e .

# Build CTF Docker image
echo "🏗️  Building CTF Agent Docker image..."
docker build -t ctf-agent:latest -f docker/Dockerfile docker/

# Start backend
echo "🚀 Starting CTF Agent..."
python -m backend.main &
BACKEND_PID=$!

echo ""
echo "✅ CTF Agent started!"
echo ""
echo "🌐 Web Interface: http://localhost:8000"
echo "📚 API Docs: http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop the service"

# Wait for interrupt
trap "kill $BACKEND_PID 2>/dev/null; exit" INT TERM

wait
