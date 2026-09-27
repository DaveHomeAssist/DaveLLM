"""Node capability profiles (DL-ROUTE-01) and the prompt-size check (DL-ROUTE-02).

A profile says what a node is good at, so the router can say up front when a prompt is
going to a node that reads it slowly. It lives in the node's ``DAVE_NODES`` entry as an
optional ``profile`` object::

    {"id": "dominic", "name": "Dominic", "url": "...",
     "profile": {"compute": "cpu", "prompt_token_limit": 1300}}
    {"id": "duncan", "name": "Duncan", "url": "...",
     "profile": {"compute": "gpu", "model_prompt_token_limits": {"gpt-oss:120b": 2500}}}

``prompt_token_limit`` is the prompt size (estimated tokens for everything sent: system
prompt, project context, history and the new message) that the node reads in reasonable
time; a ``model_prompt_token_limits`` entry overrides it for one model. The limits are
advisory. DaveLLM never reroutes a request, because the user picks the node and model;
a prompt over the limit still goes to that node, and the router tells the UI why the
reply will be slow.

A malformed profile is dropped with a warning and the node stays registered. The module
reads no environment and never imports ``app``.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError


class NodeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compute: Optional[Literal["gpu", "cpu"]] = None
    prompt_token_limit: Optional[PositiveInt] = None
    model_prompt_token_limits: Dict[str, PositiveInt] = Field(default_factory=dict)

    def limit_for(self, model_id: str) -> Optional[int]:
        """The prompt limit for ``model_id`` on this node; ``llama3`` and ``llama3:latest`` match."""
        limits = self.model_prompt_token_limits
        if model_id in limits:
            return limits[model_id]
        base, _, tag = model_id.partition(":")
        if tag == "latest" and base in limits:
            return limits[base]
        if not tag and f"{model_id}:latest" in limits:
            return limits[f"{model_id}:latest"]
        return self.prompt_token_limit


def parse_node_profiles(
    raw_nodes: Optional[str], *, warn: Callable[[str], None] = print,
) -> Dict[str, NodeProfile]:
    """Profiles by node id from the ``DAVE_NODES`` JSON; nodes without one are left out."""
    if not raw_nodes:
        return {}
    try:
        entries = json.loads(raw_nodes)
    except ValueError:
        return {}  # the node list itself is reported where it is parsed
    if not isinstance(entries, list):
        return {}
    profiles: Dict[str, NodeProfile] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("profile") is None:
            continue
        node_id = entry.get("id")
        if not isinstance(node_id, str):
            continue
        try:
            profiles[node_id] = NodeProfile.model_validate(entry["profile"])
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first.get("loc", ())) or "profile"
            warn(f"⚠️ Ignoring the profile for node '{node_id}': {where}: {first.get('msg', 'invalid')}")
    return profiles


def estimate_prompt_tokens(messages: Sequence[Mapping[str, Any]], estimate: Callable[[str], int]) -> int:
    """Estimated prompt tokens for the messages sent to the node (text only)."""
    return sum(estimate(str(message.get("content") or "")) for message in messages)


def prompt_size_check(profile: Optional[NodeProfile], model_id: str, prompt_tokens: int) -> Optional[dict]:
    """Fields for the stream's waiting status when the prompt is over the limit; ``None`` otherwise."""
    if profile is None:
        return None
    limit = profile.limit_for(model_id)
    if limit is None or prompt_tokens <= limit:
        return None
    return {"prompt_tokens": prompt_tokens, "prompt_token_limit": limit}
