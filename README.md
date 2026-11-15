# CTF Agent

CTF Agent runs an OpenAI-powered solver in a dedicated Ubuntu Docker container. Submit a URL, a socket host and port, or handout files, then follow commands and output in the browser. The dashboard collects session status, flags, and write-up actions.

## Run locally

Install Docker with Compose, then run these commands from the repository root:

```sh
cp .env.example .env
# Set OPENAI_API_KEY in .env.
docker compose --env-file .env -f docker/docker-compose.yml up --build
```

Open [the workspace](http://localhost:8000), [sessions](http://localhost:8000/dashboard), or [API documentation](http://localhost:8000/docs). The interface uses local assets and supports system, light, and dark themes.

The app is intended for personal, local use. Compose binds to localhost. The backend controls Docker through its socket; it has no authentication and should not be exposed publicly. Solver commands run as `ctfuser`, with a separate workspace volume for each session. Use it for challenges you are authorized to solve.

## Models and settings

`OPENAI_MODEL` defaults to `gpt-6-luna`. The model picker includes GPT-6 Luna, GPT-6.1 Sol, GPT-6.1 Terra, GPT-6 Astra, and the existing GPT-5 presets. Select **Custom model** to enter another API model ID. Custom models must support the Responses API and structured outputs and be available to your API account.

Reasoning defaults to the model's own setting. Presets expose their supported effort levels; invalid combinations are rejected before starting a session. Custom-model settings are ultimately validated by OpenAI. Models are never silently substituted.

`OPENAI_SERVICE_TIER=default` uses Standard processing. Set it to `flex` for Flex processing where your model supports it. Requests and retries remain bounded by the session time limit; the app does not fall back to a more expensive tier.

Cost estimates use response usage and published model rates, including cached input and Flex adjustments. They are estimates, not invoices; custom models or unrecognized billing details display **Unavailable**. The session total covers solving, not subsequent write-up generation.

## Solver images

The standard image uses Ubuntu 24.04, a Python virtual environment, and Node.js 24. It includes curl, wget, nmap, netcat, tcpdump, tshark, OpenSSL, hashcat, john, binutils, LLVM, gdb, binwalk, foremost, steghide, archive tools, build tools, and PostgreSQL client utilities. Python libraries include pwntools, angr, Capstone, Z3, cryptography, requests, Scapy, NumPy, and SciPy. GUI tools such as Ghidra and Burp are not bundled.

For SageMath, set `SOLVER_TARGET=sage` in `.env` and rebuild. Sage runs in an isolated conda-forge environment; this image requires substantially more download space and build time. Both build definitions use architecture-native dependencies for ARM64 and AMD64.

To use a registry mirror, set `SOLVER_BASE_IMAGE` to an Ubuntu 24.04-compatible image. For a manual build:

```sh
docker build --target standard -t ctf-agent:latest -f docker/Dockerfile docker
# Optional Sage variant:
docker build --target sage -t ctf-agent:sage -f docker/Dockerfile docker
```

The backend uses `ctf-agent:latest`. Compose tags the selected build target with that name.

## Data and lifecycle

Sessions and their action histories live in memory and disappear when the backend restarts. Uploaded files and the saved flag default remain in the existing `ctf-data` volume when using Compose. Generated write-ups are saved under `data/writeups` on the host. Manual development stores these under the local `data` directory. Do not use `docker compose down -v` if you want to retain the volume.

Stopping a session, reaching its deadline, or finding a flag terminates its solver container. Container workspaces are removed during cleanup. Save a completed session's write-up before restarting the backend.

## Development and tests

Install Python 3.13 and [uv](https://docs.astral.sh/uv/), then:

```sh
uv sync
uv run python -m backend.main
uv run pytest -q
```

Build the solver image first and ensure Docker is running. Missing credentials or unavailable Docker leave the UI accessible but disable starting challenges. `/health` reports whether the agent initialized.

Dependencies are maintained in `pyproject.toml` and locked in `uv.lock`. After changing them, regenerate Docker's requirements:

```sh
uv lock
uv export --no-dev --no-emit-project --format requirements-txt -o requirements.txt
```

The tests mock OpenAI and Docker by default and do not make paid requests. The frontend consists of Jinja templates, a local stylesheet, shared utilities, and separate workspace/dashboard scripts.

## API

- `GET /models`: presets, capabilities, prices, and configured defaults.
- `POST /challenges/start`: multipart challenge submission, including optional model, reasoning effort, and handouts.
- `GET /challenges`: sessions; optionally filter by status.
- `GET /challenges/{session_id}`: session metadata, usage, and actions.
- `GET /challenges/{session_id}/actions`: action history.
- `POST /challenges/{session_id}/upload`: add a handout.
- `POST /challenges/{session_id}/stop`: stop a pending or running session.
- `POST /challenges/{session_id}/save`: generate a write-up using the selected session model.
- `DELETE /challenges/{session_id}`: delete a session.
- `WS /ws/{session_id}`: live events, supporting multiple viewers.
- `GET/POST /settings/flag-format`: read or update the default flag pattern.

`scripts/ctfd_ingest.py` can submit CTFd challenges. Its model and reasoning arguments are optional, so omitted settings use the backend defaults. Run it with `--help` for options.
