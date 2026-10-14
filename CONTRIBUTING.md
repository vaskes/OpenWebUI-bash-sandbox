# Contributing

Thanks for your interest in making `OpenWebUI-bash-sandbox` better. This
project is small enough that the contribution flow is informal — open an
issue, send a PR, ping the maintainer.

## Development setup

```bash
git clone https://github.com/vaskes/OpenWebUI-bash-sandbox.git
cd OpenWebUI-bash-sandbox
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

The test suite uses **stdlib `unittest`** on purpose — no pytest required.
Run it with:

```bash
make test         # or:  python3 -m unittest discover -s tests -v
```

Lint with:

```bash
make lint         # or:  ruff check .
make format       # or:  ruff format .
```

## Project layout

```
agent_api.py        # FastAPI app — endpoints and tmux plumbing
agent_core.py       # Pure functions: safe_id, tmux_name, constants
openapi_proxy.py    # FastAPI app — OpenAPI surface + WS terminal
proxy_core.py       # Pure constants: command prefixes, timeouts, WebSocket tunables
tests/              # stdlib-unittest suite
docker-compose.yml  # Production-shaped stack
Dockerfile.*        # Per-service image
agent-stack.service # systemd unit
docs/               # ARCHITECTURE, CONFIGURATION, SECURITY
```

Pure functions live in `*_core.py` so they can be unit-tested without
importing FastAPI/Pydantic/httpx. **Keep `*_core.py` dependency-free.**

## Coding style

- Python 3.11+, `from __future__ import annotations` in every module.
- Type hints on every public function and method.
- Docstrings in PEP 257 style for every module and public function.
- `agent_api.py` and `openapi_proxy.py` are the integration layer; try
  to keep business logic out of them and into `*_core.py` instead so it
  can be tested without Docker.
- Prefer explicit error handling over bare `except:`.
- When you change the OpenAPI surface (add an endpoint, change a
  parameter), update `docs/CONFIGURATION.md` and the README's API table.

## Commit messages

This project follows [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(proxy): add /api/version endpoint
fix(agent): strip trailing marker from exec output
docs(readme): add curl quickstart example
test(core): cover unicode in safe_id
chore(deps): bump fastapi to 0.118
```

## Pull-request flow

1. Fork the repo, branch from `main`.
2. Make your change, add tests for any new pure-function logic.
3. `make test lint format` locally — all three must pass.
4. Open a PR with a one-line summary and link to any issue it closes.
5. Wait for a review. The maintainer merges on approval.

## Reporting bugs

Open a GitHub issue with:

- What you did
- What you expected
- What happened
- Output of `docker compose ps` and `docker logs <failing-container>`

## Security issues

**Do not** open a public GitHub issue for security bugs. See
[docs/SECURITY.md](docs/SECURITY.md) for the threat model and contact
the maintainer directly.
