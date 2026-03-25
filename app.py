"""
DaveLLM Router — Persistent Chat Router with Conversation Metadata
Enhanced with titles, timestamps, atomic persistence, and vector memory
"""

from __future__ import annotations

import json
import time
import asyncio
import os
import hashlib
import sqlite3
import threading
import shutil
import numpy as np
from itertools import cycle
from typing import List, Dict, Optional
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime

import requests
import re
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
import httpx
import uuid

# ============================================================
# CONSTANTS
# ============================================================

BASE_DIR = Path(os.getenv("DAVE_DATA_DIR", ".")).expanduser().resolve()
DATA_FILE = BASE_DIR / "dave_conversations.json"
DEFAULT_CONVO_TITLE = "New Conversation"
VECTOR_DB = BASE_DIR / "dave_vectors.db"
FEEDBACK_DB = Path("feedback.db")
PERFORMANCE_DB = Path("performance.db")
PROJECTS_FILE = Path("dave_projects.json")
BUDGET_DEFAULT = float(os.getenv("DAVE_BUDGET_DEFAULT", "100"))
USER_BUDGETS = {}
if os.getenv("DAVE_USER_BUDGETS"):
    try:
        USER_BUDGETS = json.loads(os.getenv("DAVE_USER_BUDGETS", "{}"))
    except Exception:
        USER_BUDGETS = {}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_TOOL_OUTPUT = 5000
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB base64 ≈ 3.75MB binary
MAX_AUDIO_SIZE = 20 * 1024 * 1024  # 20MB
WHISPER_BIN = Path(os.getenv("DAVE_WHISPER_BIN", "./whisper.cpp/build/bin/whisper-cli"))
WHISPER_MODEL = Path(os.getenv("DAVE_WHISPER_MODEL", "./whisper.cpp/models/ggml-tiny.en.bin"))
FFMPEG_BIN = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

DEFAULT_MODEL_ID = (
    "/Users/daverobertson/Desktop/Dave-LLM/models/qwen-vl-7b/"
    "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
)

# Simple model catalog for routing decisions
MODEL_CATALOG = {
    str(Path("/Users/daverobertson/Desktop/Dave-LLM/models/qwen-vl-7b/Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf").resolve()): {
        "vision": True,
        "cost_per_1k": 0.0015,
        "quality": 0.9,
        "node": "qwen-node",
    },
    str(Path("./models/llama3.2-3b-instruct-q4_k_m.gguf").resolve()): {
        "vision": False,
        "cost_per_1k": 0.0006,
        "quality": 0.8,
        "node": "mac-node",
    },
}

MODEL_HEALTH: Dict[str, dict] = {}
RECENT_ERRORS: List[dict] = []

def track_model_failure(model_id: str, error_type: str):
    if model_id not in MODEL_HEALTH:
        MODEL_HEALTH[model_id] = {"failures": 0, "last_error": None}
    MODEL_HEALTH[model_id]["failures"] += 1
    MODEL_HEALTH[model_id]["last_error"] = error_type

def record_error(event: str, detail: str):
    RECENT_ERRORS.append({
        "event": event,
        "detail": detail,
        "timestamp": datetime.now().isoformat()
    })
    if len(RECENT_ERRORS) > 50:
        del RECENT_ERRORS[:-50]

# CORS / Auth
DEFAULT_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
]
API_KEY = os.getenv("DAVE_API_KEY")
USER_DB: Dict[str, str] = {}  # api_key -> user_id (placeholder; default user if none)
RAW_CORS = os.getenv("DAVE_CORS_ORIGINS")
ALLOWED_ORIGINS = (
    [o.strip() for o in RAW_CORS.split(",") if o.strip()]
    if RAW_CORS
    else DEFAULT_ORIGINS
)

SYSTEM_PROMPT = (
    "🧠 DaveLLM System Message — Master Version\n\n"
    "You are DaveLLM, the personal, professional, and technical assistant to Dave Robertson. "
    "Your job is to operate as an extension of Dave's brain: fast, capable, structured, deeply technical, and context-aware.\n\n"
    
    "USER IDENTITY\n"
    "The user is a broad technical specialist across AV/video, lighting, networking, "
    "local AI, home automation, and software engineering. High-bandwidth operator who "
    "switches domains quickly and expects the assistant to keep up.\n\n"
    
    "YOUR ROLE\n"
    "You act as:\n"
    "• Executive assistant\n"
    "• Technical consultant\n"
    "• Systems engineer\n"
    "• AI product designer\n"
    "• Senior developer & code generator\n"
    "• Project manager\n"
    "• Research partner\n"
    "• AV/lighting/networking troubleshooter\n"
    "• Documentation writer\n"
    "• Workflow optimizer\n"
    "• Model selector, tuner, and orchestrator\n"
    "• Memory system that tracks context, prior decisions, and ongoing projects\n\n"
    
    "GENERAL PRINCIPLES\n"
    "1. Response Style:\n"
    "• Clear, direct, no fluff or filler\n"
    "• No unnecessary disclaimers\n"
    "• Assume Dave is an expert and talk to him like one\n"
    "• Keep responses tight unless he asks for elaboration\n"
    "• When giving options, compare them succinctly\n"
    "• Use structured lists when clarity matters\n"
    "• Use short paragraphs, not walls of text\n\n"
    
    "2. Tone:\n"
    "• Conversational but sharp\n"
    "• Confident but not arrogant\n"
    "• Friendly but not goofy\n"
    "• Respect Dave's speed: he moves fast and so should you\n"
    "• Avoid over-explaining basic concepts\n"
    "• Avoid dumbing anything down\n\n"
    
    "3. Reasoning & Problem Solving:\n"
    "• Think like a field tech, programmer, systems engineer, and production LD simultaneously\n"
    "• Give root-cause analysis first\n"
    "• Then provide the solution steps\n"
    "• Then provide the optimization or future-proof notes\n"
    "• Never stop at the surface layer\n\n"
    
    "4. Code:\n"
    "• Clean, commented, production-ready\n"
    "• No placeholders unless explicitly unavoidable\n"
    "• Use Dave's preferred style (Python, JS, YAML, Bash, HTML, CSS, C++, etc.)\n"
    "• Follow best practices for the relevant environment\n\n"
    
    "5. Context Awareness:\n"
    "Track and understand:\n"
    "• The DaveLLM Router\n"
    "• Local llama.cpp nodes (multiple machines)\n"
    "• Streaming endpoints\n"
    "• Model IDs, quantization formats, performance characteristics\n"
    "• Home Assistant architecture and YAML designs\n"
    "• Lansdowne Theater lighting plots, show file workflows\n"
    "• Networking domains, VLANs, addressing, subnets\n"
    "• 3D printing workflow (MakerWorld + Bambu)\n"
    "• Project naming conventions, Notion structures, task trees\n"
    "When Dave refers to 'the node,' 'the router,' 'the UI,' 'the gig,' 'the cluster,' 'the garage,' 'the theater,' etc., "
    "assume conversation continuity from prior messages.\n\n"
    
    "TECHNICAL EXPECTATIONS\n"
    "Deliver high-quality responses in:\n"
    "• Local AI & LLM Engineering (llama.cpp, GGUF, quantization, multimodal models, tool-calling, vector DBs)\n"
    "• Networking (VLAN design, IP planning, DNS, DHCP, router/AP stacking, PoE, home lab topologies)\n"
    "• AV / Video / Live Production (LED walls, signal flow, routing, power distribution, camera shading, playback, switching)\n"
    "• Lighting (ETC Ion/Element/Eos, GrandMA2/MA3, DMX universes, addressing, cue stacks, tracking, palettes)\n"
    "• Home Assistant (YAML scripts, REST sensors, automations, dashboard UI, ESPHome, MQTT)\n"
    "• DevOps & Coding (FastAPI, Node.js, Python, SQLite/Postgres, web development, systems architecture)\n"
    "• Business / Project / Notion (invoices, proposals, emails, project tracking, templates, contingency planning)\n\n"
    
    "SPECIAL INSTRUCTIONS\n"
    "• Rewrite Mode: Keep Dave's voice, fix grammar/clarity/pacing, confident/warm/human, no robotic phrasing, no unnecessary hyphens\n"
    "• Work Mode: Formal, concise, professional\n"
    "• Friends Mode: Looser, witty, high charisma, warm, lightly playful\n"
    "• Speculation / Theory Mode: Label assumptions clearly\n"
    "• Never repeat instructions back to Dave. Just execute."
)

TEMPLATES = {
    "general": {
        "title": "New Conversation",
        "system_prompt": SYSTEM_PROMPT,
        "preferred_model": DEFAULT_MODEL_ID,
    },
    "code_review": {
        "title": "Code Review Session",
        "system_prompt": SYSTEM_PROMPT + "\n\nFocus on code quality, bugs, and optimization.",
        "preferred_model": "./models/llama3.2-3b-instruct-q4_k_m.gguf",
    },
    "brainstorm": {
        "title": "Brainstorm",
        "system_prompt": SYSTEM_PROMPT + "\n\nBe exploratory and propose multiple options.",
        "preferred_model": "/Users/daverobertson/Desktop/Dave-LLM/models/qwen-vl-7b/Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",
    },
}

# ============================================================
# DATA PERSISTENCE (with auto-migration)
# ============================================================

def migrate_conversation(conversation_id: str, data):
    """
    Upgrade old-style conversations (list of messages)
    into new structured format with metadata.
    """
    if isinstance(data, list):
        # Old format — upgrade to new schema
        return {
            "title": DEFAULT_CONVO_TITLE,
            "messages": data,
            "created_at": None,
            "updated_at": None,
        }

    # Already new format
    return data

def load_conversations() -> Dict[str, dict]:
    """Load & migrate conversation history from disk."""
    if not DATA_FILE.exists():
        return {}

    try:
        with open(DATA_FILE, "r") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"⚠️ Failed to load conversations: {e}")
        return {}

    migrated = {}
    for conversation_id, convo in raw.items():
        migrated[conversation_id] = migrate_conversation(conversation_id, convo)

    return migrated

def save_conversations(conversations: Dict[str, dict]):
    """Atomic write for safety — prevents corruption on crash."""
    tmp = DATA_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(conversations, f, indent=2)
        tmp.replace(DATA_FILE)  # Atomic rename
    except IOError as e:
        print(f"⚠️ Failed to save conversations: {e}")

def load_projects() -> Dict[str, dict]:
    if not PROJECTS_FILE.exists():
        return {}
    try:
        with open(PROJECTS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Failed to load projects: {e}")
        return {}

def save_projects(projects: Dict[str, dict]):
    tmp = PROJECTS_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(projects, f, indent=2)
        tmp.replace(PROJECTS_FILE)
    except IOError as e:
        print(f"⚠️ Failed to save projects: {e}")

# Load existing conversations on startup
CONVERSATIONS: Dict[str, dict] = load_conversations()
PROJECTS: Dict[str, dict] = load_projects()

# ============================================================
# FASTAPI SETUP
# ============================================================

app = FastAPI(
    title="DaveLLM Router",
    version="2.1",
    description="Routes chat prompts to llama.cpp nodes with persistent conversation memory and metadata.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ============================================================
# RATE LIMITING
# ============================================================

RATE_LIMIT_WINDOW = int(os.getenv("DAVE_RATE_WINDOW", "60"))  # seconds
RATE_LIMIT_MAX = int(os.getenv("DAVE_RATE_MAX", "30"))  # requests per window
_rate_buckets: Dict[str, list] = {}

def check_rate_limit(client_ip: str):
    """Simple sliding-window rate limiter per IP."""
    now = time.time()
    bucket = _rate_buckets.setdefault(client_ip, [])
    # Prune expired entries
    _rate_buckets[client_ip] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW]
    bucket = _rate_buckets[client_ip]
    if len(bucket) >= RATE_LIMIT_MAX:
        raise HTTPException(429, "Rate limit exceeded. Try again shortly.")
    bucket.append(now)


# ============================================================
# NODE CONFIG
# ============================================================


def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    """
    Optional API key guard. If DAVE_API_KEY is unset, endpoint stays open.
    """
    if API_KEY is None:
        return "default"
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API key")
    return USER_DB.get(x_api_key, "default")

def get_current_user(user_id: str = Depends(require_api_key)) -> str:
    return user_id or "default"

def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid node URL: {url}")
    return url.rstrip("/")

class NodeConfig(BaseModel):
    id: str
    name: str
    url: str

    @field_validator("url")
    @classmethod
    def normalize(cls, v: str) -> str:
        return _validate_url(v)

# Node config — update IPs to match your LAN
# Env override: DAVE_NODES='[{"id":"gp66","name":"GP66","url":"http://x.x.x.x:9001"}]'
_env_nodes = os.getenv("DAVE_NODES")
if _env_nodes:
    try:
        DEFAULT_NODES = [NodeConfig(**n) for n in json.loads(_env_nodes)]
    except Exception:
        DEFAULT_NODES = []
else:
    DEFAULT_NODES = [
        NodeConfig(
            id="node-gp66",
            name="GP66 Leopard — Llama 3.2 7B (fast general)",
            url="http://PLACEHOLDER_GP66_IP:11434",
        ),
        NodeConfig(
            id="node-katana-1",
            name="Katana 1 — Code Model (code gen)",
            url="http://PLACEHOLDER_KATANA1_IP:11434",
        ),
        NodeConfig(
            id="node-katana-2",
            name="Katana 2 — Qwen VL 7B (vision)",
            url="http://PLACEHOLDER_KATANA2_IP:11434",
        ),
        NodeConfig(
            id="node-duncan",
            name="Duncan — Llama 3.1 70B (quality)",
            url="http://PLACEHOLDER_DUNCAN_IP:11434",
        ),
    ]

NODE_CONFIGS: List[NodeConfig] = DEFAULT_NODES
NODE_CYCLE = cycle(NODE_CONFIGS)
_node_lock = threading.Lock()

def choose_node() -> NodeConfig:
    """Thread-safe round-robin selection of nodes."""
    if not NODE_CONFIGS:
        raise HTTPException(503, "No nodes configured")
    with _node_lock:
        try:
            return next(NODE_CYCLE)
        except StopIteration:
            raise HTTPException(503, "No nodes available")

def get_node_by_id(node_id: str) -> NodeConfig:
    node = next((n for n in NODE_CONFIGS if n.id == node_id), None)
    if not node:
        raise HTTPException(404, f"Node '{node_id}' not found")
    return node

from collections import defaultdict
MODEL_FEEDBACK = defaultdict(list)  # cached; persisted to SQLite

def load_feedback_cache():
    conn = sqlite3.connect(str(FEEDBACK_DB))
    c = conn.cursor()
    c.execute("SELECT model_id, score, complexity FROM feedback ORDER BY id DESC LIMIT 500")
    for model_id, score, complexity in c.fetchall():
        MODEL_FEEDBACK[model_id].append({"score": score, "complexity": complexity})
    conn.close()

load_feedback_cache()

def log_performance(user_id: str, conversation_id: str, model_id: str, tokens: int, cost: float, latency_ms: float, complexity: float):
    try:
        conn = sqlite3.connect(str(PERFORMANCE_DB))
        c = conn.cursor()
        c.execute(
            "INSERT INTO performance (user_id, conversation_id, model_id, tokens, cost, latency_ms, complexity) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, conversation_id, model_id, tokens, cost, latency_ms, complexity),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Failed to log performance: {e}")

def get_user_budget(user_id: str) -> float:
    return float(USER_BUDGETS.get(user_id, BUDGET_DEFAULT))

def get_user_spent(user_id: str) -> float:
    try:
        conn = sqlite3.connect(str(PERFORMANCE_DB))
        c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(cost), 0) FROM performance WHERE user_id = ?", (user_id,))
        spent = c.fetchone()[0] or 0.0
        conn.close()
        return float(spent)
    except Exception as e:
        print(f"⚠️ Failed to compute spent: {e}")
        return 0.0

def get_project(project_id: str, user_id: str) -> dict:
    if not project_id:
        return {}
    proj = PROJECTS.get(project_id)
    if not proj:
        raise HTTPException(404, f"Project '{project_id}' not found")
    if proj.get("user_id", "default") != user_id:
        raise HTTPException(403, "Forbidden: project not owned by user")
    return proj

def list_projects_for_user(user_id: str) -> List[dict]:
    return [p for p in PROJECTS.values() if p.get("user_id", "default") == user_id]

def estimate_tokens(text: str) -> int:
    # Rough heuristic: 1 token ~ 4 chars
    return max(1, len(text) // 4)

def get_model_meta(model_id: str) -> dict:
    return MODEL_CATALOG.get(model_id, {})

def complexity_score(prompt: str, convo_len: int) -> float:
    token_est = estimate_tokens(prompt)
    has_code = "```" in prompt or ("{" in prompt and "}" in prompt)
    has_questions = prompt.count("?") >= 2
    is_long = token_est > 200
    deep_convo = convo_len > 10
    score = 0.0
    if has_code:
        score += 0.3
    if has_questions:
        score += 0.2
    if is_long:
        score += 0.2
    if deep_convo:
        score += 0.3
    return min(score, 1.0)


def choose_model_for_prompt(prompt: str, prefs: RoutePreferences, conversation_length: int = 0) -> RouteDecisionResponse:
    """
    Enhanced router: cost/quality/vision + prompt complexity + convo length.
    """
    require_vision = prefs.require_vision or False
    max_cost = prefs.max_cost or 0.01
    min_quality = prefs.min_quality or 0.8

    comp_score = complexity_score(prompt, conversation_length)
    adjusted_quality = min(1.0, min_quality + comp_score * 0.2)
    token_est = estimate_tokens(prompt)

    candidates = []
    # Incorporate feedback-adjusted quality
    def adjusted_quality_for_model(model_id: str, base_quality: float) -> float:
        feedbacks = MODEL_FEEDBACK.get(model_id, [])
        if not feedbacks:
            return base_quality
        avg_feedback = sum(f.get("score", 0) for f in feedbacks) / len(feedbacks)
        return base_quality * (1 + avg_feedback * 0.2)

    for model_id, meta in MODEL_CATALOG.items():
        if require_vision and not meta.get("vision"):
            continue
        adjusted_q = adjusted_quality_for_model(model_id, meta.get("quality", 0))
        if adjusted_q < adjusted_quality:
            continue
        if meta.get("cost_per_1k", 1) > max_cost:
            continue
        candidates.append((model_id, {**meta, "adjusted_quality": adjusted_q}))

    if not candidates:
        candidates = sorted(MODEL_CATALOG.items(), key=lambda x: x[1].get("quality", 0), reverse=True)
    else:
        candidates = sorted(
            candidates,
            key=lambda x: (-(x[1].get("adjusted_quality", x[1].get("quality", 0)) / max(x[1].get("cost_per_1k", 0.001), 0.0001)))
        )

    chosen_model, chosen_meta = candidates[0]
    confidence = 0.85 if comp_score > 0.5 else 0.7
    reason = f"complexity={comp_score:.2f}, quality={chosen_meta.get('quality', 0)}, cost/1k=${chosen_meta.get('cost_per_1k', 0)}"
    est_cost = chosen_meta.get("cost_per_1k", 0) * (token_est / 1000)

    return RouteDecisionResponse(
        model_id=chosen_model,
        confidence=confidence,
        estimated_cost=est_cost,
        reason=reason,
    )

async def get_node_health(node: NodeConfig) -> dict:
    """Check node health and latency."""
    try:
        start = time.time()
        resp = await asyncio.to_thread(
            requests.get,
            f"{node.url}/health",
            timeout=3
        )
        latency = (time.time() - start) * 1000  # ms

        if resp.ok:
            return {
                "status": "online",
                "latency": round(latency, 1),
                "node_id": node.id,
                "name": node.name
            }
    except Exception:
        pass

    return {
        "status": "offline",
        "latency": None,
        "node_id": node.id,
        "name": node.name
    }

async def get_node_models(node: NodeConfig) -> list:
    """Fetch available models from a node and flag vision capability."""

    def detect_vision(model_obj, model_id: str) -> bool:
        """Heuristic detection of vision-capable models."""
        if not model_id:
            return False

        lid = model_id.lower()
        # Basic filename heuristics, including common "vl" (vision-language) patterns
        if (
            "vision" in lid
            or "multimodal" in lid
            or "mm" in lid
            or "vl" in lid
        ):
            return True

        if isinstance(model_obj, dict):
            caps = model_obj.get("capabilities") or {}
            if isinstance(caps, dict):
                if caps.get("vision") or caps.get("multimodal"):
                    return True

            modalities = model_obj.get("modalities") or model_obj.get("modality")
            if isinstance(modalities, str):
                if any(word in modalities.lower() for word in ("vision", "image", "multimodal")):
                    return True
            if isinstance(modalities, (list, tuple)):
                lower_modalities = [str(m).lower() for m in modalities]
                if any(m in ("vision", "image", "multimodal") for m in lower_modalities):
                    return True

            # Generic flag some backends use
            if model_obj.get("vision") or model_obj.get("supports_vision"):
                return True

        return False

    try:
        resp = await asyncio.to_thread(
            requests.get,
            f"{node.url}/v1/models",
            timeout=3
        )
        if resp.ok:
            data = resp.json()

            # Merge both OpenAI-style "data" and llama.cpp "models" lists
            raw_models = []

            data_models = data.get("data") or []
            file_models = data.get("models") or []

            # Normalize "data" entries (usually already OpenAI-style)
            for m in data_models:
                raw_models.append(m)

            # Normalize "models" entries (llama.cpp file listing)
            for m in file_models:
                if isinstance(m, dict) and "id" not in m:
                    # Promote name/model to id so downstream logic can treat it uniformly
                    m = {
                        "id": m.get("id") or m.get("model") or m.get("name"),
                        **m,
                    }
                raw_models.append(m)

            if raw_models:
                normalized_models = []
                seen_ids = set()

                for m in raw_models:
                    model_id = m
                    vision_capable = False

                    if isinstance(m, dict):
                        model_id = m.get("id") or m.get("model") or m.get("name")
                        vision_capable = detect_vision(m, model_id)
                    else:
                        model_id = str(m)
                        vision_capable = detect_vision({}, model_id)

                    if not model_id:
                        continue

                    if model_id in seen_ids:
                        continue
                    seen_ids.add(model_id)

                    normalized_models.append(
                        {
                            "id": model_id,
                            "vision": vision_capable,
                        }
                    )

                print(
                    f"✅ Fetched {len(normalized_models)} models from {node.name}: "
                    f"{[m['id'] for m in normalized_models]}"
                )
                return normalized_models

            print(f"⚠️ Node {node.name} returned empty model list")
            return []
    except requests.Timeout:
        print(f"⚠️ Node {node.name} timed out fetching models")
    except requests.ConnectionError:
        print(f"⚠️ Cannot connect to {node.name} at {node.url}")
    except Exception as e:
        print(f"⚠️ Error fetching models from {node.name}: {e}")

    return []

# ============================================================
# VECTOR MEMORY STORAGE (SQLite-based)
# ============================================================

def init_vector_db():
    """Initialize SQLite vector database."""
    conn = sqlite3.connect(str(VECTOR_DB))
    c = conn.cursor()
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            id INTEGER PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            message_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(conversation_id, message_index)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_convo_id ON embeddings(conversation_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON embeddings(created_at)")
    
    conn.commit()
    conn.close()

def init_feedback_db():
    """SQLite for feedback/performance."""
    conn = sqlite3.connect(str(FEEDBACK_DB))
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY,
            model_id TEXT NOT NULL,
            score INTEGER,
            complexity REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def init_performance_db():
    """SQLite for performance and cost tracking."""
    conn = sqlite3.connect(str(PERFORMANCE_DB))
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS performance (
            id INTEGER PRIMARY KEY,
            user_id TEXT,
            conversation_id TEXT,
            model_id TEXT,
            tokens INTEGER,
            cost REAL,
            latency_ms REAL,
            complexity REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_simple_embedding(text: str) -> np.ndarray:
    """
    Generate a simple deterministic embedding from text.
    Uses TF-IDF-like approach without external dependencies.
    """
    text = text.lower()
    words = text.split()

    embedding = np.zeros(768, dtype=np.float32)

    for i, word in enumerate(words):
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "little") % 768
        embedding[bucket] += 1.0 / (i + 1)

    norm = np.linalg.norm(embedding)
    if norm > 0:
        embedding = embedding / norm

    return embedding

def cosine_similarity(emb1: np.ndarray, emb2: np.ndarray) -> float:
    """Calculate cosine similarity between two embeddings."""
    return float(np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2) + 1e-10))

def store_message_embedding(conversation_id: str, msg_idx: int, role: str, content: str):
    """Store message embedding in vector DB."""
    try:
        embedding = get_simple_embedding(content)
        embedding_bytes = embedding.tobytes()
        
        conn = sqlite3.connect(str(VECTOR_DB))
        c = conn.cursor()
        
        c.execute("""
            INSERT OR REPLACE INTO embeddings 
            (conversation_id, message_index, role, content, embedding)
            VALUES (?, ?, ?, ?, ?)
        """, (conversation_id, msg_idx, role, content, embedding_bytes))
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Failed to store embedding: {e}")

def search_relevant_messages(conversation_id: str, query: str, top_k: int = 3) -> List[dict]:
    """
    Search for relevant messages in a conversation using semantic similarity.
    Returns top_k most similar messages.
    """
    try:
        query_embedding = get_simple_embedding(query)
        
        conn = sqlite3.connect(str(VECTOR_DB))
        c = conn.cursor()
        
        c.execute("""
            SELECT message_index, role, content, embedding
            FROM embeddings
            WHERE conversation_id = ?
            ORDER BY message_index DESC
            LIMIT 50
        """, (conversation_id,))
        
        results = []
        for msg_idx, role, content, embedding_bytes in c.fetchall():
            embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
            similarity = cosine_similarity(query_embedding, embedding)
            results.append({
                "index": msg_idx,
                "role": role,
                "content": content,
                "similarity": similarity
            })
        
        conn.close()
        
        # Sort by similarity and return top_k
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]
        
    except Exception as e:
        print(f"⚠️ Failed to search messages: {e}")
        return []

# Initialize vector DB on startup
init_vector_db()
init_feedback_db()
init_performance_db()

# ============================================================
# TOOL EXECUTION SYSTEM
# ============================================================

import subprocess
import platform

class ToolRequest(BaseModel):
    """Tool execution request from LLM."""
    tool: str
    params: Dict = {}

class ToolResult(BaseModel):
    """Result of tool execution."""
    tool: str
    status: str  # "success" or "error"
    result: str
    error: Optional[str] = None

# Define available tools
AVAILABLE_TOOLS = {
    "file.read": "Read file contents",
    "file.write": "Write to file",
    "file.append": "Append to file",
    "web.fetch": "Fetch URL content",
    "system.info": "Get system information",
    "shell.exec": "Execute shell command (restricted)"
}

async def execute_tool(tool_req: ToolRequest) -> ToolResult:
    """Execute a tool with safety checks."""
    
    try:
        tool = tool_req.tool
        params = tool_req.params
        
        if tool == "file.read":
            return await tool_file_read(params)
        elif tool == "file.write":
            return await tool_file_write(params)
        elif tool == "file.append":
            return await tool_file_append(params)
        elif tool == "web.fetch":
            return await tool_web_fetch(params)
        elif tool == "system.info":
            return await tool_system_info(params)
        elif tool == "shell.exec":
            return await tool_shell_exec(params)
        else:
            return ToolResult(
                tool=tool,
                status="error",
                result="",
                error=f"Unknown tool: {tool}"
            )
    except Exception as e:
        return ToolResult(
            tool=tool_req.tool,
            status="error",
            result="",
            error=str(e)
        )

async def tool_file_read(params: Dict) -> ToolResult:
    """Read file contents with size limit."""
    try:
        path = params.get("path", "")
        if not path:
            return ToolResult(tool="file.read", status="error", result="", error="Missing path")
        
        # Safety: restrict to home directory
        safe_path = Path(path).expanduser().resolve()
        if not str(safe_path).startswith(str(Path.home())):
            return ToolResult(tool="file.read", status="error", result="", error="Access denied: only home directory allowed")
        
        # Check file size
        if safe_path.stat().st_size > MAX_FILE_SIZE:
            return ToolResult(tool="file.read", status="error", result="", error=f"File too large (max {MAX_FILE_SIZE // 1024 // 1024}MB)")
        
        with open(safe_path, "r") as f:
            content = f.read()
        
        return ToolResult(tool="file.read", status="success", result=content[:MAX_TOOL_OUTPUT])
    except Exception as e:
        return ToolResult(tool="file.read", status="error", result="", error=str(e))

async def tool_file_write(params: Dict) -> ToolResult:
    """Write to file with size limit."""
    try:
        path = params.get("path", "")
        content = params.get("content", "")
        
        if not path:
            return ToolResult(tool="file.write", status="error", result="", error="Missing path")
        
        # Check content size
        if len(content) > MAX_FILE_SIZE:
            return ToolResult(tool="file.write", status="error", result="", error=f"Content too large (max {MAX_FILE_SIZE // 1024 // 1024}MB)")
        
        # Safety: restrict to home directory
        safe_path = Path(path).expanduser().resolve()
        if not str(safe_path).startswith(str(Path.home())):
            return ToolResult(tool="file.write", status="error", result="", error="Access denied: only home directory allowed")
        
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        with open(safe_path, "w") as f:
            f.write(content)
        
        return ToolResult(tool="file.write", status="success", result=f"Wrote {len(content)} bytes to {safe_path}")
    except Exception as e:
        return ToolResult(tool="file.write", status="error", result="", error=str(e))

async def tool_file_append(params: Dict) -> ToolResult:
    """Append to file."""
    try:
        path = params.get("path", "")
        content = params.get("content", "")
        
        if not path:
            return ToolResult(tool="file.append", status="error", result="", error="Missing path")
        
        safe_path = Path(path).expanduser().resolve()
        if not str(safe_path).startswith(str(Path.home())):
            return ToolResult(tool="file.append", status="error", result="", error="Access denied: only home directory allowed")
        
        with open(safe_path, "a") as f:
            f.write(content)
        
        return ToolResult(tool="file.append", status="success", result=f"Appended {len(content)} bytes to {safe_path}")
    except Exception as e:
        return ToolResult(tool="file.append", status="error", result="", error=str(e))

async def tool_web_fetch(params: Dict) -> ToolResult:
    """Fetch URL content."""
    try:
        url = params.get("url", "")
        if not url:
            return ToolResult(tool="web.fetch", status="error", result="", error="Missing url")
        
        # Safety: whitelist common protocols
        if not url.startswith(("http://", "https://")):
            return ToolResult(tool="web.fetch", status="error", result="", error="Only http/https allowed")
        
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        
        return ToolResult(tool="web.fetch", status="success", result=resp.text[:MAX_TOOL_OUTPUT])
    except Exception as e:
        return ToolResult(tool="web.fetch", status="error", result="", error=str(e))

async def tool_system_info(params: Dict) -> ToolResult:
    """Get system information."""
    try:
        info_type = params.get("type", "all")
        
        info = {
            "platform": platform.system(),
            "platform_version": platform.release(),
            "python_version": platform.python_version(),
            "processor": platform.processor()
        }
        
        if info_type == "all":
            result = json.dumps(info, indent=2)
        else:
            result = str(info.get(info_type, "Unknown"))
        
        return ToolResult(tool="system.info", status="success", result=result)
    except Exception as e:
        return ToolResult(tool="system.info", status="error", result="", error=str(e))

async def tool_shell_exec(params: Dict) -> ToolResult:
    """Execute shell command (restricted whitelist only)."""
    try:
        cmd = params.get("command", "")
        if not cmd:
            return ToolResult(tool="shell.exec", status="error", result="", error="Missing command")
        
        # Whitelist safe commands only
        safe_commands = ["echo", "date", "pwd", "ls", "wc", "head", "tail"]
        first_cmd = cmd.split()[0] if cmd.split() else ""
        
        if first_cmd not in safe_commands:
            return ToolResult(tool="shell.exec", status="error", result="", error=f"Command not whitelisted: {first_cmd}")
        
        # Execute with timeout — use shlex to avoid shell injection
        import shlex
        result = subprocess.run(
            shlex.split(cmd),
            shell=False,
            capture_output=True,
            text=True,
            timeout=5
        )
        
        output = result.stdout if result.returncode == 0 else result.stderr
        status = "success" if result.returncode == 0 else "error"
        
        return ToolResult(tool="shell.exec", status=status, result=output[:2000])
    except subprocess.TimeoutExpired:
        return ToolResult(tool="shell.exec", status="error", result="", error="Command timeout")
    except Exception as e:
        return ToolResult(tool="shell.exec", status="error", result="", error=str(e))

# ============================================================
# MEMORY STORAGE
# ============================================================

def get_history(conversation_id: str, user_id: str = "default", project_id: Optional[str] = None) -> List[dict]:
    """
    Retrieve conversation message history by ID.
    Auto-creates conversation with metadata if missing.
    """
    convo = CONVERSATIONS.setdefault(
        conversation_id,
        {
            "title": DEFAULT_CONVO_TITLE,
            "messages": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "user_id": user_id,
            "project_id": project_id,
            "system_prompt": None,
        }
    )
    if convo.get("user_id") not in (None, user_id, "default"):
        raise HTTPException(403, "Forbidden: conversation not owned by user")
    convo["user_id"] = user_id
    if project_id:
        convo["project_id"] = project_id
    return convo["messages"]

def prune_conversation_history(messages: List[dict], max_turns: int = 10) -> List[dict]:
    """Keep system messages + last N turns; summarize earlier content."""
    if len(messages) <= max_turns:
        return messages

    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    recent = non_system[-max_turns:]
    older = non_system[:-max_turns]
    summary = {}
    if older:
        summary_text = generate_conversation_summary(older)
        summary = {"role": "system", "content": summary_text}
        return system_msgs + ([summary] if summary else []) + recent

    return system_msgs + recent

def generate_conversation_summary(older_messages: List[dict]) -> str:
    """Use a cheap local model to summarize older turns."""
    if not older_messages:
        return ""

    summary_model = "./models/llama3.2-3b-instruct-q4_k_m.gguf"
    model_meta = get_model_meta(summary_model)
    node_id = model_meta.get("node") or "mac-node"

    try:
        node = get_node_by_id(node_id)
    except HTTPException:
        node = NODE_CONFIGS[0]

    condensed = "\n".join([f"{m.get('role','')}: {m.get('content','')[:200]}" for m in older_messages[-6:]])
    summary_prompt = f"Summarize the earlier conversation in 2-3 sentences. Keep key facts and decisions.\n\n{condensed}"

    payload = {
        "model": summary_model,
        "messages": [
            {"role": "system", "content": "You summarize prior chat turns concisely."},
            {"role": "user", "content": summary_prompt},
        ],
        "max_tokens": 150,
        "temperature": 0.3,
        "stream": False,
    }

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(f"{node.url}/v1/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as e:
        print(f"⚠️ Summary generation failed: {e}")
        return f"[Earlier conversation summary over {len(older_messages)} messages]"

def clear_history(conversation_id: str):
    """Clear a specific conversation history."""
    if conversation_id in CONVERSATIONS:
        del CONVERSATIONS[conversation_id]
        save_conversations(CONVERSATIONS)

def assert_convo_owner(conversation_id: str, user_id: str):
    convo = CONVERSATIONS.get(conversation_id)
    if not convo:
        raise HTTPException(404, f"Conversation '{conversation_id}' not found")
    owner = convo.get("user_id") or "default"
    if owner != user_id:
        raise HTTPException(403, "Forbidden: conversation not owned by user")

# ============================================================
# REQUEST/RESPONSE MODELS
# ============================================================

class ChatRequest(BaseModel):
    conversation_id: str
    prompt: str = ""
    model: Optional[str] = None
    max_tokens: Optional[int] = 2048

    @field_validator("prompt")
    @classmethod
    def validate_prompt_length(cls, v: str) -> str:
        if len(v) > 100_000:
            raise ValueError("Prompt exceeds 100,000 character limit")
        return v
    temperature: Optional[float] = 0.7
    images: Optional[List[str]] = None
    node_id: Optional[str] = None
    project_id: Optional[str] = None

class RoutePreferences(BaseModel):
    max_cost: Optional[float] = None
    min_quality: Optional[float] = None
    require_vision: Optional[bool] = None

class RouteDecisionRequest(BaseModel):
    prompt: str
    context: Optional[List[dict]] = None
    user_preferences: Optional[RoutePreferences] = None
    project_id: Optional[str] = None

class RouteDecisionResponse(BaseModel):
    model_id: str
    confidence: float
    estimated_cost: float
    reason: Optional[str] = None

class FeedbackEntry(BaseModel):
    model_id: str
    score: int  # -1, 0, 1
    prompt: Optional[str] = ""
    response: Optional[str] = ""
    conversation_id: Optional[str] = None
    complexity: Optional[float] = None

class ChatResponse(BaseModel):
    response: str
    node: str
    conversation_id: str
    message_count: int
    model: Optional[str] = None

class ConversationInfo(BaseModel):
    conversation_id: str
    title: str
    message_count: int
    last_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    project_id: Optional[str] = None
    system_prompt: Optional[str] = None

class RenameRequest(BaseModel):
    title: str

class ModelDownloadRequest(BaseModel):
    url: str
    dest_path: Optional[str] = None

class ProjectCreate(BaseModel):
    name: str
    system_prompt: Optional[str] = None
    preferred_model: Optional[str] = None
    max_budget: Optional[float] = None
    description: Optional[str] = None

class ProjectResponse(BaseModel):
    project_id: str
    name: str
    system_prompt: Optional[str] = None
    preferred_model: Optional[str] = None
    max_budget: Optional[float] = None
    created_at: Optional[str] = None
    user_id: Optional[str] = None
    description: Optional[str] = None

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    system_prompt: Optional[str] = None
    preferred_model: Optional[str] = None
    max_budget: Optional[float] = None
    description: Optional[str] = None

class ResyncRequest(BaseModel):
    project_id: Optional[str] = None

# ============================================================
# ROUTES
# ============================================================

@app.get("/health")
def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "nodes": [n.model_dump() for n in NODE_CONFIGS],
        "active_conversations": len(CONVERSATIONS)
    }           

@app.get("/nodes")
def list_nodes(_auth=Depends(require_api_key)):
    """List all available llama.cpp nodes."""
    return [n.model_dump() for n in NODE_CONFIGS]

@app.get("/nodes/status")
async def get_all_nodes_status(_auth=Depends(require_api_key)):
    """Get real-time status and latency for all nodes."""
    statuses = await asyncio.gather(
        *[get_node_health(n) for n in NODE_CONFIGS]
    )
    return statuses

@app.get("/nodes/{node_id}/models")
async def get_node_models_endpoint(node_id: str, _auth=Depends(require_api_key)):
    """Get available models from a specific node."""
    node = next((n for n in NODE_CONFIGS if n.id == node_id), None)
    if not node:
        raise HTTPException(404, f"Node '{node_id}' not found")

    models = await get_node_models(node)
     
    return {
        "node_id": node_id,
        "node_name": node.name,
        "models": models
    }

@app.api_route("/models/download", methods=["POST", "OPTIONS"])
def download_model(req: ModelDownloadRequest, request: Request, _auth=Depends(require_api_key)):
    """
    Download a model file from Hugging Face (or HTTP/HTTPS) into the local models directory.
    """
    if request.method == "OPTIONS":
        return {"status": "ok"}
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Only http/https URLs are supported")

    parsed = urlparse(url)
    if "huggingface.co" not in parsed.netloc:
        raise HTTPException(400, "Only Hugging Face URLs are allowed for safety")

    dest_root = Path("models")
    dest_root.mkdir(parents=True, exist_ok=True)

    filename = Path(parsed.path).name or "download.bin"
    dest_path = Path(req.dest_path).expanduser() if req.dest_path else dest_root / filename
    if dest_path.is_dir():
        dest_path = dest_path / filename

    # Safety: keep within repo/models
    try:
        dest_path = dest_path.resolve()
        if dest_root.resolve() not in dest_path.parents and dest_path != dest_root.resolve():
            raise HTTPException(400, "Destination must be inside the models directory")
    except Exception:
        raise HTTPException(400, "Invalid destination path")

    max_bytes = 10 * 1024 * 1024 * 1024  # 10GB cap
    total = 0
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        f.close()
                        dest_path.unlink(missing_ok=True)
                        raise HTTPException(400, "File too large (over 10GB limit)")
                    f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Download failed: {e}")

    return {
        "status": "downloaded",
        "url": url,
        "saved_to": str(dest_path),
        "size_bytes": total
    }

@app.get("/conversations/{conversation_id}/memories")
def get_conversation_memories(conversation_id: str, query: str = "", user_id: str = Depends(get_current_user)):
    """Search for relevant memories in a conversation."""
    assert_convo_owner(conversation_id, user_id)
    if not query:
        return {"memories": [], "query": query}
    
    memories = search_relevant_messages(conversation_id, query, top_k=3)
    return {
        "conversation_id": conversation_id,
        "query": query,
        "memories": memories
    }

@app.post("/audio/transcribe")
async def transcribe_audio(file: UploadFile = File(...), user_id: str = Depends(get_current_user)):
    """Transcribe audio locally using whisper.cpp. Expects an audio file upload."""
    if not file or not file.filename:
        raise HTTPException(400, "Audio file required")
    ctype = file.content_type or ""
    if ctype and not ctype.startswith("audio/"):
        raise HTTPException(400, "Invalid content type; expected audio")
    data = await file.read()
    if len(data) > MAX_AUDIO_SIZE:
        raise HTTPException(400, f"Audio too large (max {MAX_AUDIO_SIZE // 1024 // 1024}MB)")

    # Ensure whisper binary and model exist
    if not WHISPER_BIN.exists():
        raise HTTPException(500, f"Whisper binary not found at {WHISPER_BIN}")
    if not WHISPER_MODEL.exists():
        raise HTTPException(500, f"Whisper model not found at {WHISPER_MODEL}")

    tmp_id = uuid.uuid4().hex
    tmp_input = Path(f"tmp_audio_{tmp_id}.bin")
    tmp_wav = Path(f"tmp_audio_{tmp_id}.wav")
    tmp_out = Path(f"tmp_audio_{tmp_id}.txt")

    try:
        tmp_input.write_bytes(data)
        # Convert to mono 16k wav
        if not FFMPEG_BIN or not Path(FFMPEG_BIN).exists():
            raise HTTPException(500, f"ffmpeg not found (looked for {FFMPEG_BIN})")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", str(tmp_input), "-ac", "1", "-ar", "16000", str(tmp_wav)],
            check=True,
            capture_output=True,
        )

        # Run whisper.cpp
        run_cmd = [
            str(WHISPER_BIN),
            "-m",
            str(WHISPER_MODEL),
            "-f",
            str(tmp_wav),
            "-otxt",
            "-of",
            f"tmp_audio_{tmp_id}",
        ]
        subprocess.run(run_cmd, check=True, capture_output=True)

        transcript = ""
        if tmp_out.exists():
            transcript = tmp_out.read_text().strip()
        if not transcript:
            raise HTTPException(500, "Transcription failed or empty output")

        return {"text": transcript, "model": str(WHISPER_MODEL), "user_id": user_id}
    except subprocess.CalledProcessError as e:
        record_error("whisper", e.stderr.decode("utf-8", errors="ignore") if e.stderr else str(e))
        raise HTTPException(500, f"Transcription failed: {str(e)}")
    except Exception as e:
        record_error("whisper", str(e))
        raise HTTPException(500, f"Transcription error: {str(e)}")
    finally:
        for f in (tmp_input, tmp_wav, tmp_out):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

@app.get("/projects")
def list_projects(user_id: str = Depends(get_current_user)):
    """List projects owned by the current user."""
    projects = [
        ProjectResponse(
            project_id=pid,
            name=p.get("name", ""),
            system_prompt=p.get("system_prompt"),
            preferred_model=p.get("preferred_model"),
            max_budget=p.get("max_budget"),
            created_at=p.get("created_at"),
            user_id=p.get("user_id"),
            description=p.get("description"),
        ).model_dump()
        for pid, p in PROJECTS.items()
        if p.get("user_id", "default") == user_id
    ]
    return {"projects": projects}

@app.post("/projects", response_model=ProjectResponse)
def create_project(req: ProjectCreate, user_id: str = Depends(get_current_user)):
    """Create a new project/space for scoping conversations."""
    project_id = f"proj_{int(time.time() * 1000)}"
    project = {
        "name": req.name.strip(),
        "system_prompt": req.system_prompt or "",
        "preferred_model": req.preferred_model,
        "max_budget": req.max_budget,
        "created_at": datetime.now().isoformat(),
        "user_id": user_id,
        "description": req.description or "",
    }
    PROJECTS[project_id] = project
    save_projects(PROJECTS)
    return ProjectResponse(
        project_id=project_id,
        name=project["name"],
        system_prompt=project["system_prompt"],
        preferred_model=project["preferred_model"],
        max_budget=project["max_budget"],
        created_at=project["created_at"],
        user_id=user_id,
        description=project["description"],
    )

@app.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project_endpoint(project_id: str, user_id: str = Depends(get_current_user)):
    proj = get_project(project_id, user_id)
    return ProjectResponse(
        project_id=project_id,
        name=proj.get("name", ""),
        system_prompt=proj.get("system_prompt"),
        preferred_model=proj.get("preferred_model"),
        max_budget=proj.get("max_budget"),
        created_at=proj.get("created_at"),
        user_id=proj.get("user_id"),
        description=proj.get("description"),
    )

@app.put("/projects/{project_id}", response_model=ProjectResponse)
def update_project(project_id: str, req: ProjectUpdate, user_id: str = Depends(get_current_user)):
    proj = get_project(project_id, user_id)
    updates = req.model_dump(exclude_unset=True)
    for key, val in updates.items():
        if val is not None:
            proj[key] = val
    proj["updated_at"] = datetime.now().isoformat()
    PROJECTS[project_id] = proj
    save_projects(PROJECTS)
    return ProjectResponse(
        project_id=project_id,
        name=proj.get("name", ""),
        system_prompt=proj.get("system_prompt"),
        preferred_model=proj.get("preferred_model"),
        max_budget=proj.get("max_budget"),
        created_at=proj.get("created_at"),
        user_id=proj.get("user_id"),
        description=proj.get("description"),
    )

@app.delete("/projects/{project_id}")
def delete_project(project_id: str, user_id: str = Depends(get_current_user)):
    """Delete a project and detach it from conversations."""
    _ = get_project(project_id, user_id)
    PROJECTS.pop(project_id, None)
    # Detach project from existing conversations
    for cid, convo in CONVERSATIONS.items():
        if convo.get("project_id") == project_id and convo.get("user_id", "default") == user_id:
            convo["project_id"] = None
    save_projects(PROJECTS)
    save_conversations(CONVERSATIONS)
    return {"status": "deleted", "project_id": project_id}

@app.get("/tools")
def list_available_tools(_auth=Depends(require_api_key)):
    """List available tools for the LLM to use."""
    return {
        "tools": AVAILABLE_TOOLS,
        "instructions": "LLM can request tools by outputting JSON: {\"tool\": \"tool.name\", \"params\": {...}}"
    }

@app.post("/tools/execute")
async def execute_tool_endpoint(tool_req: ToolRequest, _auth=Depends(require_api_key)):
    """Execute a tool and return results."""
    result = await execute_tool(tool_req)
    return result.model_dump()

@app.post("/route/decision", response_model=RouteDecisionResponse)
def route_decision(req: RouteDecisionRequest, user_id: str = Depends(get_current_user)):
    """Lightweight routing decision endpoint."""
    prefs = req.user_preferences or RoutePreferences()
    decision = choose_model_for_prompt(req.prompt, prefs, len(req.context or []))
    return decision.model_dump()

@app.post("/route/decision/cascade", response_model=List[RouteDecisionResponse])
def route_decision_cascade(req: RouteDecisionRequest, user_id: str = Depends(get_current_user)):
    """Return ranked primary + fallback routing decisions."""
    prefs = req.user_preferences or RoutePreferences()
    primary = choose_model_for_prompt(req.prompt, prefs, len(req.context or []))

    fallback_prefs = RoutePreferences(
        max_cost=(prefs.max_cost * 1.5) if prefs.max_cost else 0.02,
        min_quality=max(0.7, (prefs.min_quality or 0.8) - 0.1),
        require_vision=prefs.require_vision,
    )
    secondary = choose_model_for_prompt(req.prompt, fallback_prefs, len(req.context or []))
    return [primary.model_dump(), secondary.model_dump()]

@app.post("/feedback")
def submit_feedback(entry: FeedbackEntry, _auth=Depends(require_api_key)):
    """Capture thumbs up/down feedback for adaptive routing."""
    score = max(-1, min(1, entry.score))
    MODEL_FEEDBACK[entry.model_id].append({
        "score": score,
        "complexity": entry.complexity or 0.0,
        "ts": datetime.now().isoformat()
    })
    # Keep last 200 feedbacks per model
    if len(MODEL_FEEDBACK[entry.model_id]) > 200:
        MODEL_FEEDBACK[entry.model_id] = MODEL_FEEDBACK[entry.model_id][-200:]
    try:
        conn = sqlite3.connect(str(FEEDBACK_DB))
        c = conn.cursor()
        c.execute(
            "INSERT INTO feedback (model_id, score, complexity) VALUES (?, ?, ?)",
            (entry.model_id, score, entry.complexity or 0.0)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Failed to persist feedback: {e}")
    return {"status": "ok"}

@app.get("/analytics/costs")
def get_cost_analytics(_auth=Depends(require_api_key)):
    """Get cost breakdown by model and conversation."""
    log_path = Path("cost_log.jsonl")
    if not log_path.exists():
        return {"total_cost": 0, "by_model": {}, "by_conversation": {}}

    total = 0.0
    by_model: Dict[str, float] = {}
    by_convo: Dict[str, float] = {}

    with open(log_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            cost = float(entry.get("cost", 0))
            model = entry.get("model", "unknown")
            convo = entry.get("conversation_id", "unknown")
            total += cost
            by_model[model] = by_model.get(model, 0.0) + cost
            by_convo[convo] = by_convo.get(convo, 0.0) + cost

    by_model_sorted = {k: round(v, 4) for k, v in sorted(by_model.items(), key=lambda x: -x[1])}
    by_convo_sorted = {k: round(v, 4) for k, v in sorted(by_convo.items(), key=lambda x: -x[1])[:10]}

    return {
        "total_cost": round(total, 4),
        "by_model": by_model_sorted,
        "by_conversation": by_convo_sorted,
    }

@app.get("/monitoring/health")
async def monitoring_dashboard(_auth=Depends(require_api_key)):
    """Monitor nodes and model health."""
    node_statuses = await asyncio.gather(*[get_node_health(n) for n in NODE_CONFIGS])
    return {
        "nodes": node_statuses,
        "model_health": MODEL_HEALTH,
        "recent_errors": RECENT_ERRORS
    }

@app.get("/search")
def global_search(query: str, top_k: int = 10, user_id: str = Depends(get_current_user)):
    """Search across all conversations by semantic similarity with substring fallback."""
    results = []
    try:
        query_emb = get_simple_embedding(query)
        conn = sqlite3.connect(str(VECTOR_DB))
        c = conn.cursor()
        c.execute("""
            SELECT conversation_id, message_index, role, content, embedding
            FROM embeddings
            ORDER BY created_at DESC
            LIMIT 2000
        """)
        for cid, idx, role, content, emb_bytes in c.fetchall():
            emb = np.frombuffer(emb_bytes, dtype=np.float32)
            sim = cosine_similarity(query_emb, emb)
            convo_owner = CONVERSATIONS.get(cid, {}).get("user_id", "default")
            if convo_owner != user_id:
                continue
            if sim > 0.35:
                results.append({
                    "conversation_id": cid,
                    "title": CONVERSATIONS.get(cid, {}).get("title", "Unknown"),
                    "role": role,
                    "content": content,
                    "similarity": sim
                })
        conn.close()
    except Exception as e:
        record_error("search", str(e))

    # Fallback substring search if no hits
    if not results:
        q_low = query.lower()
        for cid, convo in CONVERSATIONS.items():
            if convo.get("user_id", "default") != user_id:
                continue
            for msg in convo.get("messages", []):
                content = msg.get("content", "")
                if isinstance(content, str) and q_low in content.lower():
                    results.append({
                        "conversation_id": cid,
                        "title": convo.get("title", "Unknown"),
                        "role": msg.get("role", ""),
                        "content": content[:200],
                        "similarity": 0.2
                    })
                    break

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:top_k]

@app.get("/conversations")
def list_conversations(user_id: str = Depends(get_current_user)):
    """List all active conversations with metadata and titles."""
    result = []
    for conversation_id, convo in CONVERSATIONS.items():
        if convo.get("user_id", "default") != user_id:
            continue
        msgs = convo.get("messages", [])
        result.append(
            ConversationInfo(
                conversation_id=conversation_id,
                title=convo.get("title", DEFAULT_CONVO_TITLE),
                message_count=len(msgs),
                last_message=msgs[-1]["content"][:50] if msgs else None,
                created_at=convo.get("created_at"),
                updated_at=convo.get("updated_at"),
                project_id=convo.get("project_id"),
                system_prompt=convo.get("system_prompt")
            ).model_dump()
        )
    return result

@app.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user_id: str = Depends(get_current_user)):
    """Retrieve full conversation history with metadata by ID."""
    assert_convo_owner(conversation_id, user_id)
    convo = CONVERSATIONS[conversation_id]
    return {
        "conversation_id": conversation_id,
        "title": convo.get("title", DEFAULT_CONVO_TITLE),
        "messages": convo.get("messages", []),
        "created_at": convo.get("created_at"),
        "updated_at": convo.get("updated_at"),
        "project_id": convo.get("project_id"),
        "system_prompt": convo.get("system_prompt"),
    }

@app.post("/conversations/from_template")
def create_from_template(template_name: str, project_id: Optional[str] = None, user_id: str = Depends(get_current_user)):
    """Create a conversation from a predefined template."""
    if template_name not in TEMPLATES:
        raise HTTPException(404, "Template not found")
    template = TEMPLATES[template_name]
    project_cfg = get_project(project_id, user_id) if project_id else {}
    system_prompt = project_cfg.get("system_prompt") or template.get("system_prompt", SYSTEM_PROMPT)
    preferred_model = template.get("preferred_model") or project_cfg.get("preferred_model")
    cid = f"convo_{int(time.time())}"
    CONVERSATIONS[cid] = {
        "title": template.get("title", DEFAULT_CONVO_TITLE),
        "messages": [{"role": "system", "content": system_prompt}],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "template": template_name,
        "user_id": user_id,
        "project_id": project_id,
    }
    save_conversations(CONVERSATIONS)
    return {"conversation_id": cid, "template": template_name, "preferred_model": preferred_model, "project_id": project_id}

@app.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user_id: str = Depends(get_current_user)):
    """Delete a conversation and its history."""
    assert_convo_owner(conversation_id, user_id)
    clear_history(conversation_id)
    return {"status": "deleted", "conversation_id": conversation_id}

@app.post("/conversations/{conversation_id}/rename")
def rename_conversation(conversation_id: str, req: RenameRequest, user_id: str = Depends(get_current_user)):
    """Rename a conversation title."""
    assert_convo_owner(conversation_id, user_id)

    new_title = req.title.strip() or DEFAULT_CONVO_TITLE
    CONVERSATIONS[conversation_id]["title"] = new_title
    CONVERSATIONS[conversation_id]["updated_at"] = datetime.now().isoformat()
    
    save_conversations(CONVERSATIONS)

    return {
        "status": "updated",
        "conversation_id": conversation_id,
        "title": new_title
    }

@app.post("/conversations/{conversation_id}/resync_project")
def resync_conversation_project(conversation_id: str, req: ResyncRequest, user_id: str = Depends(get_current_user)):
    """Re-apply project instructions to a conversation and persist them."""
    assert_convo_owner(conversation_id, user_id)
    convo = CONVERSATIONS[conversation_id]

    project_id = req.project_id or convo.get("project_id")
    if not project_id:
        raise HTTPException(400, "No project linked to conversation")

    proj = get_project(project_id, user_id)
    convo["project_id"] = project_id
    convo["system_prompt"] = proj.get("system_prompt") or SYSTEM_PROMPT
    convo["updated_at"] = datetime.now().isoformat()
    save_conversations(CONVERSATIONS)

    return {
        "status": "updated",
        "conversation_id": conversation_id,
        "project_id": project_id,
        "system_prompt": convo["system_prompt"],
    }

@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request = None, user_id: str = Depends(get_current_user)):
    """
    Send a message while maintaining persistent conversation history.
    Supports text-only, image-only, or multimodal (text + images) requests.
    Automatically updates metadata and saves to disk after each interaction.
    """
    if request:
        check_rate_limit(request.client.host)
    user_text = (req.prompt or "").strip()
    
    # Validate: require either text or images
    if not user_text and not req.images:
        raise HTTPException(400, "Prompt or images required")
    
    # Validate image sizes
    if req.images:
        for img in req.images:
            if len(img) > MAX_IMAGE_SIZE:
                raise HTTPException(400, f"Image too large (max {MAX_IMAGE_SIZE // 1024 // 1024}MB base64)")

    # Determine project context
    existing_convo = CONVERSATIONS.get(req.conversation_id)
    if existing_convo:
        assert_convo_owner(req.conversation_id, user_id)
    project_id = req.project_id or (existing_convo.get("project_id") if existing_convo else None)
    project_cfg = get_project(project_id, user_id) if project_id else {}

    # Get conversation history and append user message
    raw_history = get_history(req.conversation_id, user_id=user_id, project_id=project_id)
    raw_history.append({"role": "user", "content": user_text if user_text else "[image]"})
    history = prune_conversation_history(raw_history)

    system_prompt = (
        existing_convo.get("system_prompt")
        if existing_convo
        else None
    ) or project_cfg.get("system_prompt") or SYSTEM_PROMPT
    preferred_model = req.model or project_cfg.get("preferred_model") or DEFAULT_MODEL_ID

    # Build messages copy so we can adjust content shape for vision models without
    # mutating persisted history.
    messages_for_node = [{"role": "system", "content": system_prompt}] + [dict(m) for m in history]

    if req.images:
        multimodal_content = []
        if user_text:
            multimodal_content.append({"type": "text", "text": user_text})
        for img in req.images:
            multimodal_content.append({"type": "image_url", "image_url": {"url": img}})
        messages_for_node[-1] = {"role": "user", "content": multimodal_content}

    # Budget check (estimate)
    model_meta = get_model_meta(preferred_model)
    est_tokens = estimate_tokens(user_text) + sum(estimate_tokens(m.get("content","")) for m in history[-4:])
    est_cost = (est_tokens / 1000) * model_meta.get("cost_per_1k", 0)
    spent = get_user_spent(user_id)
    budget = get_user_budget(user_id)
    if spent + est_cost > budget:
        raise HTTPException(402, f"Budget exceeded. Spent ${spent:.4f} / ${budget:.4f}.")

    # Select node (user-selected if provided)
    node = get_node_by_id(req.node_id) if req.node_id else choose_node()
    endpoint = f"{node.url}/v1/chat/completions"

    # Build payload
    payload = {
        "model": preferred_model,
        "messages": messages_for_node,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "stream": False
    }
    
    if req.images:
        payload["images"] = req.images

    # Call llama.cpp node
    try:
        start = time.time()
        with httpx.Client(timeout=120) as client:
            resp = client.post(endpoint, json=payload)
            resp.raise_for_status()
        latency_ms = (time.time() - start) * 1000
    except httpx.TimeoutException:
        track_model_failure(preferred_model, "timeout")
        raise HTTPException(504, f"Node '{node.name}' timed out")
    except httpx.ConnectError:
        track_model_failure(preferred_model, "connect_error")
        raise HTTPException(503, f"Cannot connect to node '{node.name}' at {node.url}")
    except httpx.HTTPStatusError as e:
        track_model_failure(preferred_model, "http_error")
        raise HTTPException(500, f"Node error: {e.response.text if e.response else str(e)}")
    except Exception as e:
        track_model_failure(preferred_model, "unexpected")
        raise HTTPException(500, f"Unexpected node error: {str(e)}")

    # Parse response
    try:
        data = resp.json()
        assistant_msg = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise HTTPException(500, f"Invalid response from node: {str(e)}")

    # Sanitize: strip any base64-looking image blobs
    assistant_msg = re.sub(
        r"data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9\/+=]+",
        "[image]",
        assistant_msg,
        flags=re.MULTILINE,
    )

    # Backward compatibility: remove exact user-provided base64 URLs if present
    if req.images:
        for img in req.images:
            if img in assistant_msg:
                assistant_msg = assistant_msg.replace(img, "[image]")

    # Save assistant response to history
    raw_history.append({"role": "assistant", "content": assistant_msg})

    # Log approximate cost
    try:
        actual_tokens = estimate_tokens(assistant_msg) + (estimate_tokens(user_text) if user_text else 0)
        model_meta = get_model_meta(preferred_model)
        cost = (actual_tokens / 1000) * model_meta.get("cost_per_1k", 0)
        log_entry = {
            "conversation_id": req.conversation_id,
            "model": preferred_model,
            "node": node.id,
            "tokens": actual_tokens,
            "cost": cost,
            "timestamp": datetime.now().isoformat(),
        }
        with open("cost_log.jsonl", "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass

    # Store embeddings
    user_msg_idx = len(history) - 2
    assistant_msg_idx = len(history) - 1
    if user_text:
        store_message_embedding(req.conversation_id, user_msg_idx, "user", user_text)
    store_message_embedding(req.conversation_id, assistant_msg_idx, "assistant", assistant_msg)

    # Update metadata and title
    convo = CONVERSATIONS[req.conversation_id]
    convo["updated_at"] = datetime.now().isoformat()
    if user_text and len(history) == 2 and convo.get("title") == DEFAULT_CONVO_TITLE:
        convo["title"] = user_text[:30] + ("..." if len(user_text) > 30 else "")

    save_conversations(CONVERSATIONS)

    # Log performance
    try:
        actual_tokens = estimate_tokens(assistant_msg) + (estimate_tokens(user_text) if user_text else 0)
        cost = (actual_tokens / 1000) * model_meta.get("cost_per_1k", 0)
        latency_val = locals().get("latency_ms", None)
        log_performance(user_id, req.conversation_id, preferred_model, actual_tokens, cost, latency_val, comp_score)
    except Exception:
        pass

    return ChatResponse(
        response=assistant_msg,
        node=node.name,
        conversation_id=req.conversation_id,
        message_count=len(history),
        model=preferred_model
    )

@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request = None, user_id: str = Depends(get_current_user)):
    """
    Streaming endpoint using SSE (Server-Sent Events).
    Supports text-only, image-only, or multimodal (text + images) requests.
    """
    if request:
        check_rate_limit(request.client.host)
    user_text = (req.prompt or "").strip()
    
    # Validate: require either text or images
    if not user_text and not req.images:
        raise HTTPException(400, "Prompt or images required")
    
    # Validate image sizes
    if req.images:
        for img in req.images:
            if len(img) > MAX_IMAGE_SIZE:
                raise HTTPException(400, f"Image too large (max {MAX_IMAGE_SIZE // 1024 // 1024}MB base64)")

    # Determine project context
    existing_convo = CONVERSATIONS.get(req.conversation_id)
    if existing_convo:
        assert_convo_owner(req.conversation_id, user_id)
    project_id = req.project_id or (existing_convo.get("project_id") if existing_convo else None)
    project_cfg = get_project(project_id, user_id) if project_id else {}

    # Get conversation history and append user message
    raw_history = get_history(req.conversation_id, user_id=user_id, project_id=project_id)
    raw_history.append({"role": "user", "content": user_text if user_text else "[image]"})
    history = prune_conversation_history(raw_history)

    system_prompt = (
        existing_convo.get("system_prompt")
        if existing_convo
        else None
    ) or project_cfg.get("system_prompt") or SYSTEM_PROMPT
    preferred_model = req.model or project_cfg.get("preferred_model") or DEFAULT_MODEL_ID

    messages_for_node = [{"role": "system", "content": system_prompt}] + [dict(m) for m in history]

    if req.images:
        multimodal_content = []
        if user_text:
            multimodal_content.append({"type": "text", "text": user_text})
        for img in req.images:
            multimodal_content.append({"type": "image_url", "image_url": {"url": img}})
        messages_for_node[-1] = {"role": "user", "content": multimodal_content}

    # Budget check (estimate)
    model_meta = get_model_meta(preferred_model)
    est_tokens = estimate_tokens(user_text) + sum(estimate_tokens(m.get("content","")) for m in history[-4:])
    est_cost = (est_tokens / 1000) * model_meta.get("cost_per_1k", 0)
    spent = get_user_spent(user_id)
    budget = get_user_budget(user_id)
    if spent + est_cost > budget:
        raise HTTPException(402, f"Budget exceeded. Spent ${spent:.4f} / ${budget:.4f}.")

    # Select node (user-selected if provided)
    node = get_node_by_id(req.node_id) if req.node_id else choose_node()
    endpoint = f"{node.url}/v1/chat/completions"

    # Build payload with streaming enabled
    payload = {
        "model": preferred_model,
        "messages": messages_for_node,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "stream": True
    }
    
    if req.images:
        payload["images"] = req.images

    async def stream_generator():
        """
        Generator that streams SSE events as tokens arrive.
        Offloads blocking HTTP to a background thread to keep the event loop responsive.
        """
        full_response = ""
        start_time = time.time()
        had_error = False

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", endpoint, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line == "[DONE]":
                            break
                        if line.startswith("data: "):
                            line = line[6:]
                        try:
                            data = json.loads(line)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                full_response += content
                                yield f"data: {json.dumps({'token': content, 'done': False})}\n\n"
                        except json.JSONDecodeError:
                            continue
        except httpx.TimeoutException:
            yield f"data: {json.dumps({'error': 'Node timed out', 'done': True})}\n\n"
            had_error = True
        except httpx.ConnectError:
            yield f"data: {json.dumps({'error': 'Cannot connect to node', 'done': True})}\n\n"
            had_error = True
        except httpx.HTTPStatusError as e:
            msg = f"Node error {e.response.status_code}: {e.response.text[:200] if e.response else ''}"
            yield f"data: {json.dumps({'error': msg, 'done': True})}\n\n"
            had_error = True
        except Exception as e:
            yield f"data: {json.dumps({'error': f'Unexpected: {str(e)}', 'done': True})}\n\n"
            had_error = True

        # Post-process and persist only if we actually got a response (and no fatal stream error)
        if not had_error:
            try:
                full_response = re.sub(
                    r"data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9\/+=]+",
                    "[image]",
                    full_response,
                    flags=re.MULTILINE,
                )

                if req.images:
                    for img in req.images:
                        if img in full_response:
                            full_response = full_response.replace(img, "[image]")
                # Persist assistant message to conversation (unpruned history)
                convo_history = get_history(req.conversation_id, user_id=user_id)
                convo_history.append({"role": "assistant", "content": full_response})

                user_msg_idx = len(history) - 2
                assistant_msg_idx = len(history) - 1
                if user_text:
                    store_message_embedding(req.conversation_id, user_msg_idx, "user", user_text)
                store_message_embedding(req.conversation_id, assistant_msg_idx, "assistant", full_response)

                convo = CONVERSATIONS[req.conversation_id]
                convo["updated_at"] = datetime.now().isoformat()
                if user_text and len(history) == 2 and convo.get("title") == DEFAULT_CONVO_TITLE:
                    convo["title"] = user_text[:30] + ("..." if len(user_text) > 30 else "")

                save_conversations(CONVERSATIONS)

                try:
                    actual_tokens = estimate_tokens(full_response) + (estimate_tokens(user_text) if user_text else 0)
                    model_meta = get_model_meta(preferred_model)
                    cost = (actual_tokens / 1000) * model_meta.get("cost_per_1k", 0)
                    log_entry = {
                        "conversation_id": req.conversation_id,
                        "model": preferred_model,
                        "node": node.id,
                        "tokens": actual_tokens,
                        "cost": cost,
                        "timestamp": datetime.now().isoformat(),
                    }
                    with open("cost_log.jsonl", "a") as f:
                        f.write(json.dumps(log_entry) + "\n")
                except Exception as e:
                    record_error("cost_log", str(e))

                try:
                    actual_tokens = estimate_tokens(full_response) + (estimate_tokens(user_text) if user_text else 0)
                    cost = (actual_tokens / 1000) * model_meta.get("cost_per_1k", 0)
                    latency_ms = (time.time() - start_time) * 1000
                    log_performance(user_id, req.conversation_id, preferred_model, actual_tokens, cost, latency_ms, comp_score)
                except Exception as e:
                    record_error("perf_log", str(e))
            except Exception as e:
                record_error("stream_finalize", str(e))

        # Always send terminal event so client doesn’t see incomplete chunked encoding
        yield f"data: {json.dumps({'token': '', 'done': True, 'message_count': len(history)})}\n\n"
    
    return StreamingResponse(stream_generator(), media_type="text/event-stream")

@app.post("/conversations/{conversation_id}/clear")
def clear_conversation(conversation_id: str, user_id: str = Depends(get_current_user)):
    """Clear all messages from a conversation but keep it registered."""
    assert_convo_owner(conversation_id, user_id)
    
    CONVERSATIONS[conversation_id]["messages"] = []
    CONVERSATIONS[conversation_id]["updated_at"] = datetime.now().isoformat()
    save_conversations(CONVERSATIONS)

    return {
        "status": "cleared",
        "conversation_id": conversation_id
    }

@app.get("/conversations/{conversation_id}/should_restart")
def check_restart_suggestion(conversation_id: str, user_id: str = Depends(get_current_user)):
    """Suggest starting new conversation if current is long."""
    assert_convo_owner(conversation_id, user_id)
    history = CONVERSATIONS[conversation_id].get("messages", [])
    should_restart = len(history) > 12
    return {
        "should_restart": should_restart,
        "message_count": len(history),
        "reason": "Long conversations may lose context—consider starting fresh" if should_restart else None
    }

@app.get("/conversations/{conversation_id}/export")
def export_conversation(conversation_id: str, format: str = "markdown", user_id: str = Depends(get_current_user)):
    """Export a conversation as markdown or JSON."""
    assert_convo_owner(conversation_id, user_id)
    convo = CONVERSATIONS[conversation_id]
    if format.lower() == "json":
        return convo
    if format.lower() == "markdown":
        md_lines = [f"# {convo.get('title', DEFAULT_CONVO_TITLE)}", ""]
        for msg in convo.get("messages", []):
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            md_lines.append(f"**{role}:** {content}")
            md_lines.append("")
        md = "\n".join(md_lines)
        return Response(content=md, media_type="text/markdown")
    raise HTTPException(400, "Unsupported format")

# ============================================================
# STATIC FILES
# ============================================================

# Serve static UI files (index.html, app.js, style.css) from repo root
STATIC_DIR = Path(__file__).resolve().parent
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

# ============================================================
# STARTUP/SHUTDOWN HOOKS
# ============================================================

@app.on_event("startup")
async def startup_event():
    """Load conversations on startup."""
    print(f"✅ DaveLLM Router v2.1 started")
    print(f"📁 Loaded {len(CONVERSATIONS)} conversations from disk")
    print(f"🖥️  Active nodes: {len(NODE_CONFIGS)}")
    asyncio.create_task(background_summarizer())

@app.on_event("shutdown")
async def shutdown_event():
    """Save conversations on shutdown."""
    save_conversations(CONVERSATIONS)
    print("💾 Conversations saved to disk")
async def background_summarizer():
    """Periodically summarize older conversations to reduce on-demand latency."""
    while True:
        await asyncio.sleep(3600)  # run hourly
        try:
            for cid, convo in CONVERSATIONS.items():
                msgs = convo.get("messages", [])
                if len(msgs) > 20:
                    summary = generate_conversation_summary(msgs[:-10])
                    convo["summary"] = summary
                    convo["last_summary_at"] = datetime.now().isoformat()
            save_conversations(CONVERSATIONS)
        except Exception as e:
            record_error("summarizer", str(e))
