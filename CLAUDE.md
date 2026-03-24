# DaveLLM Router

Local LLM routing server. FastAPI backend (app.py) + vanilla JS frontend (app.js) in Electron shell.

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
