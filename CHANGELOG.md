# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `agent_core` and `proxy_core` modules isolate pure-function logic for unit testing
- Comprehensive unit tests for `safe_id`, `tmux_name`, and proxy prefix constants
- `Makefile` with `build`/`up`/`test`/`lint`/`health` targets
- `requirements.txt` and `requirements-dev.txt` for reproducible installs
- `.editorconfig` for consistent formatting across editors
- `.dockerignore` to keep build contexts small
- `docs/ARCHITECTURE.md` with detailed design notes
- `docs/CONFIGURATION.md` with all env vars, volumes, and ports documented
- `docs/SECURITY.md` describing the security model and threat surface
- `CHANGELOG.md` (this file)
- `CODE_OF_CONDUCT.md` and `CONTRIBUTING.md`

### Changed
- Type hints, docstrings, and error handling hardened across both services
- `agent_api.py` rewritten with explicit return types, structured error responses
- `openapi_proxy.py` split dispatch into `_do_web_search` and `_do_exec` for readability
- `Dockerfile.sandbox` and `Dockerfile.openapi-proxy` use pinned `requirements.txt`
- `WebSocket` endpoint in `openapi_proxy.py` now has an explicit shutdown event

### Fixed
- SSH command prefix correctly strips the original `ssh ` before injecting `-o StrictHostKeyChecking=accept-new`
- `web_search` errors are now reported as structured JSON instead of raising

## [1.0.0] - 2026-09-13

### Added
- Initial three-container stack: Open WebUI + sandbox-agent + openapi-proxy
- Single `bash` tool exposed via OpenAPI 3.0
- Persistent tmux sessions per chat session_id
- Persistent named volumes for `/root`, apt cache, pip cache
- systemd unit `agent-stack.service` for autostart on boot
- Auto-wired Tool Server connection in `TOOL_SERVER_CONNECTIONS` env var
- WebSocket `/ws` terminal endpoint (experimental)

[Unreleased]: https://github.com/vaskes/OpenWebUI-bash-sandbox/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/vaskes/OpenWebUI-bash-sandbox/releases/tag/v1.0.0
