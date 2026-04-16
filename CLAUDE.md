# DaveLLM Router

## Project Overview

Local LLM routing server that load-balances inference requests across a 4-node LAN Ollama cluster. FastAPI backend handles routing and model selection. Electron desktop UI provides the chat interface.

## Stack

- Python (FastAPI, uvicorn, httpx, numpy, pydantic)
- SQLite for vector memory (`dave_vectors.db`) and feedback/performance DBs
- Electron 30 with vanilla JS frontend (no React, no frontend framework)
- Ollama on 4 LAN inference nodes (port 11434 each)

## Key Decisions

- Mac runs the router only. All inference happens on dedicated LAN GPU nodes.
- Tool execution uses `shlex` (no `shell=True`) for security.
- Thread-safe node selection via `itertools.cycle` round-robin.
- Prompt length capped at 100k characters. PII stripped from system prompts sent to inference nodes.
- No cloud dependency when local nodes are available. Ollama localhost:11434 as fallback.

## Documentation Maintenance

- **Issues**: Track in CLAUDE.md issue tracker table below
- **Session log**: Append to `/Users/daverobertson/Desktop/Code/95-docs-personal/today.csv` after each meaningful change

## Issue Tracker

| ID | Severity | Status | Title | Notes |
|----|----------|--------|-------|-------|

## Architecture

```
Electron Desktop UI (app.js + index.html + style.css)
    |
FastAPI Router (app.py, port 8000)
    |
Ollama nodes on LAN (ports 11434 per machine)
    |
Local GGUF/Ollama models
```

## Multi-Node Cluster

4 inference nodes on LAN, all running Ollama:
- **node-gp66**: MSI GP66 Leopard, RTX 3070 — fast general (7B)
- **node-katana-1**: MSI Katana, RTX 4060 — code model
- **node-katana-2**: MSI Katana, RTX 4060 — vision model
- **node-duncan**: Duncan workstation, 80 GB RAM — large model (70B, CPU inference)

Mac runs the router only. Ollama localhost:11434 available as fallback.

## Running

```bash
# Router (Mac)
source .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000

# Desktop UI
cd desktop && npx electron .
```

## Node config

Set `DAVE_NODES` env var as JSON array, or edit DEFAULT_NODES in app.py.
Each node runs Ollama on port 11434 with `OLLAMA_HOST=0.0.0.0`.

## Key constraints

- No cloud dependency when local nodes are available
- Prompt length validated (100k char max)
- Tool execution uses shlex (no shell=True)
- Thread-safe node selection
- PII removed from system prompt sent to inference nodes
