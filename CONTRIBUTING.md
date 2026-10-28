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

## Mutation checks

Some of the strongest tripwires in this repository are the mutation
checks in `tools/`. Each script breaks **one** property of the code on
purpose and expects the **named** test to go red:

```bash
make mutation     # or:  for s in tools/mutation-check-*.sh; do bash "$s"; done
```

The point is not the mutation but the *counter-test*: a check that
cannot be made red is decoration, and decoration rots silently. Four
of these patterns had drifted away from the code, so their mutations
stopped applying, and the scripts were not run anywhere at all — the
only way to find out was to run them by hand.

They run in CI (the **Mutation checks** job) on every push, and a test
(`tests/test_mutation_scripts_v2141.py`) checks that every pattern in
every script is still in the code. When you move code around, run
`make mutation` before you push: a script that reports `SKIP … pattern
not found` is a broken tripwire, and it fails the same way in CI.

### What a line of the output means

| line | meaning |
|------|---------|
| `OK …` | the named test ran and went red — the mutation was caught |
| `FAIL …` | the named test stayed green — the property is not pinned |
| `INVALID …` | the test did not run at all — the selector matched nothing |
| `SKIP …` | the pattern was not in the code — the mutation did not apply |

Only `OK` is a catch. `INVALID` and `SKIP` are failures: they mean the
script proved nothing, and they fail the run.

### The interpreter

The scripts need a python **with pytest**. They look for one themselves
(`python3`, then `python`), and `PY` overrides the choice:

```bash
PY=/path/to/python-with-pytest bash tools/mutation-check-docs.sh
```

`PY` is honoured only when it can `import pytest`. If no interpreter has
pytest, the script exits **2** before it touches the code: it does not
fall back to a python without pytest and count the resulting "command not
found" as a caught mutation. That is exactly how two of these scripts
used to report ten and eleven catches for tests that never started —
the release notes counted them as caught, and nobody noticed for a
release.

## Project layout

```
agent_api.py        # FastAPI app — endpoints and tmux plumbing
agent_core.py       # Pure functions: safe_id, tmux_name, constants
agent_config.py     # Every knob, read from the environment once and validated once
mcp_server.py       # The MCP server: the same tools over the MCP protocol
body_limit.py       # Pure-ASGI body limiter (413), shared by both services
json_logging.py     # JSON log formatting
metrics.py          # Prometheus counters and gauges
openapi_proxy.py    # FastAPI app — OpenAPI surface + WS terminal
proxy_core.py       # Pure constants: command prefixes, timeouts, WebSocket tunables
tests/              # stdlib-unittest suite
docker-compose.yml  # Production-shaped stack
Dockerfile.*        # Per-service image
agent-stack.service.example # systemd unit template
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
