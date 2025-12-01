# CTF Agent

An AI-powered agent for solving Capture The Flag (CTF) challenges automatically. The system uses OpenAI's GPT-4o-mini model to analyze challenges and execute commands in a secure Docker environment.

## Features

- **Intelligent AI Agent**: Dynamic problem-solving approach that adapts to different challenge types
- **Web-based Interface**: Clean, modern UI for submitting challenges and monitoring progress
- **Real-time Observability**: Live WebSocket updates showing every action the AI agent takes
- **Docker Isolation**: Secure, isolated environment with pre-installed CTF tools
- **Multi-challenge Support**: Handles web, crypto, reverse engineering, forensics, and other challenge types
- **Smart Challenge Detection**: Auto-detects challenge type from URL patterns and context
- **Strategic Problem Solving**: AI follows reconnaissance → analysis → exploitation methodology
- **Flag Detection**: Automatic flag detection using customizable regex patterns
- **Cost Efficient**: Uses GPT-4o-mini to minimize API costs while maintaining effectiveness

## Architecture

- **Web Interface**: FastAPI with Jinja2 templates and Tailwind CSS
- **Backend**: FastAPI (Python) with Docker integration and OpenAI client
- **AI Model**: OpenAI GPT-4o-mini for cost-effective challenge solving
- **Container**: Ubuntu-based Docker image with comprehensive CTF toolset
- **Communication**: WebSocket for real-time updates

## Quick Start

### Prerequisites

- Docker and Docker Compose
- OpenAI API key
- uv (Python package manager)

### Setup

1. **Clone and setup environment**:
   ```bash
   git clone <repository-url>
   cd CTFAgent
   cp .env.example .env
   ```

2. **Configure environment variables**:
   ```bash
   # Edit .env file
   OPENAI_API_KEY=your_openai_api_key_here
   OPENAI_MODEL=gpt-4o-mini
   ```

3. **Build and run with Docker Compose**:
   ```bash
   docker-compose -f docker/docker-compose.yml up --build
   ```

4. **Access the application**:
   - Web Interface: http://localhost:8000
   - API Documentation: http://localhost:8000/docs

### Manual Setup (Development)

1. **Setup and run**:
   ```bash
   uv pip install -e .
   docker build -t ctf-agent:latest -f docker/Dockerfile docker/
   python -m backend.main
   ```

2. **Or use the development script**:
   ```bash
   ./dev.sh
   ```

## Usage

1. **Start a Challenge**:
   - Enter the challenge URL
   - Specify the flag format (regex pattern, e.g., `flag{.*}`)
   - Optionally set challenge type and description
   - Choose the reasoning effort (Low, Medium, High) for model planning depth
   - Click "Start Challenge"

2. **Monitor Progress**:
   - Watch real-time action logs in the right panel
   - See AI reasoning for each step
   - Monitor command execution and outputs
   - Track session status and duration

3. **Results**:
   - Flag automatically detected when found
   - Session completes with success/failure status
   - Full action history preserved for analysis

## How the AI Agent Works

The AI agent uses a sophisticated problem-solving approach:

### 1. **Challenge Analysis**
- Auto-detects challenge type from URL patterns
- Analyzes the target to understand the attack surface
- Selects appropriate tools and techniques

### 2. **Strategic Reconnaissance** 
- Gathers initial intelligence about the target
- Uses tools like `curl`, `nmap`, `gobuster` based on challenge type
- Builds understanding of potential vulnerabilities

### 3. **Adaptive Exploitation**
- Forms hypotheses about potential attack vectors
- Tests specific vulnerabilities systematically
- Adapts approach based on findings

### 4. **Continuous Learning**
- Each command builds on previous findings
- AI explains its reasoning for every action
- Automatically searches all outputs for flag patterns

## Example Workflow

For a web challenge at `https://example-ctf.com/web1`:

1. **Analysis**: AI detects this as a web challenge from URL
2. **Reconnaissance**: Performs initial HTTP requests to understand the application
3. **Discovery**: Enumerates directories, analyzes source code, checks headers
4. **Vulnerability Assessment**: Tests for SQL injection, XSS, file inclusion, etc.
5. **Exploitation**: Attempts to exploit discovered vulnerabilities
6. **Flag Extraction**: Searches all command outputs for the flag pattern
7. **Reporting**: Provides detailed logs of the entire process

## CTF Tools Included

The Docker container includes:

**Web Tools**: curl, wget, burpsuite, gobuster, dirb, nikto
**Programming**: Python 3, Node.js, pip packages (requests, beautifulsoup4, pwntools)
**Crypto**: openssl, hashcat, john, pycryptodome
**Reverse Engineering**: gdb, radare2, ghidra, binutils
**Forensics**: binwalk, foremost, strings, hexdump
**Steganography**: steghide, stegsolve
**Network**: nmap, netcat, telnet, dnsutils

## API Endpoints

- `POST /challenges/start` - Start new challenge
- `GET /challenges/{session_id}` - Get session status
- `GET /challenges/{session_id}/actions` - Get action history
- `POST /challenges/{session_id}/stop` - Stop running session
- `DELETE /challenges/{session_id}` - Delete session
- `WS /ws/{session_id}` - WebSocket for real-time updates

## Security

- Docker containers run with limited privileges
- Non-root user execution
- Network isolation
- Resource limits
- Automatic cleanup of containers

## Development

### Project Structure

```
CTFAgent/
├── backend/           # FastAPI backend
│   ├── main.py       # Main application
│   ├── models.py     # Pydantic models
│   ├── ai_agent.py   # AI agent logic
│   └── docker_manager.py # Docker integration
├── frontend/         # React frontend
│   ├── src/
│   │   ├── components/
│   │   ├── hooks/
│   │   └── types/
├── docker/          # Docker configurations
│   ├── Dockerfile   # CTF tools container
│   ├── Dockerfile-backend
│   └── docker-compose.yml
└── pyproject.toml   # Python dependencies
```

### Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes and test thoroughly
4. Submit a pull request

## Limitations

- Currently optimized for web challenges
- Requires OpenAI API access
- Docker dependency for isolation
- Limited to challenges accessible via HTTP/HTTPS

## Cost Optimization

- Uses GPT-4o-mini (90% cheaper than GPT-4)
- Efficient prompt engineering
- Action batching to reduce API calls
- Configurable timeouts to prevent runaway costs

## License

MIT License - see LICENSE file for details.

## Troubleshooting

**Docker Issues**: Ensure Docker daemon is running and user has permissions
**API Errors**: Verify OpenAI API key and model availability
**WebSocket Disconnections**: Check network connectivity and firewall settings
**Container Failures**: Review Docker logs and resource availability

For more help, check the issues section or create a new issue.
