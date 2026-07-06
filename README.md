# DaveLLM Desktop (Electron + FastAPI)

## Prereqs
- Python 3.x
- Node.js (npm)
- Local models in `models/` (or use the built-in Hugging Face downloader)

## Setup
```bash
# Python
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Node/Electron
npm install
```

## Run (desktop shell)
```bash
npm start
```
This spawns uvicorn (FastAPI backend) and opens the Electron window pointed at `http://127.0.0.1:8000`.

## Config
- `DAVE_PORT` to change backend port (default 8000)
- `DAVE_PYTHON` to override Python executable (default `venv/bin/python`)
- `DAVE_DATA_DIR` to relocate conversations/logs/vectors (default current dir)

## Models
Place GGUFs under `models/` or use the UI Hugging Face downloader (`Model Fetcher`). Ensure paths/nodes are configured in `app.py` if you add more models.

## Notes
- On macOS, if port 8000 is busy, set `DAVE_PORT=8010` before `npm start`.
- The app uses whisper.cpp (bundled) for audio transcription; ensure `ffmpeg` is installed.
- Routing base in the UI respects `window.__API_BASE__` injected by Electron preload.
