"""Node capability profiles (DL-ROUTE-01) and the prompt-size check (DL-ROUTE-02).

A profile says what a node is good at, so the router can say up front when a prompt is
going to a node that reads it slowly, both before sending (DL-ROUTE-05) and while the reply
waits (DL-ROUTE-02). It lives in the node's ``DAVE_NODES`` entry as an
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

A malformed profile is dropped with a warning and the node stays registered.

``NodeActivity`` (DL-ROUTE-04) counts the requests the router has in flight on each node.
Ollama nodes run one request at a time, so a count above zero means a new reply waits
behind the others; the UI says so instead of looking stuck. Only this router's requests
are counted. It also remembers the prompt key (model and conversation) each node last
started: Ollama keeps that prompt cached while the model stays loaded, so a follow-up in the
same conversation only has to read its new message.

The module reads no environment and never imports ``app``.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Literal, Mapping, Optional, Sequence

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


def prompt_size_estimate(profile: Optional[NodeProfile], model_id: str, prompt_tokens: int) -> dict:
    """The estimate, the node's limit for the model (``None`` without one) and whether it is over.

    The composer's pre-send check (DL-ROUTE-05) shows all three; the stream's waiting status
    (DL-ROUTE-02) reports only prompts over the limit. Both use this one comparison.
    """
    limit = profile.limit_for(model_id) if profile is not None else None
    return {
        "prompt_tokens": prompt_tokens,
        "prompt_token_limit": limit,
        "over_limit": limit is not None and prompt_tokens > limit,
    }


def prompt_size_check(profile: Optional[NodeProfile], model_id: str, prompt_tokens: int) -> Optional[dict]:
    """Fields for the stream's waiting status when the prompt is over the limit; ``None`` otherwise."""
    estimate = prompt_size_estimate(profile, model_id, prompt_tokens)
    if not estimate["over_limit"]:
        return None
    return {"prompt_tokens": prompt_tokens, "prompt_token_limit": estimate["prompt_token_limit"]}


def reads_only_new_message(prefix_cached: bool, model_loaded: Optional[bool], others_in_flight: int) -> bool:
    """Whether the node reads only the new message: Ollama still holds this chat's prompt prefix.

    That needs the conversation the node last served, the model confirmed in memory, and nothing
    else running on the node in between.
    """
    return prefix_cached and model_loaded is True and not others_in_flight


class NodeActivity:
    """Requests in flight per node; safe from the threadpool and the event loop alike."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}
        self._last_prompt: Dict[str, Optional[str]] = {}

    def in_flight(self, node_key: str) -> int:
        with self._lock:
            return self._counts.get(node_key, 0)

    def last_prompt(self, node_key: str) -> Optional[str]:
        with self._lock:
            return self._last_prompt.get(node_key)

    def start(self, node_key: str, prompt_key: Optional[str] = None) -> int:
        """Count one more request; returns how many others were already in flight.

        The count is not a queue position: requests reach the node in whatever order their
        own setup finishes, so it only says the node is busy.

        ``prompt_key`` names the prompt this request leaves in the node's cache; ``None`` (a
        summary, a tool run, a project chat) means nothing reusable, which clears the last key.
        """
        with self._lock:
            ahead = self._counts.get(node_key, 0)
            self._counts[node_key] = ahead + 1
            self._last_prompt[node_key] = prompt_key
            return ahead

    def finish(self, node_key: str) -> None:
        with self._lock:
            left = self._counts.get(node_key, 0) - 1
            if left > 0:
                self._counts[node_key] = left
            else:
                self._counts.pop(node_key, None)

    @contextmanager
    def track(self, node_key: str, prompt_key: Optional[str] = None) -> Iterator[int]:
        ahead = self.start(node_key, prompt_key)
        try:
            yield ahead
        finally:
            self.finish(node_key)
