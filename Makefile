# OpenWebUI-bash-sandbox — developer Makefile
# Run `make help` for a list of targets.

SHELL := /usr/bin/env bash
COMPOSE := docker compose

.PHONY: help build up down logs ps restart test test-cov lint format clean health check-env

help: ## Show this help message
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

build: check-env ## Build all Docker images (refuses to run with placeholder .env)
	$(COMPOSE) build

# --build is not optional. Neither service pins an image, so `up -d` reuses
# whatever tag is already present and silently serves the previous code.
# Verified 2026-10-20: after editing agent_core.py, plain `up -d` kept the
# old version while `up -d --build` picked the change up immediately.
# Layer caching keeps this cheap when nothing changed.
up: check-env ## Start the full stack in the background (refuses with placeholder .env)
	$(COMPOSE) up -d --build
	@echo "Waiting for services to become healthy…"
	@$(COMPOSE) ps

down: ## Stop and remove containers
	$(COMPOSE) down

logs: ## Tail logs from all services
	$(COMPOSE) logs -f --tail=100

ps: ## Show service status
	$(COMPOSE) ps

# --force-recreate without --build is the most misleading invocation in this
# file: it rebuilds the containers from the *existing* image, so it looks
# like a fresh restart while serving the code from the previous deploy.
restart: check-env ## Recreate all containers (refuses with placeholder .env)
	$(COMPOSE) up -d --build --force-recreate

test: ## Run the unit test suite (stdlib unittest runner; needs requirements-dev.txt)
	@python3 -m unittest discover -s tests -v

test-cov: ## Run tests with coverage (requires pytest + pytest-cov)
	@pytest --cov=. --cov-report=term-missing tests/

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
