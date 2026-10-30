# OpenWebUI-bash-sandbox — developer Makefile
# Run `make help` for a list of targets.

SHELL := /usr/bin/env bash
COMPOSE := docker compose

# The workspace is a filesystem with a ceiling. Docker cannot cap a
# volume, so tools/setup-workspace.sh makes an ext4 filesystem in a file
# of AGENT_WORKSPACE_SIZE and mounts it, and the stack's volume binds
# that mount point.
#
# Making it needs root (mount(2) does); checking it does not. `make up`
# checks it, because a workspace that was never set up is an unbounded
# directory on the host's disk -- which is exactly the hole the ceiling
# closes. The path below is the same default the compose file uses, so
# the two cannot drift apart.
WORKSPACE_PATH ?= /opt/owbs-workspace
WORKSPACE_SIZE ?= 1g

.PHONY: help build up down logs ps restart test test-cov mutation lint format clean health check-env workspace workspace-check

help: ## Show this help message
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

build: check-env ## Build all Docker images (refuses to run with placeholder .env)
	$(COMPOSE) build

# --build is not optional. Neither service pins an image, so `up -d` reuses
# whatever tag is already present and silently serves the previous code.
# Verified 2026-10-20: after editing agent_core.py, plain `up -d` kept the
# old version while `up -d --build` picked the change up immediately.
# Layer caching keeps this cheap when nothing changed.
up: check-env workspace-check ## Start the full stack in the background (refuses with placeholder .env)
	AGENT_WORKSPACE_PATH="$(WORKSPACE_PATH)" $(COMPOSE) up -d --build
	@echo "Waiting for services to become healthy…"
	@$(COMPOSE) ps

# The workspace is a filesystem with a ceiling, and it lives on the
# host: an ext4 filesystem in a file, mounted, and handed to the stack
# as a volume. Run this once per host (it needs root), or whenever the
# ceiling changes -- which wipes the workspace, because a ceiling is
# the size of the filesystem and a different size is a different
# filesystem.
workspace: ## Give the sandbox its workspace: a filesystem with a ceiling (needs sudo)
	sudo bash tools/setup-workspace.sh "$(WORKSPACE_PATH)" "$(WORKSPACE_SIZE)"

# The same question as `make workspace`, without root and without
# changing anything: is the workspace really there, and is its ceiling
# the one that was asked for?
workspace-check: ## Check that the workspace is mounted with the right ceiling
	bash tools/setup-workspace.sh --check "$(WORKSPACE_PATH)" "$(WORKSPACE_SIZE)"

down: ## Stop and remove containers
	$(COMPOSE) down

logs: ## Tail logs from all services
	$(COMPOSE) logs -f --tail=100

ps: ## Show service status
	$(COMPOSE) ps

# --force-recreate without --build is the most misleading invocation in this
# file: it rebuilds the containers from the *existing* image, so it looks
# like a fresh restart while serving the code from the previous deploy.
restart: check-env workspace-check ## Recreate all containers (refuses with placeholder .env)
	AGENT_WORKSPACE_PATH="$(WORKSPACE_PATH)" $(COMPOSE) up -d --build --force-recreate

test: ## Run the unit test suite (stdlib unittest runner; needs requirements-dev.txt)
	@python3 -m unittest discover -s tests -v

test-cov: ## Run tests with coverage (requires pytest + pytest-cov)
	@pytest --cov=. --cov-report=term-missing tests/

# Every mutation check breaks one property of the code on purpose and
# expects the named test to go red. A check that cannot be made red is
# decoration, and decoration rots: the patterns drift away from the
# code, the mutation stops applying, and the script writes SKIP into a
# line nobody reads. They run in CI too (the "Mutation checks" job).
mutation: ## Run the mutation checks (each tripwire must be able to go red)
	@for script in tools/mutation-check-*.sh; do \
		echo "== $$script"; \
		PY="$$(command -v python3)" bash "$$script" || exit 1; \
	done

lint: ## Lint with ruff (requires `pip install ruff`)
	@ruff check .

format: ## Format with ruff (requires `pip install ruff`)
	@ruff format .

clean: ## Remove pyc files and __pycache__
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete

health: ## Curl every public endpoint
	@echo "--- sandbox-agent /healthz"
	@curl -sS http://localhost:8082/healthz | python3 -m json.tool
	@echo "--- openapi-proxy /healthz"
	@curl -sS http://localhost:8090/healthz | python3 -m json.tool
	@echo "--- openapi-proxy /openapi.json (paths only)"
	@curl -sS http://localhost:8090/openapi.json | python3 -c "import sys,json; spec=json.load(sys.stdin); print('\n'.join(f'  {m.upper():6} {p}' for p,methods in spec.get('paths',{}).items() for m in methods if m in ('get','post','put','delete')))"
	@echo "--- bash tool smoke test"
	@curl -sS -X POST http://localhost:8090/bash -H "Content-Type: application/json" -d '{"command":"pwd && whoami"}' | python3 -m json.tool


check-env: ## Fail if .env still has CHANGE_ME placeholders
	@if [ ! -f .env ]; then \
		echo "ERROR: .env does not exist. Copy .env.example to .env and edit it." >&2; \
		exit 1; \
	fi
	@if grep -E '^[A-Z_]+=(CHANGE_ME|CHANGE_ME_)' .env >/dev/null 2>&1; then \
		echo "ERROR: .env still contains CHANGE_ME placeholders. Open .env and replace them." >&2; \
		grep -E '^[A-Z_]+=(CHANGE_ME|CHANGE_ME_)' .env >&2; \
		exit 1; \
	fi
	@if grep -E '^OPENAPI_PROXY_TOKEN=[[:space:]]*$$' .env >/dev/null 2>&1; then \
		echo "ERROR: OPENAPI_PROXY_TOKEN is empty." >&2; \
		echo "       Generate one with: openssl rand -hex 32" >&2; \
		exit 1; \
	fi
	@if LC_ALL=C grep -E '^OPENAPI_PROXY_TOKEN=.*[^[:print:]]' .env >/dev/null 2>&1 \
		|| LC_ALL=C grep -E '^OPENAPI_PROXY_TOKEN=.*(["\\]|[[:space:]])' .env >/dev/null 2>&1; then \
		echo "ERROR: OPENAPI_PROXY_TOKEN contains a character that breaks its use." >&2; \
		echo "       The token is embedded into the Open WebUI tool connection as JSON" >&2; \
		echo "       and travels in an HTTP header: a quote, a backslash, whitespace or" >&2; \
		echo "       a non-ASCII character breaks one of them, and the failure is quiet." >&2; \
		echo "       Regenerate the token with: openssl rand -hex 32" >&2; \
		exit 1; \
	fi
