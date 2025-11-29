"""
DaveLLM Router – A minimal FastAPI application for routing prompts to llama.cpp nodes.

This module forwards chat requests to one of several nodes.  You can expand this router
with more sophisticated routing logic, metrics, streaming, and discovery as your project
grows.
"""

from __future__ import annotations

import logging
import os
from itertools import cycle
from typing import List, Optional
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field, validator

logger = logging.getLogger(__name__)

app = FastAPI(
    title="DaveLLM Router",
    description="Routes chat prompts to available nodes",
    version="0.1.0",
)


def _validate_url(url: str) -> str:
    # Parse and sanity-check the URL so that downstream requests do not
    # fail due to missing scheme/host pieces.  This validation also
    # normalizes away any trailing slash for consistency when we build
    # endpoint URLs later.
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid node URL: {url!r}")
    return url.rstrip("/")


class NodeConfig(BaseModel):
    name: str = Field(..., description="Human readable node identifier")
    url: str = Field(..., description="Base URL for the node's API")

    _normalize_url = validator("url", allow_reuse=True)(_validate_url)


DEFAULT_NODES: List[NodeConfig] = [
    NodeConfig(name="mac-test-node", url="http://127.0.0.1:9001"),
]


class ChatRequest(BaseModel):
    prompt: str = Field(..., description="User prompt to send to the model")
    model: Optional[str] = Field(None, description="Optional model override")
    max_tokens: Optional[int] = Field(256, ge=1, description="Maximum tokens to generate")


class ChatResponse(BaseModel):
    response: str
    node: str


def _parse_nodes_from_env(env_value: str) -> List[NodeConfig]:
    # Environment variable format examples:
    #   "http://127.0.0.1:9001"                          -> name inferred from host
    #   "gpu-node|http://10.0.0.5:9001, cpu|http://..." -> explicit names with pipe
    # Whitespace and empty entries are ignored to allow easy multiline definitions.
    nodes: List[NodeConfig] = []
    for raw_node in env_value.split(","):
        if not (clean := raw_node.strip()):
            continue

        if "|" in clean:
            name, url = clean.split("|", 1)
        else:
            url = clean
            parsed = urlparse(url)
            name = parsed.netloc or url

        try:
            nodes.append(NodeConfig(name=name.strip() or url, url=url.strip()))
        except ValueError as exc:
            logger.warning("Skipping invalid node definition %r: %s", clean, exc)
    return nodes


def _load_nodes() -> List[NodeConfig]:
    # Prefer user-supplied configuration; otherwise fall back to a sane
    # default so the service is runnable in development without extra
    # setup.  Invalid env entries are logged and replaced with defaults.
    raw_nodes = os.getenv("LLM_NODES")
    if not raw_nodes:
        return DEFAULT_NODES

    parsed_nodes = _parse_nodes_from_env(raw_nodes)
    if not parsed_nodes:
        logger.warning("No valid nodes found in LLM_NODES; falling back to defaults")
        return DEFAULT_NODES
    return parsed_nodes


NODE_CONFIGS: List[NodeConfig] = _load_nodes()
NODE_CYCLE = cycle(NODE_CONFIGS)


def _choose_node() -> NodeConfig:
    # Round-robin selection provides simple load distribution without
    # external state.  cycle() never raises StopIteration, but we guard to
    # surface a clear error if misconfigured.
    try:
        return next(NODE_CYCLE)
    except StopIteration:  # pragma: no cover - cycle should never exhaust
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No nodes configured")


@app.get("/health", tags=["Status"])
def health() -> dict:
    """Simple health check showing configured nodes."""
    return {"status": "ok", "nodes": [node.dict() for node in NODE_CONFIGS]}


@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(request: ChatRequest) -> ChatResponse:
    """
    Forward a chat prompt to one of the configured nodes.

    The router uses round-robin selection. In a production setup you might want more
    sophisticated routing such as least-loaded or latency-aware selection.
    """
    # Reject clearly invalid input early before attempting network calls.
    if not request.prompt.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Prompt must not be empty")

    if not NODE_CONFIGS:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No nodes configured")

    # Choose a node and build the target endpoint.  exclude_none keeps the
    # payload minimal while still honoring provided overrides.
    node = _choose_node()
    payload = request.dict(exclude_none=True)
    node_endpoint = f"{node.url}/generate"

    # HTTP call to the node is split into connection/transport errors and
    # application-level errors so clients get clearer responses.
    try:
        resp = requests.post(node_endpoint, json=payload, timeout=60)
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error contacting node {node.name} at {node.url}: {exc}",
        ) from exc

    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Node {node.name} responded with {resp.status_code}: {resp.text}",
        ) from exc

    # The node is expected to return a JSON object with a "response"
    # field; if not, we still surface whatever payload we received for
    # transparency, coerced to string for consistent typing.
    data = resp.json()
    answer = data.get("response") if isinstance(data, dict) else data
    return ChatResponse(response=str(answer), node=node.name)
