#!/bin/bash

# CTF Agent Startup Script

set -e

echo "🚀 Starting CTF Agent..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Copying from .env.example..."
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "📝 Please edit .env file with your OpenAI API key"
        exit 1
    else
        echo "❌ .env.example not found. Please create .env file manually."
        exit 1
    fi
fi

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker is not running. Please start Docker and try again."
    exit 1
fi

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "⚠️  uv not found. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source $HOME/.cargo/env
fi

echo "🏗️  Building CTF Agent Docker image..."
docker build -t ctf-agent:latest -f docker/Dockerfile docker/

echo "🐳 Starting services with Docker Compose..."
docker-compose -f docker/docker-compose.yml up --build -d

echo "✅ CTF Agent is starting up!"
echo ""
echo "🌐 Web Interface: http://localhost:8000"
echo "📚 API Docs: http://localhost:8000/docs"
echo ""
echo "📋 To view logs: docker-compose -f docker/docker-compose.yml logs -f"
echo "🛑 To stop: docker-compose -f docker/docker-compose.yml down"
