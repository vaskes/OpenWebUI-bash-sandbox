# Credits

OpenWebUI-bash-sandbox is the result of collaboration between an AI agent,
an AI code-reviewer, and a human operator.

## Authors

- **Developer** — Mavis Agent (powered by MiniMax M3)
- **Code reviews** — Qwen 3.8-Max
- **Vibecoding** — Vaskes
- **Network infrastructure** — Vaskes
- **Human testing** — Vaskes
- **Deployment & integration** — Vaskes

## Acknowledgements

The architecture follows the **Open WebUI External Tool Servers** contract;
the LLM-facing surface area is exactly what Open WebUI's `TOOL_SERVER_CONNECTIONS`
env var expects — no custom Open WebUI patches required.

`uvicorn`, `FastAPI`, `pydantic`, and `httpx` are the runtime backbone.
`tmux` is the session backend — chosen for its ability to keep a shell
process alive across HTTP requests without any daemon code on our side.

## Origin

This project started as a local helper for one operator (Vaskes) who wanted
to give an LLM a real shell on his own infrastructure without the LLM
touching anything outside the `/opt/agent-workspace` directory. The
multi-wave code review (Oct 2026 — wave 1 baseline, waves 2-3 security audit, sandbox-recommendations round from the bot-side Qwen review, and a release-prep follow-up) was a deliberate exercise: take a
working pet project and harden it until it is fit for production use
without re-architecting it into something different.
