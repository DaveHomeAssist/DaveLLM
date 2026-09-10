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
import ipaddress
import sqlite3
import socket
import threading
import shutil
import numpy as np
from itertools import cycle
from typing import Dict, List, Literal, Optional
from urllib.parse import urljoin, urlparse
from pathlib import Path
from datetime import datetime

import requests
import re
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
import httpx
import uuid

from daveharness import (
    DEFAULT_ERROR_BUDGET,
    DEFAULT_STEP_LIMIT,
    InMemoryPendingCallStore,
    ToolDefinition,
    ToolRegistry,
    resume_executor_loop,
    run_executor_loop,
    run_tool,
)
from project_context import (
    ContextBudgetError,
    ProjectContextError,
    ProjectContextStore,
    RevisionConflictError,
    component_quotas,
    estimate_tokens as estimate_project_tokens,
)

# ============================================================
# CONSTANTS
# ============================================================

PRODUCT_VERSION = (Path(__file__).resolve().parent / "VERSION").read_text(
    encoding="utf-8"
).strip()
BASE_DIR = Path(os.getenv("DAVE_DATA_DIR", ".")).expanduser().resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = BASE_DIR / "dave_conversations.json"
DEFAULT_CONVO_TITLE = "New Conversation"
VECTOR_DB = BASE_DIR / "dave_vectors.db"
FEEDBACK_DB = BASE_DIR / "feedback.db"
PERFORMANCE_DB = BASE_DIR / "performance.db"
PROJECTS_FILE = BASE_DIR / "dave_projects.json"
SETTINGS_FILE = BASE_DIR / "dave_settings.json"
COST_LOG = BASE_DIR / "cost_log.jsonl"
PROJECT_CONTEXT_DB = BASE_DIR / "dave_project_context.db"
PROJECT_UPLOADS_DIR = BASE_DIR / "project_uploads"
BUDGET_DEFAULT = float(os.getenv("DAVE_BUDGET_DEFAULT", "100"))
USER_BUDGETS = {}
if os.getenv("DAVE_USER_BUDGETS"):
    try:
        USER_BUDGETS = json.loads(os.getenv("DAVE_USER_BUDGETS", "{}"))
    except Exception:
        USER_BUDGETS = {}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_TOOL_OUTPUT = 5000
MAX_WEB_FETCH_BYTES = 1024 * 1024
MAX_WEB_FETCH_REDIRECTS = 5
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB base64 ≈ 3.75MB binary
MAX_AUDIO_SIZE = 20 * 1024 * 1024  # 20MB
try:
    DEFAULT_MODEL_CONTEXT_WINDOW = int(os.getenv("DAVE_MODEL_CONTEXT_DEFAULT", "32768"))
    DEFAULT_PROJECT_CONTEXT_TOKENS = int(os.getenv("DAVE_PROJECT_CONTEXT_TOKENS", "16384"))
    DEFAULT_BRAIN_COMPACT_TOKENS = int(os.getenv("DAVE_BRAIN_COMPACT_TOKENS", "3072"))
    BRAIN_RECOVERY_DAYS = int(os.getenv("DAVE_BRAIN_RECOVERY_DAYS", "30"))
except ValueError as exc:
    raise RuntimeError("DaveLLM token and recovery settings must be integers") from exc
WHISPER_BIN = Path(
    os.getenv("DAVE_WHISPER_BIN")
    or shutil.which("whisper-cli")
    or "./whisper.cpp/build/bin/whisper-cli"
).expanduser()
WHISPER_MODEL = Path(
    os.getenv("DAVE_WHISPER_MODEL")
    or BASE_DIR / "models" / "ggml-tiny.en.bin"
).expanduser()
FFMPEG_BIN = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

# Simple model catalog for routing decisions
MODEL_CATALOG = {
    str(Path("/Users/daverobertson/Desktop/Dave-LLM/models/qwen-vl-7b/Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf").resolve()): {
        "vision": True,
        "cost_per_1k": 0.0015,
        "quality": 0.9,
    },
    str(Path("./models/llama3.2-3b-instruct-q4_k_m.gguf").resolve()): {
        "vision": False,
        "cost_per_1k": 0.0006,
        "quality": 0.8,
    },
}

MODEL_HEALTH: Dict[str, dict] = {}
MODEL_INVENTORY: Dict[str, set[str]] = {}
RECENT_ERRORS: List[dict] = []


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_tool_roots() -> List[Path]:
    raw = os.getenv("DAVE_TOOL_ROOTS", "[]")
    try:
        values = json.loads(raw)
        if not isinstance(values, list):
            raise ValueError("must be a JSON array")
        roots = []
        for value in values:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError("every tool root must be an absolute path")
            roots.append(path.resolve())
        return roots
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        import logging as _logging
        _logging.getLogger("dave_llm").warning(
            "Ignoring invalid DAVE_TOOL_ROOTS configuration: %s", exc
        )
        return []


def _load_model_context_windows() -> Dict[str, int]:
    raw = os.getenv("DAVE_MODEL_CONTEXT_WINDOWS", "{}")
    try:
        values = json.loads(raw)
        if not isinstance(values, dict):
            raise ValueError("must be a JSON object")
        windows = {str(key): int(value) for key, value in values.items()}
        if any(value < 4_096 for value in windows.values()):
            raise ValueError("every model context window must be at least 4,096 tokens")
        return windows
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        import logging as _logging
        _logging.getLogger("dave_llm").warning(
            "Ignoring invalid DAVE_MODEL_CONTEXT_WINDOWS configuration: %s", exc
        )
        return {}


TOOLS_ENABLED = _env_flag("DAVE_ENABLE_TOOLS")
SHELL_TOOL_ENABLED = _env_flag("DAVE_ENABLE_SHELL_TOOL")
TOOL_ROOTS = _load_tool_roots()
MODEL_CONTEXT_WINDOWS = _load_model_context_windows()

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

# Log a startup warning when the router is running without an API key.
# The request-time guard (require_api_key) fails closed in that case so
# the service stays reachable for /health diagnostics but refuses any
# guarded endpoint until DAVE_API_KEY is set.
if API_KEY is None:
    import logging as _logging
    _logging.getLogger("dave_llm").warning(
        "DAVE_API_KEY env var is not set. All guarded endpoints will return "
        "503 until it is configured. Set DAVE_API_KEY in the environment "
        "and restart the router."
    )
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
        "session_override": "",
    },
    "code_review": {
        "title": "Code Review Session",
        "system_prompt": SYSTEM_PROMPT + "\n\nFocus on code quality, bugs, and optimization.",
        "session_override": "Focus on code quality, bugs, and optimization.",
    },
    "brainstorm": {
        "title": "Brainstorm",
        "system_prompt": SYSTEM_PROMPT + "\n\nBe exploratory and propose multiple options.",
        "session_override": "Be exploratory and propose multiple options.",
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


def load_settings() -> Dict[str, str]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with open(SETTINGS_FILE, "r") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception as e:
        print(f"⚠️ Failed to load settings: {e}")
        return {}


def save_settings(settings: Dict[str, str]):
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(settings, f, indent=2)
        tmp.replace(SETTINGS_FILE)
    except IOError as e:
        print(f"⚠️ Failed to save settings: {e}")

# Load existing conversations on startup
CONVERSATIONS: Dict[str, dict] = load_conversations()
PROJECTS: Dict[str, dict] = load_projects()
SETTINGS: Dict[str, str] = load_settings()
PROJECT_CONTEXT = ProjectContextStore(
    PROJECT_CONTEXT_DB,
    PROJECT_UPLOADS_DIR,
    default_context_budget=DEFAULT_PROJECT_CONTEXT_TOKENS,
    default_brain_threshold=DEFAULT_BRAIN_COMPACT_TOKENS,
)
for _project_id, _project in PROJECTS.items():
    PROJECT_CONTEXT.ensure_project(
        _project_id,
        instructions=str(_project.get("system_prompt") or ""),
        context_budget_tokens=_project.get("context_budget_tokens"),
    )

# ============================================================
# FASTAPI SETUP
# ============================================================

app = FastAPI(
    title="DaveLLM Router",
    version=PRODUCT_VERSION,
    description="Routes chat prompts to Ollama nodes with persistent conversation memory and metadata.",
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
    API key guard. Fails closed when DAVE_API_KEY is unset so a
    misconfigured server does not silently expose every guarded endpoint.
    503 (not 401) is returned in that case because the failure is on the
    server side, not the caller. /health remains unguarded so ops can
    still check process liveness.
    """
    if API_KEY is None:
        raise HTTPException(
            503,
            "Router is not configured: DAVE_API_KEY env var is unset. "
            "Set it and restart the router.",
        )
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
    except Exception as exc:
        # Registering zero nodes silently looks identical to "Ollama has no
        # models"; say why the inventory is empty instead.
        print(
            "❌ DAVE_NODES was set but could not be parsed, so no nodes are "
            f"registered: {exc}"
        )
        print(
            '   Expected JSON: [{"id":"<node-id>","name":"<display-name>",'
            '"url":"http://<ollama-host>:11434"}]'
        )
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

def _node_timeout() -> float:
    """Seconds to wait on a node's /api/tags. Override with DAVE_NODE_TIMEOUT."""
    raw = os.getenv("DAVE_NODE_TIMEOUT", "")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            print(f"⚠️ Ignoring invalid DAVE_NODE_TIMEOUT={raw!r}; using 10s")
    return 10.0

NODE_TIMEOUT = _node_timeout()

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
    try:
        conn = sqlite3.connect(str(FEEDBACK_DB))
        c = conn.cursor()
        c.execute("SELECT model_id, score, complexity FROM feedback ORDER BY id DESC LIMIT 500")
        for model_id, score, complexity in c.fetchall():
            MODEL_FEEDBACK[model_id].append({"score": score, "complexity": complexity})
        conn.close()
    except sqlite3.OperationalError:
        # Table may not exist yet on first run; init_feedback_db() will create it later
        pass

# load_feedback_cache() is called after init_feedback_db() below (near line 842)

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
    PROJECT_CONTEXT.ensure_project(
        project_id,
        instructions=str(proj.get("system_prompt") or ""),
        context_budget_tokens=proj.get("context_budget_tokens"),
    )
    profile = PROJECT_CONTEXT.get_profile(project_id)
    proj["system_prompt"] = profile["instructions"]
    proj["context_budget_tokens"] = profile["context_budget_tokens"]
    return proj

def list_projects_for_user(user_id: str) -> List[dict]:
    return [p for p in PROJECTS.values() if p.get("user_id", "default") == user_id]

def estimate_tokens(text: str) -> int:
    # Rough heuristic: 1 token ~ 4 chars
    return max(1, len(text) // 4)


def get_model_context_window(model_id: str) -> int:
    return int(MODEL_CONTEXT_WINDOWS.get(model_id, DEFAULT_MODEL_CONTEXT_WINDOW))


def content_token_count(content) -> int:
    if isinstance(content, str):
        return estimate_project_tokens(content)
    if isinstance(content, list):
        return sum(
            estimate_project_tokens(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return estimate_project_tokens(str(content or ""))

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

def describe_node_error(node: NodeConfig, exc: Exception) -> str:
    """Turn a transport failure into a reason an operator can act on."""
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"{node.url} did not answer /api/tags within {NODE_TIMEOUT:g}s "
            "(raise DAVE_NODE_TIMEOUT if the node is just slow)"
        )
    if isinstance(exc, httpx.ConnectError):
        return (
            f"cannot connect to {node.url} (is Ollama running and bound beyond "
            "loopback? set OLLAMA_HOST=0.0.0.0:11434 on the node)"
        )
    return f"{type(exc).__name__} talking to {node.url}: {exc}"

async def get_node_health(node: NodeConfig) -> dict:
    """Check Ollama node health and latency through its model inventory."""
    error = None
    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=NODE_TIMEOUT) as client:
            resp = await client.get(f"{node.url}/api/tags")
        latency = (time.time() - start) * 1000  # ms

        if resp.is_success:
            return {
                "status": "online",
                "latency": round(latency, 1),
                "node_id": node.id,
                "name": node.name,
                "error": None,
            }
        error = f"{node.url}/api/tags returned HTTP {resp.status_code}"
    except Exception as exc:
        error = describe_node_error(node, exc)

    return {
        "status": "offline",
        "latency": None,
        "node_id": node.id,
        "name": node.name,
        "error": error,
    }

async def fetch_node_models(node: NodeConfig) -> dict:
    """Fetch Ollama's models as {"models": [...], "error": str | None}.

    An empty list with ``error`` set means the node could not be reached; an
    empty list and no ``error`` means the node genuinely has nothing pulled.
    Callers have to be able to tell those two apart.
    """

    def detect_vision(model_obj, model_id: str) -> bool:
        """Heuristic detection of vision-capable models."""
        if not model_id:
            return False

        lid = model_id.lower()
        # Basic filename heuristics. "mm" matches as a whole token only,
        # otherwise every "gemma" tag is mislabelled as vision-capable.
        if "vision" in lid or "multimodal" in lid:
            return True
        if "mm" in re.split(r"[^a-z0-9]+", lid):
            return True
        # "vl" (vision-language) shows up alone, glued to the family name, and
        # with a version suffix: vl, qwen2.5vl, internvl2, deepseek-vl2. Require
        # a non-alphanumeric boundary after the optional version digits so that
        # longer words merely containing "vl" do not match.
        if re.search(r"vl[0-9]*(?![a-z0-9])", lid):
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
        async with httpx.AsyncClient(timeout=NODE_TIMEOUT) as client:
            resp = await client.get(f"{node.url}/api/tags")
        if resp.is_success:
            data = resp.json()

            raw_models = []
            for m in data.get("models") or []:
                if isinstance(m, dict) and "id" not in m:
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
                return {"models": normalized_models, "error": None}

            print(
                f"⚠️ Node {node.name} is reachable but has no models pulled "
                f"(run `ollama pull <model>` on {node.url})"
            )
            return {"models": [], "error": None}

        error = f"{node.url}/api/tags returned HTTP {resp.status_code}"
        print(f"⚠️ Node {node.name}: {error}")
        return {"models": [], "error": error}
    except Exception as exc:
        error = describe_node_error(node, exc)
        print(f"⚠️ Node {node.name}: {error}")
        return {"models": [], "error": error}

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

# Load feedback cache after DB tables exist
load_feedback_cache()

# ============================================================
# TOOL EXECUTION SYSTEM
# ============================================================

import subprocess
import platform

class ToolRequest(BaseModel):
    """Tool execution request from LLM."""
    tool: str
    params: Dict = Field(default_factory=dict)

class ToolResult(BaseModel):
    """Result of tool execution."""
    tool: str
    status: str  # "success" or "error"
    result: str
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[float] = None
    termination: Optional[str] = None


def validate_public_url(url: str) -> None:
    """Resolve a URL hostname and reject every non-public address."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute http/https URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not allowed")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        resolved = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Hostname resolution failed: {exc}") from exc
    if not resolved:
        raise ValueError("Hostname did not resolve")

    for entry in resolved:
        address = ipaddress.ip_address(entry[4][0])
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or not address.is_global
        ):
            raise ValueError(f"Resolved address is not public: {address}")


def resolve_tool_path(path: str) -> Path:
    """Resolve a tool path and require containment in an explicit root."""
    if not path:
        raise ValueError("Missing path")
    candidate = Path(path).expanduser().resolve()
    if not any(candidate == root or candidate.is_relative_to(root) for root in TOOL_ROOTS):
        raise PermissionError("Access denied: path is outside DAVE_TOOL_ROOTS")
    return candidate

async def execute_tool(tool_req: ToolRequest) -> ToolResult:
    """Run one registered tool through the shared validated dispatcher."""
    if not TOOLS_ENABLED:
        return ToolResult(
            tool=tool_req.tool,
            status="error",
            result="",
            error="Tools are disabled. Set DAVE_ENABLE_TOOLS=true to enable them.",
        )

    execution = await run_tool(
        tool_req.tool,
        tool_req.params,
        registry=TOOL_REGISTRY,
    )
    return ToolResult(
        tool=execution.name,
        status="success" if execution.status == "success" else "error",
        result=execution.result,
        error=execution.error,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        duration_ms=execution.duration_ms,
        termination=execution.termination,
    )

def tool_file_read(params: Dict) -> ToolResult:
    """Read file contents with size limit."""
    try:
        safe_path = resolve_tool_path(params.get("path", ""))
        
        # Check file size
        if safe_path.stat().st_size > MAX_FILE_SIZE:
            return ToolResult(tool="file.read", status="error", result="", error=f"File too large (max {MAX_FILE_SIZE // 1024 // 1024}MB)")
        
        with open(safe_path, "r") as f:
            content = f.read()
        
        return ToolResult(tool="file.read", status="success", result=content[:MAX_TOOL_OUTPUT])
    except Exception as e:
        return ToolResult(tool="file.read", status="error", result="", error=str(e))

def tool_file_write(params: Dict) -> ToolResult:
    """Write to file with size limit and subdirectory allowlist."""
    try:
        path = params.get("path", "")
        content = params.get("content", "")

        # Check content size
        if len(content) > MAX_FILE_SIZE:
            return ToolResult(tool="file.write", status="error", result="", error=f"Content too large (max {MAX_FILE_SIZE // 1024 // 1024}MB)")

        safe_path = resolve_tool_path(path)

        safe_path.parent.mkdir(parents=True, exist_ok=True)
        with open(safe_path, "w") as f:
            f.write(content)
        
        return ToolResult(tool="file.write", status="success", result=f"Wrote {len(content)} bytes to {safe_path}")
    except Exception as e:
        return ToolResult(tool="file.write", status="error", result="", error=str(e))

def tool_file_append(params: Dict) -> ToolResult:
    """Append to file."""
    try:
        path = params.get("path", "")
        content = params.get("content", "")
        
        if len(content) > MAX_FILE_SIZE:
            return ToolResult(tool="file.append", status="error", result="", error=f"Content too large (max {MAX_FILE_SIZE // 1024 // 1024}MB)")

        safe_path = resolve_tool_path(path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        with open(safe_path, "a") as f:
            f.write(content)
        
        return ToolResult(tool="file.append", status="success", result=f"Appended {len(content)} bytes to {safe_path}")
    except Exception as e:
        return ToolResult(tool="file.append", status="error", result="", error=str(e))

async def tool_web_fetch(params: Dict) -> ToolResult:
    """Fetch bounded public HTTP content with redirect-by-redirect validation."""
    try:
        url = params.get("url", "")
        if not url:
            return ToolResult(tool="web.fetch", status="error", result="", error="Missing url")

        current_url = url
        async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
            for redirect_count in range(MAX_WEB_FETCH_REDIRECTS + 1):
                await asyncio.to_thread(validate_public_url, current_url)
                async with client.stream("GET", current_url) as resp:
                    if resp.status_code in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("location")
                        if not location:
                            raise ValueError("Redirect response is missing Location")
                        if redirect_count >= MAX_WEB_FETCH_REDIRECTS:
                            raise ValueError("Too many redirects")
                        current_url = urljoin(current_url, location)
                        continue

                    resp.raise_for_status()
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_WEB_FETCH_BYTES:
                            raise ValueError(
                                f"Response exceeds {MAX_WEB_FETCH_BYTES} byte limit"
                            )
                    text = bytes(body).decode("utf-8", errors="replace")
                    return ToolResult(
                        tool="web.fetch",
                        status="success",
                        result=text[:MAX_TOOL_OUTPUT],
                    )
        raise ValueError("Fetch did not produce a response")
    except Exception as e:
        return ToolResult(tool="web.fetch", status="error", result="", error=str(e))

def tool_system_info(params: Dict) -> ToolResult:
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

def tool_shell_exec(params: Dict) -> ToolResult:
    """Execute shell command (restricted whitelist only)."""
    try:
        if not SHELL_TOOL_ENABLED:
            return ToolResult(
                tool="shell.exec",
                status="error",
                result="",
                error="shell.exec is disabled. Set DAVE_ENABLE_SHELL_TOOL=true to enable it.",
            )
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


ASYNC_TOOL_HANDLER_ALLOWLIST = frozenset({"web.fetch"})
TOOL_REGISTRY = ToolRegistry(
    async_handler_allowlist=ASYNC_TOOL_HANDLER_ALLOWLIST,
)
PENDING_CALL_STORE = InMemoryPendingCallStore()


def register_builtin_tools() -> None:
    """Load built-in schemas and handlers into the revocable runtime registry."""
    definitions = [
        ToolDefinition(
            name="system.info",
            description="Read bounded operating system and Python runtime information.",
            parameters={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "all",
                            "platform",
                            "platform_version",
                            "python_version",
                            "processor",
                        ],
                    }
                },
                "additionalProperties": False,
            },
            handler=tool_system_info,
            permission="read_system",
            cancellation="bounded",
        ),
        ToolDefinition(
            name="file.read",
            description="Read a UTF-8 text file inside an allowed tool root.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=tool_file_read,
            permission="read_files",
            cancellation="bounded",
        ),
        ToolDefinition(
            name="file.write",
            description="Write UTF-8 text inside an allowed tool root.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=tool_file_write,
            permission="write_files",
            approval_required=True,
            cancellation="bounded",
        ),
        ToolDefinition(
            name="file.append",
            description="Append UTF-8 text inside an allowed tool root.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=tool_file_append,
            permission="write_files",
            approval_required=True,
            cancellation="bounded",
        ),
        ToolDefinition(
            name="web.fetch",
            description="Fetch a bounded public HTTP or HTTPS text response.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            handler=tool_web_fetch,
            permission="public_network",
            cancellation="bounded",
            async_handler=True,
        ),
    ]
    if SHELL_TOOL_ENABLED:
        definitions.append(
            ToolDefinition(
                name="shell.exec",
                description="Execute one command from the fixed safe-command allowlist.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "minLength": 1},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                handler=tool_shell_exec,
                permission="execute_process",
                approval_required=True,
                timeout_seconds=6,
                cancellation="bounded",
            )
        )
    for definition in definitions:
        TOOL_REGISTRY.register(definition)


register_builtin_tools()

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
            "session_override": "",
            "instruction_mode": "layered",
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


def get_leading_system_messages(messages: List[dict]) -> List[dict]:
    """Return legacy instruction messages stored at the start of a conversation."""
    leading = []
    for message in messages:
        if message.get("role") != "system":
            break
        leading.append(message)
    return leading


def get_global_system_prompt() -> str:
    """Return the editable runtime default or the source-controlled fallback."""
    configured = SETTINGS.get("global_system_prompt")
    return configured if isinstance(configured, str) and configured.strip() else SYSTEM_PROMPT


def normalize_session_instructions(conversation: dict, project: dict) -> tuple[str, str]:
    """Map existing prompt snapshots into layered instructions without losing text."""
    if "session_override" in conversation:
        return (
            str(conversation.get("session_override") or ""),
            str(conversation.get("instruction_mode") or "layered"),
        )

    legacy_instructions = [
        message.get("content", "").strip()
        for message in get_leading_system_messages(conversation.get("messages", []))
        if message.get("content", "").strip()
    ]
    legacy_prompt = conversation.get("system_prompt") or (
        "\n\n".join(legacy_instructions) if legacy_instructions else ""
    )
    if not legacy_prompt:
        return "", "layered"

    global_default = get_global_system_prompt()
    project_prompt = str(project.get("system_prompt") or "")
    inherited_prompt = compose_system_instructions(
        global_default,
        project_prompt,
        "",
    )
    if legacy_prompt == inherited_prompt:
        return "", "layered"
    prefix = f"{inherited_prompt}\n\n"
    if legacy_prompt.startswith(prefix):
        candidate = legacy_prompt[len(prefix):]
        if compose_system_instructions(
            global_default,
            project_prompt,
            candidate,
        ) == legacy_prompt:
            return candidate, "layered"
    return str(legacy_prompt), "replace"


def compose_system_instructions(
    global_default: str,
    project_instructions: str,
    session_override: str,
    *,
    mode: str = "layered",
) -> str:
    """Compose one exact system message with visible lowest-to-highest precedence."""
    if mode == "replace" and session_override.strip():
        return session_override.strip()

    parts = [global_default.strip()]
    if project_instructions.strip():
        parts.append(f"PROJECT INSTRUCTIONS\n{project_instructions.strip()}")
    if session_override.strip():
        parts.append(f"SESSION OVERRIDE\n{session_override.strip()}")
    return "\n\n".join(part for part in parts if part)


def get_instruction_layers(conversation: dict, project: dict) -> dict:
    """Return editable layers and the exact effective system instruction text."""
    global_default = get_global_system_prompt()
    project_instructions = str(project.get("system_prompt") or "")
    session_override, mode = normalize_session_instructions(conversation, project)
    effective = compose_system_instructions(
        global_default,
        project_instructions,
        session_override,
        mode=mode,
    )
    return {
        "precedence": ["global_default", "project_instructions", "session_override"],
        "merge_rule": (
            "Later non-empty layers have higher precedence. Legacy replace mode keeps "
            "the prior session snapshot exact until it is saved or reverted."
        ),
        "mode": mode,
        "layers": {
            "global_default": {
                "label": "Global default",
                "content": global_default,
                "editable": True,
                "priority": 1,
            },
            "project_instructions": {
                "label": "Project instructions",
                "content": project_instructions,
                "editable": bool(project),
                "priority": 2,
            },
            "session_override": {
                "label": "Session override",
                "content": session_override,
                "editable": True,
                "priority": 3,
            },
        },
        "effective": effective,
        "character_count": len(effective),
        "token_estimate": estimate_tokens(effective),
    }


def resolve_conversation_system_prompt(conversation: dict, project: dict) -> str:
    """Resolve the exact active instruction text from the three visible layers."""
    if "session_override" not in conversation:
        session_override, mode = normalize_session_instructions(conversation, project)
        conversation["session_override"] = session_override
        conversation["instruction_mode"] = mode
    return get_instruction_layers(conversation, project)["effective"]


def prepare_history_for_prompt(messages: List[dict]) -> List[dict]:
    """Remove persisted legacy instructions, then prune and summarize chat history."""
    legacy_count = len(get_leading_system_messages(messages))
    return prune_conversation_history(messages[legacy_count:])


def build_messages_for_node(
    system_prompt: str,
    history: List[dict],
    project_context: Optional[List[dict]] = None,
) -> List[dict]:
    """Build an Ollama payload with instructions before bounded project evidence."""
    return [
        {"role": "system", "content": system_prompt},
        *[dict(message) for message in (project_context or [])],
        *[dict(message) for message in history],
    ]


def build_project_messages_for_node(
    *,
    project_id: Optional[str],
    project: dict,
    model_id: str,
    output_reserve: int,
    query: str,
    system_prompt: str,
    history: List[dict],
) -> tuple[List[dict], dict]:
    """Assemble the P4 order and reserve exact room for all four components."""
    if not project_id:
        return build_messages_for_node(system_prompt, history), {}
    base_messages = build_messages_for_node(system_prompt, history)
    base_tokens = sum(content_token_count(message.get("content")) for message in base_messages)
    project_instruction_tokens = estimate_project_tokens(project.get("system_prompt") or "")
    non_project_tokens = max(0, base_tokens - project_instruction_tokens)
    context_window = get_model_context_window(model_id)
    safety_margin = max(512, round(context_window * 0.05))
    available_project_tokens = (
        context_window
        - max(1, int(output_reserve or 2048))
        - safety_margin
        - non_project_tokens
    )
    assembled = PROJECT_CONTEXT.build_context_messages(
        project_id,
        query=query,
        available_tokens=available_project_tokens,
    )
    return (
        build_messages_for_node(system_prompt, history, assembled["messages"]),
        {
            **assembled["budget"],
            "model_context_window": context_window,
            "output_reserve": max(1, int(output_reserve or 2048)),
            "safety_margin": safety_margin,
            "non_project_tokens": non_project_tokens,
        },
    )

def generate_conversation_summary(older_messages: List[dict]) -> str:
    """Use a cheap local model to summarize older turns."""
    if not older_messages:
        return ""

    available = [
        (node, model_id)
        for node in NODE_CONFIGS
        for model_id in sorted(MODEL_INVENTORY.get(node.id, set()))
    ]
    if not available:
        return f"[Earlier conversation summary over {len(older_messages)} messages]"
    node, summary_model = available[0]

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

class TemplateConversationRequest(BaseModel):
    template_name: str
    project_id: Optional[str] = None

class ModelDownloadRequest(BaseModel):
    url: str
    dest_path: Optional[str] = None

class ProjectCreate(BaseModel):
    name: str
    system_prompt: Optional[str] = None
    preferred_model: Optional[str] = None
    preferred_node: Optional[str] = None
    max_budget: Optional[float] = None
    description: Optional[str] = None
    context_budget_tokens: Optional[int] = Field(default=16_384, ge=1_024, le=262_144)
    archived: bool = False

class ProjectResponse(BaseModel):
    project_id: str
    name: str
    system_prompt: Optional[str] = None
    preferred_model: Optional[str] = None
    preferred_node: Optional[str] = None
    max_budget: Optional[float] = None
    created_at: Optional[str] = None
    user_id: Optional[str] = None
    description: Optional[str] = None
    notepad: str = ""
    context_budget_tokens: int = 16_384
    archived: bool = False

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    system_prompt: Optional[str] = None
    preferred_model: Optional[str] = None
    preferred_node: Optional[str] = None
    max_budget: Optional[float] = None
    description: Optional[str] = None
    context_budget_tokens: Optional[int] = Field(default=None, ge=1_024, le=262_144)
    archived: Optional[bool] = None

class ResyncRequest(BaseModel):
    project_id: Optional[str] = None


class InstructionUpdate(BaseModel):
    global_default: Optional[str] = None
    project_instructions: Optional[str] = None
    session_override: Optional[str] = None

    @field_validator("global_default", "project_instructions", "session_override")
    @classmethod
    def validate_instruction_size(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value) > 100_000:
            raise ValueError("Instruction layer exceeds 100,000 characters")
        return value


class NotepadUpdate(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def validate_notepad_size(cls, value: str) -> str:
        if len(value) > 200_000:
            raise ValueError("Notepad exceeds 200,000 characters")
        return value


class BrainUpdate(BaseModel):
    pinned_text: Optional[str] = None
    active_text: Optional[str] = None
    recent_text: Optional[str] = None
    compact_threshold: Optional[int] = Field(default=None, ge=128, le=262_144)
    expected_revision: Optional[int] = Field(default=None, ge=1)

    @field_validator("pinned_text", "active_text", "recent_text")
    @classmethod
    def validate_brain_text(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value) > 1_000_000:
            raise ValueError("A BRAIN tier exceeds 1,000,000 characters")
        return value


class ArtifactUpdate(BaseModel):
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    title: Optional[str] = Field(default=None, max_length=300)


class ProjectFileUpdate(BaseModel):
    attached: bool


class ProjectAttachmentUpdate(BaseModel):
    project_id: Optional[str] = None


class ProjectContextPreviewRequest(BaseModel):
    query: str = Field(default="", max_length=1_000_000)
    conversation_id: Optional[str] = None
    model: Optional[str] = None
    max_tokens: int = Field(default=2_048, ge=1, le=262_144)


class AgentRunRequest(BaseModel):
    messages: List[Dict]
    node_id: str
    model: str
    max_tokens: int = 2048
    temperature: float = 0.7
    step_limit: int = DEFAULT_STEP_LIMIT
    error_budget: int = DEFAULT_ERROR_BUDGET
    approved_tools: List[str] = Field(default_factory=list)

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, value: List[Dict]) -> List[Dict]:
        if not value:
            raise ValueError("At least one message is required")
        if len(value) > 200:
            raise ValueError("Message array exceeds 200 entries")
        if len(json.dumps(value, ensure_ascii=False)) > 1_000_000:
            raise ValueError("Message payload exceeds 1,000,000 characters")
        allowed_roles = {"system", "user", "assistant", "tool"}
        for index, message in enumerate(value):
            if message.get("role") not in allowed_roles:
                raise ValueError(f"messages[{index}] has an unsupported role")
            if "content" not in message and "tool_calls" not in message:
                raise ValueError(
                    f"messages[{index}] requires content or tool_calls"
                )
        return value

    @field_validator("step_limit")
    @classmethod
    def validate_step_limit(cls, value: int) -> int:
        if not 1 <= value <= 32:
            raise ValueError("step_limit must be between 1 and 32")
        return value

    @field_validator("error_budget")
    @classmethod
    def validate_error_budget(cls, value: int) -> int:
        if not 1 <= value <= 8:
            raise ValueError("error_budget must be between 1 and 8")
        return value


class AgentResumeRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=256)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "deny"]


BRAIN_COMPACTION_QUEUE: asyncio.Queue[str] = asyncio.Queue()
BRAIN_COMPACTION_PENDING: set[str] = set()
BACKGROUND_TASKS: list[asyncio.Task] = []


def context_http_error(exc: ProjectContextError) -> HTTPException:
    if isinstance(exc, RevisionConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, ContextBudgetError):
        return HTTPException(422, str(exc))
    return HTTPException(400, str(exc))


def backfill_project_artifacts(project_id: str, user_id: str) -> None:
    """Make existing assistant outputs retrievable without rewriting conversations."""
    get_project(project_id, user_id)
    for conversation_id, conversation in CONVERSATIONS.items():
        if conversation.get("user_id", "default") != user_id:
            continue
        if conversation.get("project_id") != project_id:
            continue
        for index, message in enumerate(conversation.get("messages", [])):
            if message.get("role") != "assistant" or not isinstance(message.get("content"), str):
                continue
            PROJECT_CONTEXT.add_artifact(
                project_id,
                title=conversation.get("title") or "Assistant output",
                body=message["content"],
                conversation_id=conversation_id,
                source_message_index=index,
            )


def capture_project_artifact(
    project_id: Optional[str],
    conversation_id: str,
    message_index: int,
    body: str,
) -> None:
    if not project_id or not body:
        return
    try:
        conversation = CONVERSATIONS.get(conversation_id, {})
        PROJECT_CONTEXT.add_artifact(
            project_id,
            title=conversation.get("title") or "Assistant output",
            body=body,
            conversation_id=conversation_id,
            source_message_index=message_index,
        )
    except ProjectContextError as exc:
        record_error("artifact_capture", str(exc))


def enqueue_brain_compaction(project_id: str) -> bool:
    if project_id in BRAIN_COMPACTION_PENDING:
        return False
    BRAIN_COMPACTION_PENDING.add(project_id)
    BRAIN_COMPACTION_QUEUE.put_nowait(project_id)
    return True


async def brain_compaction_worker() -> None:
    """Compact queued projects and reconcile threshold crossings once per day."""
    reconciliation_interval = 86_400
    next_reconciliation = time.monotonic() + reconciliation_interval
    while True:
        queued_project: Optional[str] = None
        try:
            timeout = max(0.0, next_reconciliation - time.monotonic())
            queued_project = await asyncio.wait_for(
                BRAIN_COMPACTION_QUEUE.get(),
                timeout=timeout,
            )
            await asyncio.to_thread(PROJECT_CONTEXT.compact_brain, queued_project)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        except ProjectContextError as exc:
            record_error("brain_compaction", str(exc))
        except Exception as exc:
            record_error("brain_compaction", str(exc))
        finally:
            if queued_project is not None:
                BRAIN_COMPACTION_PENDING.discard(queued_project)
                BRAIN_COMPACTION_QUEUE.task_done()
        if time.monotonic() >= next_reconciliation:
            try:
                await asyncio.to_thread(
                    PROJECT_CONTEXT.purge_expired_brains,
                    BRAIN_RECOVERY_DAYS,
                )
                for project_id in list(PROJECTS):
                    try:
                        brain = PROJECT_CONTEXT.get_brain(project_id)
                        if brain["should_compact"]:
                            enqueue_brain_compaction(project_id)
                    except ProjectContextError as exc:
                        record_error("brain_reconcile", str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                record_error("brain_reconcile", str(exc))
            finally:
                next_reconciliation = time.monotonic() + reconciliation_interval

# ============================================================
# ROUTES
# ============================================================

@app.get("/health")
def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "version": PRODUCT_VERSION,
        "nodes": [n.model_dump() for n in NODE_CONFIGS],
        "active_conversations": len(CONVERSATIONS)
    }           

@app.get("/nodes")
def list_nodes(_auth=Depends(require_api_key)):
    """List all configured Ollama nodes."""
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

    result = await fetch_node_models(node)
    models = result["models"]
    error = result["error"]

    # Only publish an inventory we actually read. Overwriting it after a
    # transient failure would reject models the node really does serve.
    if error is None:
        MODEL_INVENTORY[node_id] = {model["id"] for model in models}

    return {
        "node_id": node_id,
        "node_name": node.name,
        "models": models,
        "error": error,
    }

ALLOWED_MODEL_DOWNLOAD_HOSTS = ("huggingface.co", "hf.co")
MAX_MODEL_DOWNLOAD_REDIRECTS = 5

def _validate_model_download_url(url: str) -> str:
    """
    Allow only credential-free URLs whose exact host is Hugging Face (or a
    subdomain, such as the cdn-lfs endpoints large files redirect to).
    Substring checks on the netloc are bypassable with userinfo tricks like
    http://huggingface.co@127.0.0.1:8000/, which is an SSRF against loopback
    services, so match the parsed hostname exactly.
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Only http/https URLs are supported")
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(400, "URLs with embedded credentials are not allowed")
    host = (parsed.hostname or "").lower()
    if not any(
        host == allowed or host.endswith("." + allowed)
        for allowed in ALLOWED_MODEL_DOWNLOAD_HOSTS
    ):
        raise HTTPException(400, "Only Hugging Face URLs are allowed for safety")
    return url

@app.api_route("/models/download", methods=["POST", "OPTIONS"])
def download_model(req: ModelDownloadRequest, request: Request, _auth=Depends(require_api_key)):
    """
    Download a model file from Hugging Face (or HTTP/HTTPS) into the local models directory.
    """
    if request.method == "OPTIONS":
        return {"status": "ok"}
    url = _validate_model_download_url(req.url.strip())
    parsed = urlparse(url)

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
    wrote_file = False
    try:
        # Follow redirects manually so every hop is revalidated against the
        # Hugging Face allowlist instead of trusting wherever the first
        # response points.
        redirects = 0
        while True:
            r = requests.get(url, stream=True, timeout=30, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                location = r.headers.get("Location", "")
                r.close()
                redirects += 1
                if redirects > MAX_MODEL_DOWNLOAD_REDIRECTS:
                    raise HTTPException(400, "Too many redirects")
                if not location:
                    raise HTTPException(502, "Redirect response missing a Location header")
                url = _validate_model_download_url(urljoin(url, location).strip())
                continue
            break
        with r:
            r.raise_for_status()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                wrote_file = True
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(400, "File too large (over 10GB limit)")
                    f.write(chunk)
    except HTTPException:
        if wrote_file:
            dest_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        if wrote_file:
            dest_path.unlink(missing_ok=True)
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
    if not WHISPER_BIN.is_file() or not os.access(WHISPER_BIN, os.X_OK):
        raise HTTPException(500, f"Whisper binary not found at {WHISPER_BIN}")
    if not WHISPER_MODEL.is_file():
        raise HTTPException(500, f"Whisper model not found at {WHISPER_MODEL}")

    tmp_id = uuid.uuid4().hex
    tmp_prefix = BASE_DIR / f"tmp_audio_{tmp_id}"
    tmp_input = Path(f"{tmp_prefix}.bin")
    tmp_wav = Path(f"{tmp_prefix}.wav")
    tmp_out = Path(f"{tmp_prefix}.txt")

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
            str(tmp_prefix),
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

def project_response(project_id: str, project: dict) -> ProjectResponse:
    return ProjectResponse(
        project_id=project_id,
        name=project.get("name", ""),
        system_prompt=project.get("system_prompt"),
        preferred_model=project.get("preferred_model"),
        preferred_node=project.get("preferred_node"),
        max_budget=project.get("max_budget"),
        created_at=project.get("created_at"),
        user_id=project.get("user_id"),
        description=project.get("description"),
        notepad=project.get("notepad", ""),
        context_budget_tokens=int(project.get("context_budget_tokens") or 16_384),
        archived=bool(project.get("archived", False)),
    )


@app.get("/projects")
def list_projects(user_id: str = Depends(get_current_user)):
    """List projects owned by the current user."""
    projects = []
    for project_id, project in PROJECTS.items():
        if project.get("user_id", "default") != user_id:
            continue
        project = get_project(project_id, user_id)
        projects.append(project_response(project_id, project).model_dump())
    return {"projects": projects}


@app.get("/instructions/global")
def get_global_instructions(_auth=Depends(require_api_key)):
    content = get_global_system_prompt()
    return {
        "content": content,
        "is_source_default": "global_system_prompt" not in SETTINGS,
        "character_count": len(content),
        "token_estimate": estimate_tokens(content),
    }


@app.delete("/instructions/global")
def reset_global_instructions(_auth=Depends(require_api_key)):
    """Remove the runtime override and restore the source-controlled default."""
    SETTINGS.pop("global_system_prompt", None)
    save_settings(SETTINGS)
    content = get_global_system_prompt()
    return {
        "content": content,
        "is_source_default": True,
        "character_count": len(content),
        "token_estimate": estimate_tokens(content),
    }

@app.post("/projects", response_model=ProjectResponse)
def create_project(req: ProjectCreate, user_id: str = Depends(get_current_user)):
    """Create a new project/space for scoping conversations."""
    project_id = f"proj_{int(time.time() * 1000)}"
    project = {
        "name": req.name.strip(),
        "system_prompt": req.system_prompt or "",
        "preferred_model": req.preferred_model,
        "preferred_node": req.preferred_node,
        "max_budget": req.max_budget,
        "created_at": datetime.now().isoformat(),
        "user_id": user_id,
        "description": req.description or "",
        "notepad": "",
        "context_budget_tokens": req.context_budget_tokens or 16_384,
        "archived": req.archived,
    }
    PROJECTS[project_id] = project
    try:
        PROJECT_CONTEXT.ensure_project(
            project_id,
            instructions=project["system_prompt"],
            context_budget_tokens=project["context_budget_tokens"],
        )
        PROJECT_CONTEXT.update_profile(
            project_id,
            instructions=project["system_prompt"],
            context_budget_tokens=project["context_budget_tokens"],
        )
    except ProjectContextError as exc:
        PROJECTS.pop(project_id, None)
        PROJECT_CONTEXT.delete_project(project_id)
        raise context_http_error(exc)
    save_projects(PROJECTS)
    return project_response(project_id, project)

@app.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project_endpoint(project_id: str, user_id: str = Depends(get_current_user)):
    proj = get_project(project_id, user_id)
    return project_response(project_id, proj)

@app.put("/projects/{project_id}", response_model=ProjectResponse)
def update_project(project_id: str, req: ProjectUpdate, user_id: str = Depends(get_current_user)):
    proj = get_project(project_id, user_id)
    updates = req.model_dump(exclude_unset=True)
    try:
        profile = PROJECT_CONTEXT.update_profile(
            project_id,
            instructions=updates.get("system_prompt"),
            context_budget_tokens=updates.get("context_budget_tokens"),
        )
    except ProjectContextError as exc:
        raise context_http_error(exc)
    for key, val in updates.items():
        if val is not None:
            proj[key] = val
    proj["system_prompt"] = profile["instructions"]
    proj["context_budget_tokens"] = profile["context_budget_tokens"]
    proj["updated_at"] = datetime.now().isoformat()
    PROJECTS[project_id] = proj
    save_projects(PROJECTS)
    return project_response(project_id, proj)


@app.get("/projects/{project_id}/homepage")
def get_project_homepage(project_id: str, user_id: str = Depends(get_current_user)):
    """Return one inspectable surface for the four project-context components."""
    project = get_project(project_id, user_id)
    backfill_project_artifacts(project_id, user_id)
    try:
        homepage = PROJECT_CONTEXT.homepage(project_id)
    except ProjectContextError as exc:
        raise context_http_error(exc)
    return {
        "project": project_response(project_id, project).model_dump(),
        "attached_conversation_ids": [
            conversation_id
            for conversation_id, conversation in CONVERSATIONS.items()
            if conversation.get("user_id", "default") == user_id
            and conversation.get("project_id") == project_id
        ],
        **homepage,
    }


@app.post("/projects/{project_id}/context-preview")
def preview_project_context(
    project_id: str,
    req: ProjectContextPreviewRequest,
    user_id: str = Depends(get_current_user),
):
    """Assemble the exact next-request messages without calling a model."""
    project = get_project(project_id, user_id)
    backfill_project_artifacts(project_id, user_id)
    history: List[dict] = []
    if req.conversation_id:
        assert_convo_owner(req.conversation_id, user_id)
        stored_conversation = CONVERSATIONS[req.conversation_id]
        if stored_conversation.get("project_id") != project_id:
            raise HTTPException(
                409,
                "Context preview requires a conversation explicitly attached to this project",
            )
        conversation = dict(stored_conversation)
        history = [dict(message) for message in stored_conversation.get("messages", [])]
    else:
        conversation = {
            "session_override": "",
            "instruction_mode": "layered",
            "messages": [],
        }
    history.append({"role": "user", "content": req.query or "[empty prompt]"})
    prepared_history = prepare_history_for_prompt(history)
    system_prompt = resolve_conversation_system_prompt(conversation, project)
    model_id = req.model or project.get("preferred_model") or ""
    try:
        messages, budget = build_project_messages_for_node(
            project_id=project_id,
            project=project,
            model_id=model_id,
            output_reserve=req.max_tokens,
            query=req.query,
            system_prompt=system_prompt,
            history=prepared_history,
        )
    except ProjectContextError as exc:
        raise context_http_error(exc)
    return {
        "project_id": project_id,
        "conversation_id": req.conversation_id,
        "model": model_id or None,
        "messages": messages,
        "budget": budget,
    }


@app.post("/projects/{project_id}/files")
async def upload_project_file(
    project_id: str,
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    """Store a project reference and index supported UTF-8 text locally."""
    get_project(project_id, user_id)
    content = await file.read(MAX_FILE_SIZE + 1)
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, f"Project file exceeds {MAX_FILE_SIZE // 1024 // 1024}MB")
    try:
        return PROJECT_CONTEXT.add_file(
            project_id,
            display_name=file.filename or "reference",
            media_type=file.content_type,
            content=content,
        )
    except ProjectContextError as exc:
        raise context_http_error(exc)


@app.get("/projects/{project_id}/files")
def list_project_files(project_id: str, user_id: str = Depends(get_current_user)):
    get_project(project_id, user_id)
    return {"files": PROJECT_CONTEXT.list_files(project_id)}


@app.put("/projects/{project_id}/files/{file_id}")
def update_project_file(
    project_id: str,
    file_id: str,
    req: ProjectFileUpdate,
    user_id: str = Depends(get_current_user),
):
    get_project(project_id, user_id)
    try:
        return PROJECT_CONTEXT.set_file_attached(
            project_id,
            file_id,
            req.attached,
        )
    except ProjectContextError as exc:
        raise context_http_error(exc)


@app.post("/projects/{project_id}/files/{file_id}/reindex")
def reindex_project_file(
    project_id: str,
    file_id: str,
    user_id: str = Depends(get_current_user),
):
    get_project(project_id, user_id)
    try:
        return PROJECT_CONTEXT.reindex_file(project_id, file_id)
    except ProjectContextError as exc:
        raise context_http_error(exc)


@app.delete("/projects/{project_id}/files/{file_id}")
def delete_project_file(
    project_id: str,
    file_id: str,
    user_id: str = Depends(get_current_user),
):
    get_project(project_id, user_id)
    try:
        PROJECT_CONTEXT.delete_file(project_id, file_id)
    except ProjectContextError as exc:
        raise context_http_error(exc)
    return {"status": "deleted", "project_id": project_id, "file_id": file_id}


@app.get("/projects/{project_id}/artifacts")
def list_project_artifacts(
    project_id: str,
    include_archived: bool = False,
    user_id: str = Depends(get_current_user),
):
    get_project(project_id, user_id)
    backfill_project_artifacts(project_id, user_id)
    return {
        "artifacts": PROJECT_CONTEXT.list_artifacts(
            project_id,
            include_archived=include_archived,
        )
    }


@app.get("/projects/{project_id}/artifacts/{artifact_id}")
def get_project_artifact(
    project_id: str,
    artifact_id: str,
    user_id: str = Depends(get_current_user),
):
    get_project(project_id, user_id)
    try:
        return PROJECT_CONTEXT.get_artifact(project_id, artifact_id)
    except ProjectContextError as exc:
        raise context_http_error(exc)


@app.put("/projects/{project_id}/artifacts/{artifact_id}")
def update_project_artifact(
    project_id: str,
    artifact_id: str,
    req: ArtifactUpdate,
    user_id: str = Depends(get_current_user),
):
    get_project(project_id, user_id)
    try:
        return PROJECT_CONTEXT.update_artifact(
            project_id,
            artifact_id,
            **req.model_dump(exclude_unset=True),
        )
    except ProjectContextError as exc:
        raise context_http_error(exc)


@app.delete("/projects/{project_id}/artifacts/{artifact_id}")
def delete_project_artifact(
    project_id: str,
    artifact_id: str,
    user_id: str = Depends(get_current_user),
):
    get_project(project_id, user_id)
    try:
        PROJECT_CONTEXT.delete_artifact(project_id, artifact_id)
    except ProjectContextError as exc:
        raise context_http_error(exc)
    return {"status": "deleted", "project_id": project_id, "artifact_id": artifact_id}


@app.get("/projects/{project_id}/brain")
def get_project_brain(project_id: str, user_id: str = Depends(get_current_user)):
    get_project(project_id, user_id)
    return PROJECT_CONTEXT.get_brain(project_id)


@app.put("/projects/{project_id}/brain")
async def update_project_brain(
    project_id: str,
    req: BrainUpdate,
    user_id: str = Depends(get_current_user),
):
    project = get_project(project_id, user_id)
    current = PROJECT_CONTEXT.get_brain(project_id)
    values = req.model_dump(exclude_unset=True)
    pinned = values.get("pinned_text", current["pinned_text"])
    active = values.get("active_text", current["active_text"])
    quotas = component_quotas(project["context_budget_tokens"])
    instruction_tokens = estimate_project_tokens(project.get("system_prompt") or "")
    brain_allowance = quotas["brain"] + max(
        0,
        quotas["project_instructions"] - instruction_tokens,
    )
    if estimate_project_tokens(f"{pinned}\n{active}") > brain_allowance:
        raise HTTPException(422, "Pinned and active BRAIN content exceed the protected allocation")
    if values.get("compact_threshold", current["compact_threshold"]) > brain_allowance:
        raise HTTPException(422, "BRAIN compaction threshold exceeds its available allocation")
    try:
        brain = PROJECT_CONTEXT.update_brain(project_id, **values)
    except ProjectContextError as exc:
        raise context_http_error(exc)
    queued = enqueue_brain_compaction(project_id) if brain["should_compact"] else False
    return {**brain, "compaction_queued": queued}


@app.post("/projects/{project_id}/brain/compact")
async def compact_project_brain(
    project_id: str,
    user_id: str = Depends(get_current_user),
):
    get_project(project_id, user_id)
    try:
        return await asyncio.to_thread(
            PROJECT_CONTEXT.compact_brain,
            project_id,
            force=True,
            reason="explicit",
        )
    except ProjectContextError as exc:
        raise context_http_error(exc)


@app.get("/projects/{project_id}/brain/revisions")
def list_project_brain_revisions(
    project_id: str,
    user_id: str = Depends(get_current_user),
):
    get_project(project_id, user_id)
    return {"revisions": PROJECT_CONTEXT.list_brain_revisions(project_id)}


@app.post("/projects/{project_id}/brain/revisions/{revision}/restore")
def restore_project_brain(
    project_id: str,
    revision: int,
    user_id: str = Depends(get_current_user),
):
    get_project(project_id, user_id)
    try:
        return PROJECT_CONTEXT.restore_brain(project_id, revision)
    except ProjectContextError as exc:
        raise context_http_error(exc)


@app.delete("/projects/{project_id}/brain")
def delete_project_brain(project_id: str, user_id: str = Depends(get_current_user)):
    get_project(project_id, user_id)
    return PROJECT_CONTEXT.soft_delete_brain(project_id)


@app.get("/projects/{project_id}/notepad")
def get_project_notepad(project_id: str, user_id: str = Depends(get_current_user)):
    """Return the project-scoped plain-text notepad."""
    project = get_project(project_id, user_id)
    content = str(project.get("notepad") or "")
    return {
        "project_id": project_id,
        "content": content,
        "character_count": len(content),
        "updated_at": project.get("notepad_updated_at"),
    }


@app.put("/projects/{project_id}/notepad")
def update_project_notepad(
    project_id: str,
    req: NotepadUpdate,
    user_id: str = Depends(get_current_user),
):
    """Autosave project-scoped plain text without creating a second document model."""
    project = get_project(project_id, user_id)
    project["notepad"] = req.content
    project["notepad_updated_at"] = datetime.now().isoformat()
    project["updated_at"] = project["notepad_updated_at"]
    save_projects(PROJECTS)
    return {
        "project_id": project_id,
        "content": project["notepad"],
        "character_count": len(project["notepad"]),
        "updated_at": project["notepad_updated_at"],
    }

@app.delete("/projects/{project_id}")
def delete_project(project_id: str, user_id: str = Depends(get_current_user)):
    """Delete a project and detach it from conversations."""
    _ = get_project(project_id, user_id)
    PROJECTS.pop(project_id, None)
    # Detach project from existing conversations
    for cid, convo in CONVERSATIONS.items():
        if convo.get("project_id") == project_id and convo.get("user_id", "default") == user_id:
            convo["project_id"] = None
            convo.setdefault("context_events", []).append(
                {
                    "type": "project_detached",
                    "project_id": project_id,
                    "timestamp": datetime.now().isoformat(),
                    "reason": "project_deleted",
                    "applies_to": "future_messages_only",
                }
            )
    PROJECT_CONTEXT.delete_project(project_id)
    save_projects(PROJECTS)
    save_conversations(CONVERSATIONS)
    return {"status": "deleted", "project_id": project_id}

@app.get("/tools")
def list_available_tools(_auth=Depends(require_api_key)):
    """List available tools for the LLM to use."""
    if not TOOLS_ENABLED:
        raise HTTPException(403, "Tools are disabled")
    return {
        "tools": TOOL_REGISTRY.public_catalog(),
        "instructions": (
            "The executor sends these JSON schemas to Ollama on every model step. "
            "Tools marked approval_required pause before execution unless approved "
            "for the current run."
        ),
    }

@app.post("/tools/execute")
async def execute_tool_endpoint(tool_req: ToolRequest, _auth=Depends(require_api_key)):
    """Execute a tool and return results."""
    if not TOOLS_ENABLED:
        raise HTTPException(403, "Tools are disabled")
    result = await execute_tool(tool_req)
    return result.model_dump()


@app.post("/tools/agent/run")
async def run_agent_endpoint(req: AgentRunRequest, _auth=Depends(require_api_key)):
    """Run the bounded Ollama executor loop and return its complete transcript."""
    if not TOOLS_ENABLED:
        raise HTTPException(403, "Tools are disabled")
    node = get_node_by_id(req.node_id)
    inventory = MODEL_INVENTORY.get(node.id)
    if inventory is None:
        raise HTTPException(409, f"Model inventory for node '{node.id}' has not been loaded")
    if req.model not in inventory:
        raise HTTPException(400, f"Model '{req.model}' is not available on node '{node.id}'")

    unknown_approvals = sorted(set(req.approved_tools) - set(TOOL_REGISTRY.public_catalog()))
    if unknown_approvals:
        raise HTTPException(
            400,
            f"Cannot approve unregistered tool(s): {', '.join(unknown_approvals)}",
        )

    async def invoke_model(messages: List[Dict], schemas: List[Dict]):
        payload = {
            "model": req.model,
            "messages": messages,
            "tools": schemas,
            "tool_choice": "auto",
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{node.url}/v1/chat/completions",
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    outcome = await run_executor_loop(
        req.messages,
        invoke_model,
        registry=TOOL_REGISTRY,
        step_limit=req.step_limit,
        error_budget=req.error_budget,
        approved_tools=set(req.approved_tools),
        pending_store=PENDING_CALL_STORE,
    )
    return outcome.to_dict()


@app.post("/tools/agent/resume")
async def resume_agent_endpoint(
    req: AgentResumeRequest,
    _auth=Depends(require_api_key),
):
    """Apply one exact pending decision and continue the bounded loop."""
    if not TOOLS_ENABLED:
        raise HTTPException(403, "Tools are disabled")
    outcome = await resume_executor_loop(
        run_id=req.run_id,
        call_id=req.call_id,
        digest=req.digest,
        decision=req.decision,
        pending_store=PENDING_CALL_STORE,
    )
    return outcome.to_dict()

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
    if not COST_LOG.exists():
        return {"total_cost": 0, "by_model": {}, "by_conversation": {}}

    total = 0.0
    by_model: Dict[str, float] = {}
    by_convo: Dict[str, float] = {}

    with open(COST_LOG) as f:
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
        "context_events": convo.get("context_events", []),
        "system_prompt": convo.get("system_prompt"),
        "session_override": normalize_session_instructions(
            convo,
            get_project(convo.get("project_id"), user_id)
            if convo.get("project_id")
            else {},
        )[0],
    }


@app.get("/conversations/{conversation_id}/instructions")
def get_conversation_instructions(
    conversation_id: str,
    user_id: str = Depends(get_current_user),
):
    assert_convo_owner(conversation_id, user_id)
    conversation = CONVERSATIONS[conversation_id]
    project_id = conversation.get("project_id")
    project = get_project(project_id, user_id) if project_id else {}
    return {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "project_name": project.get("name"),
        **get_instruction_layers(conversation, project),
    }


@app.put("/conversations/{conversation_id}/instructions")
def update_conversation_instructions(
    conversation_id: str,
    req: InstructionUpdate,
    user_id: str = Depends(get_current_user),
):
    """Save visible instruction layers and apply them to the next message."""
    assert_convo_owner(conversation_id, user_id)
    conversation = CONVERSATIONS[conversation_id]
    project_id = conversation.get("project_id")
    project = get_project(project_id, user_id) if project_id else {}
    updates = req.model_dump(exclude_unset=True)

    if "global_default" in updates:
        global_default = str(updates["global_default"] or "")
        if not global_default.strip():
            raise HTTPException(400, "Global default instructions cannot be empty")
        SETTINGS["global_system_prompt"] = global_default
        save_settings(SETTINGS)

    if "project_instructions" in updates:
        if not project_id:
            raise HTTPException(400, "This conversation is not attached to a project")
        project_instructions = str(updates["project_instructions"] or "")
        try:
            profile = PROJECT_CONTEXT.update_profile(
                project_id,
                instructions=project_instructions,
            )
        except ProjectContextError as exc:
            raise context_http_error(exc)
        project["system_prompt"] = profile["instructions"]
        project["context_budget_tokens"] = profile["context_budget_tokens"]
        project["updated_at"] = datetime.now().isoformat()
        save_projects(PROJECTS)

    if "session_override" in updates:
        conversation["session_override"] = str(updates["session_override"] or "")
        conversation["instruction_mode"] = "layered"

    layers = get_instruction_layers(conversation, project)
    conversation["system_prompt"] = layers["effective"]
    conversation["updated_at"] = datetime.now().isoformat()
    save_conversations(CONVERSATIONS)
    return {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "project_name": project.get("name"),
        **layers,
    }


@app.delete("/conversations/{conversation_id}/instructions/session")
def reset_session_instructions(
    conversation_id: str,
    user_id: str = Depends(get_current_user),
):
    """Revert the session to its inherited global and project instructions."""
    assert_convo_owner(conversation_id, user_id)
    conversation = CONVERSATIONS[conversation_id]
    project_id = conversation.get("project_id")
    project = get_project(project_id, user_id) if project_id else {}
    conversation["session_override"] = ""
    conversation["instruction_mode"] = "layered"
    layers = get_instruction_layers(conversation, project)
    conversation["system_prompt"] = layers["effective"]
    conversation["updated_at"] = datetime.now().isoformat()
    save_conversations(CONVERSATIONS)
    return {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "project_name": project.get("name"),
        **layers,
    }

@app.post("/conversations/from_template")
def create_from_template(req: TemplateConversationRequest, user_id: str = Depends(get_current_user)):
    """Create a conversation from a predefined template."""
    template_name = req.template_name
    project_id = req.project_id
    if template_name not in TEMPLATES:
        raise HTTPException(404, "Template not found")
    template = TEMPLATES[template_name]
    project_cfg = get_project(project_id, user_id) if project_id else {}
    session_override = template.get("session_override", "")
    provisional_conversation = {
        "session_override": session_override,
        "instruction_mode": "layered",
        "messages": [],
    }
    system_prompt = resolve_conversation_system_prompt(
        provisional_conversation,
        project_cfg,
    )
    # Templates carry no model preference. The only preferred model is the
    # project's own setting, and the actual send still uses the visible
    # inventory-backed selection.
    preferred_model = project_cfg.get("preferred_model")
    cid = f"convo_{uuid.uuid4().hex}"
    CONVERSATIONS[cid] = {
        "title": template.get("title", DEFAULT_CONVO_TITLE),
        "messages": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "template": template_name,
        "user_id": user_id,
        "project_id": project_id,
        "system_prompt": system_prompt,
        "session_override": session_override,
        "instruction_mode": "layered",
    }
    save_conversations(CONVERSATIONS)
    return {
        "conversation_id": cid,
        "template": template_name,
        "preferred_model": preferred_model,
        "project_id": project_id,
        "system_prompt": system_prompt,
        "session_override": session_override,
    }

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

def set_conversation_project(
    conversation_id: str,
    conversation: dict,
    project_id: Optional[str],
    user_id: str,
    *,
    event_type: str = "project_attachment_changed",
) -> dict:
    old_project_id = conversation.get("project_id")
    old_project = get_project(old_project_id, user_id) if old_project_id else {}
    session_override, mode = normalize_session_instructions(conversation, old_project)
    project = get_project(project_id, user_id) if project_id else {}
    conversation["project_id"] = project_id
    conversation["session_override"] = session_override
    conversation["instruction_mode"] = mode
    conversation["system_prompt"] = resolve_conversation_system_prompt(conversation, project)
    conversation["updated_at"] = datetime.now().isoformat()
    if old_project_id != project_id or event_type == "project_resynced":
        conversation.setdefault("context_events", []).append(
            {
                "type": event_type,
                "from_project_id": old_project_id,
                "project_id": project_id,
                "timestamp": conversation["updated_at"],
                "applies_to": "future_messages_only",
            }
        )
    save_conversations(CONVERSATIONS)
    return {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "project_name": project.get("name"),
        "system_prompt": conversation["system_prompt"],
        "context_events": conversation.get("context_events", []),
    }


@app.put("/conversations/{conversation_id}/project")
def update_conversation_project(
    conversation_id: str,
    req: ProjectAttachmentUpdate,
    user_id: str = Depends(get_current_user),
):
    """Explicitly attach or detach future chat turns without rewriting history."""
    assert_convo_owner(conversation_id, user_id)
    return set_conversation_project(
        conversation_id,
        CONVERSATIONS[conversation_id],
        req.project_id,
        user_id,
    )


@app.post("/conversations/{conversation_id}/resync_project")
def resync_conversation_project(conversation_id: str, req: ResyncRequest, user_id: str = Depends(get_current_user)):
    """Re-apply project instructions to a conversation and persist them."""
    assert_convo_owner(conversation_id, user_id)
    convo = CONVERSATIONS[conversation_id]

    project_id = req.project_id or convo.get("project_id")
    if not project_id:
        raise HTTPException(400, "No project linked to conversation")

    result = set_conversation_project(
        conversation_id,
        convo,
        project_id,
        user_id,
        event_type="project_resynced",
    )

    return {
        "status": "updated",
        "conversation_id": conversation_id,
        "project_id": project_id,
        "system_prompt": result["system_prompt"],
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
    user_text = req.prompt or ""
    
    # Validate: require either text or images
    if not user_text.strip() and not req.images:
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
        attached_project_id = existing_convo.get("project_id")
        if req.project_id is not None and req.project_id != attached_project_id:
            raise HTTPException(
                409,
                "Project changes require the explicit conversation project endpoint",
            )
        project_id = attached_project_id
    else:
        project_id = req.project_id
    project_cfg = get_project(project_id, user_id) if project_id else {}

    if not req.node_id or not req.model:
        raise HTTPException(400, "node_id and model are required from the loaded inventory")
    node = get_node_by_id(req.node_id)
    inventory = MODEL_INVENTORY.get(node.id)
    if inventory is None:
        raise HTTPException(409, f"Model inventory for node '{node.id}' has not been loaded")
    if req.model not in inventory:
        raise HTTPException(400, f"Model '{req.model}' is not available on node '{node.id}'")
    preferred_model = req.model

    # Get conversation history and preserve raw indexes before pruning.
    raw_history = get_history(req.conversation_id, user_id=user_id, project_id=project_id)
    is_first_exchange = not any(
        message.get("role") in {"user", "assistant"} for message in raw_history
    )
    user_msg_idx = len(raw_history)
    raw_history.append({"role": "user", "content": user_text if user_text else "[image]"})
    history = prepare_history_for_prompt(raw_history)

    conversation = CONVERSATIONS[req.conversation_id]
    system_prompt = resolve_conversation_system_prompt(conversation, project_cfg)
    conversation["system_prompt"] = system_prompt
    # Build messages copy so we can adjust content shape for vision models without
    # mutating persisted history.
    try:
        messages_for_node, context_budget = build_project_messages_for_node(
            project_id=project_id,
            project=project_cfg,
            model_id=preferred_model,
            output_reserve=req.max_tokens or 2048,
            query=user_text,
            system_prompt=system_prompt,
            history=history,
        )
    except ProjectContextError as exc:
        raise context_http_error(exc)
    conversation["last_context_budget"] = context_budget

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

    # Call the selected Ollama node through its OpenAI-compatible endpoint.
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

    # Save assistant response to history using persisted raw-history indexes.
    assistant_msg_idx = len(raw_history)
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
        with open(COST_LOG, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass

    # Store embeddings
    if user_text:
        store_message_embedding(req.conversation_id, user_msg_idx, "user", user_text)
    store_message_embedding(req.conversation_id, assistant_msg_idx, "assistant", assistant_msg)

    # Update metadata and title
    convo = CONVERSATIONS[req.conversation_id]
    convo["updated_at"] = datetime.now().isoformat()
    title_text = user_text.strip()
    if title_text and is_first_exchange and convo.get("title") == DEFAULT_CONVO_TITLE:
        convo["title"] = title_text[:30] + ("..." if len(title_text) > 30 else "")
    capture_project_artifact(
        project_id,
        req.conversation_id,
        assistant_msg_idx,
        assistant_msg,
    )

    save_conversations(CONVERSATIONS)

    # Log performance
    try:
        actual_tokens = estimate_tokens(assistant_msg) + (estimate_tokens(user_text) if user_text else 0)
        cost = (actual_tokens / 1000) * model_meta.get("cost_per_1k", 0)
        latency_val = locals().get("latency_ms", None)
        comp_score = complexity_score(user_text, len(history))
        log_performance(user_id, req.conversation_id, preferred_model, actual_tokens, cost, latency_val, comp_score)
    except Exception:
        pass

    return ChatResponse(
        response=assistant_msg,
        node=node.name,
        conversation_id=req.conversation_id,
        message_count=len(raw_history),
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
    user_text = req.prompt or ""
    
    # Validate: require either text or images
    if not user_text.strip() and not req.images:
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
        attached_project_id = existing_convo.get("project_id")
        if req.project_id is not None and req.project_id != attached_project_id:
            raise HTTPException(
                409,
                "Project changes require the explicit conversation project endpoint",
            )
        project_id = attached_project_id
    else:
        project_id = req.project_id
    project_cfg = get_project(project_id, user_id) if project_id else {}

    if not req.node_id or not req.model:
        raise HTTPException(400, "node_id and model are required from the loaded inventory")
    node = get_node_by_id(req.node_id)
    inventory = MODEL_INVENTORY.get(node.id)
    if inventory is None:
        raise HTTPException(409, f"Model inventory for node '{node.id}' has not been loaded")
    if req.model not in inventory:
        raise HTTPException(400, f"Model '{req.model}' is not available on node '{node.id}'")
    preferred_model = req.model

    # Get conversation history and preserve raw indexes before pruning.
    raw_history = get_history(req.conversation_id, user_id=user_id, project_id=project_id)
    is_first_exchange = not any(
        message.get("role") in {"user", "assistant"} for message in raw_history
    )
    user_msg_idx = len(raw_history)
    raw_history.append({"role": "user", "content": user_text if user_text else "[image]"})
    history = prepare_history_for_prompt(raw_history)

    conversation = CONVERSATIONS[req.conversation_id]
    system_prompt = resolve_conversation_system_prompt(conversation, project_cfg)
    conversation["system_prompt"] = system_prompt
    try:
        messages_for_node, context_budget = build_project_messages_for_node(
            project_id=project_id,
            project=project_cfg,
            model_id=preferred_model,
            output_reserve=req.max_tokens or 2048,
            query=user_text,
            system_prompt=system_prompt,
            history=history,
        )
    except ProjectContextError as exc:
        raise context_http_error(exc)
    conversation["last_context_budget"] = context_budget

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
                    if not resp.is_success:
                        body = (await resp.aread()).decode("utf-8", errors="replace")[:200]
                        yield f"data: {json.dumps({'error': f'Node error {resp.status_code}: {body}', 'done': True})}\n\n"
                        had_error = True
                    else:
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
            msg = f"Node error {e.response.status_code}"
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
                assistant_msg_idx = len(convo_history)
                convo_history.append({"role": "assistant", "content": full_response})
                if user_text:
                    store_message_embedding(req.conversation_id, user_msg_idx, "user", user_text)
                store_message_embedding(req.conversation_id, assistant_msg_idx, "assistant", full_response)

                convo = CONVERSATIONS[req.conversation_id]
                convo["updated_at"] = datetime.now().isoformat()
                title_text = user_text.strip()
                if title_text and is_first_exchange and convo.get("title") == DEFAULT_CONVO_TITLE:
                    convo["title"] = title_text[:30] + ("..." if len(title_text) > 30 else "")
                capture_project_artifact(
                    project_id,
                    req.conversation_id,
                    assistant_msg_idx,
                    full_response,
                )

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
                    with open(COST_LOG, "a") as f:
                        f.write(json.dumps(log_entry) + "\n")
                except Exception as e:
                    record_error("cost_log", str(e))

                try:
                    actual_tokens = estimate_tokens(full_response) + (estimate_tokens(user_text) if user_text else 0)
                    cost = (actual_tokens / 1000) * model_meta.get("cost_per_1k", 0)
                    latency_ms = (time.time() - start_time) * 1000
                    comp_score = complexity_score(user_text, len(history))
                    log_performance(user_id, req.conversation_id, preferred_model, actual_tokens, cost, latency_ms, comp_score)
                except Exception as e:
                    record_error("perf_log", str(e))
            except Exception as e:
                record_error("stream_finalize", str(e))

        # Always send terminal event so client doesn’t see incomplete chunked encoding
        yield f"data: {json.dumps({'token': '', 'done': True, 'message_count': len(raw_history)})}\n\n"
    
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
        filename = re.sub(r"[^A-Za-z0-9._-]+", "-", convo.get("title", "conversation")).strip("-") or "conversation"
        return Response(
            content=md,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{filename}.md"'},
        )
    raise HTTPException(400, "Unsupported format")

# ============================================================
# STATIC FILES
# ============================================================

# Serve only the explicit browser bundle. Source, Git metadata, and data stay private.
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

# ============================================================
# STARTUP/SHUTDOWN HOOKS
# ============================================================

@app.on_event("startup")
async def startup_event():
    """Load conversations on startup."""
    print(f"✅ DaveLLM Router v{PRODUCT_VERSION} started")
    print(f"📁 Loaded {len(CONVERSATIONS)} conversations from disk")
    print(f"🖥️  Active nodes: {len(NODE_CONFIGS)}")
    BACKGROUND_TASKS.extend(
        [
            asyncio.create_task(background_summarizer()),
            asyncio.create_task(brain_compaction_worker()),
        ]
    )

@app.on_event("shutdown")
async def shutdown_event():
    """Save conversations on shutdown."""
    for task in BACKGROUND_TASKS:
        task.cancel()
    if BACKGROUND_TASKS:
        await asyncio.gather(*BACKGROUND_TASKS, return_exceptions=True)
        BACKGROUND_TASKS.clear()
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
