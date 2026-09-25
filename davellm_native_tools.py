"""DaveLLM native read tools: project.notepad.read, project.brain.read, project.artifacts,
chat.search, and cluster.status.

Scope always comes from the run, never from tool arguments. The run binding
names the user, the project, and the BRAIN revision captured when the run
started; no schema has a user, project, or owner field. Project tools refuse
runs without a project, ``chat.search`` refuses calls outside a run, and every
project read repeats the ownership check that DaveLLM's HTTP routes use.

This module owns only the tool logic. DaveLLM supplies its existing services
through ``NativeServices``: project lookup with the route ownership check, the
BRAIN revision store, artifacts, the conversation search behind ``/search``,
and node health. Results never include database identifiers other than the
artifact and conversation identifiers the UI already shows, storage paths,
node addresses, or error text from lower layers, and every result is bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from davellm_files import OUTPUT_BUDGET_BYTES, _option, encode_result


NOTEPAD_MAX_CHARS = 20_000
BRAIN_MAX_CHARS = 30_000  # across the pinned, active, and recent sections
ARTIFACT_MAX_CHARS = 30_000
ARTIFACT_ID_MAX_CHARS = 100
ARTIFACT_DEFAULT_ENTRIES = 20
ARTIFACT_MAX_ENTRIES = 50
SEARCH_DEFAULT_RESULTS = 5
SEARCH_MAX_RESULTS = 10
SEARCH_MAX_QUERY_CHARS = 200
SNIPPET_CHARS = 300
TITLE_CHARS = 200
NODE_MAX_MODELS = 50
CHAT_ROLES = frozenset({"user", "assistant"})

RUN_REQUIRED = "This tool requires a DaveLLM run"
PROJECT_RUN_REQUIRED = "This tool requires a project-scoped run"
PROJECT_UNAVAILABLE = "The project for this run is not available"
BRAIN_UNAVAILABLE = "The BRAIN revision captured for this run is not available"
ARTIFACT_NOT_FOUND = "Artifact not found"
EMPTY_QUERY = "Query must contain text"
NATIVE_TOOL_FAILED = "Native tool failed"
_BUDGET = OUTPUT_BUDGET_BYTES - 512


class NativeToolError(Exception):
    """A fixed, model-safe refusal."""


@dataclass(frozen=True)
class RunScope:
    user_id: str
    project_id: str | None
    brain_revision: int | None
    brain_digest: str | None


@dataclass(frozen=True)
class NativeServices:
    """DaveLLM's existing services, as the native tools need them.

    ``project`` must apply the HTTP routes' ownership check and raise
    ``NativeToolError`` when the user may not read the project; ``brain_revision``
    and ``artifact`` raise ``NativeToolError`` when nothing matches.
    """

    scope: Callable[[], RunScope | None]
    project: Callable[[str, str], Mapping[str, Any]]
    brain_revision: Callable[[str, int], Mapping[str, Any]]
    brain_digest: Callable[[str, Mapping[str, Any]], str]
    artifacts: Callable[[str], Sequence[Mapping[str, Any]]]
    artifact: Callable[[str, str], Mapping[str, Any]]
    search: Callable[[str, str, int], Sequence[Mapping[str, Any]]]
    nodes: Callable[[], Sequence[Mapping[str, Any]]]


def _size(payload: Mapping[str, Any]) -> int:
    return len(encode_result(payload).encode("utf-8"))


def _fit(payload: dict[str, Any], keys: Sequence[str], limit: int) -> bool:
    """Shorten the text fields ``keys`` so they hold ``limit`` characters together and
    the payload fits the result budget. Later fields give way first."""
    cut = False
    remaining = limit
    for key in keys:
        text = payload[key]
        if len(text) > remaining:
            payload[key], cut = text[:remaining], True
        remaining -= len(payload[key])
    while _size(payload) > _BUDGET:
        key = next(key for key in reversed(keys) if payload[key])
        payload[key] = payload[key][:len(payload[key]) * 9 // 10]
        cut = True
    return cut


def _scope(services: NativeServices) -> RunScope:
    scope = services.scope()
    if scope is None:
        raise NativeToolError(RUN_REQUIRED)
    return scope


def _project(services: NativeServices) -> tuple[RunScope, Mapping[str, Any]]:
    """The run's own project, after the routes' ownership check."""
    scope = services.scope()
    if scope is None or not scope.project_id:
        raise NativeToolError(PROJECT_RUN_REQUIRED)
    return scope, services.project(scope.project_id, scope.user_id)


def _name(project: Mapping[str, Any]) -> str:
    return str(project.get("name") or "")[:TITLE_CHARS]


def notepad_read(arguments: Mapping[str, Any], services: NativeServices) -> dict[str, Any]:
    """project.notepad.read: the run's project notepad."""
    _, project = _project(services)
    text = str(project.get("notepad") or "")
    payload: dict[str, Any] = {
        "project": _name(project), "notepad": text, "character_count": len(text),
        "updated_at": project.get("notepad_updated_at"), "truncated": False,
    }
    payload["truncated"] = _fit(payload, ("notepad",), NOTEPAD_MAX_CHARS)
    return payload


def brain_read(arguments: Mapping[str, Any], services: NativeServices) -> dict[str, Any]:
    """project.brain.read: the BRAIN revision captured when the run started, never a later one."""
    scope, project = _project(services)
    if scope.brain_revision is None or not scope.brain_digest:
        raise NativeToolError(BRAIN_UNAVAILABLE)
    snapshot = services.brain_revision(str(scope.project_id), scope.brain_revision)
    if services.brain_digest(str(scope.project_id), snapshot) != scope.brain_digest:
        raise NativeToolError(BRAIN_UNAVAILABLE)
    sections = {name: str(snapshot.get(f"{name}_text") or "") for name in ("pinned", "active", "recent")}
    payload: dict[str, Any] = {
        "project": _name(project), "revision": scope.brain_revision,
        "deleted": bool(snapshot.get("deleted_at")), **sections,
        "character_count": {name: len(text) for name, text in sections.items()}, "truncated": False,
    }
    payload["truncated"] = _fit(payload, ("pinned", "active", "recent"), BRAIN_MAX_CHARS)
    return payload


def _artifact_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact": str(record.get("artifact_id") or ""),
        "title": str(record.get("title") or "")[:TITLE_CHARS],
        "kind": str(record.get("kind") or ""),
        "pinned": bool(record.get("pinned")),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def artifacts(arguments: Mapping[str, Any], services: NativeServices) -> dict[str, Any]:
    """project.artifacts: list the run's project artifacts, or read one by ``artifact``."""
    scope, project = _project(services)
    project_id = str(scope.project_id)
    wanted = _option(arguments, "artifact", None)
    if wanted is not None:
        record = services.artifact(project_id, str(wanted)[:ARTIFACT_ID_MAX_CHARS])
        text = str(record.get("body") or "")
        payload: dict[str, Any] = {"project": _name(project), **_artifact_entry(record), "text": text,
                                   "character_count": len(text), "truncated": False}
        payload["truncated"] = _fit(payload, ("text",), ARTIFACT_MAX_CHARS)
        return payload
    limit = _option(arguments, "max_entries", ARTIFACT_DEFAULT_ENTRIES)
    records = list(services.artifacts(project_id))
    entries = [_artifact_entry(record) for record in records[:limit]]
    listing: dict[str, Any] = {"project": _name(project), "artifacts": entries, "count": len(entries),
                               "total": len(records), "truncated": len(records) > len(entries)}
    while _size(listing) > _BUDGET:
        entries.pop()
        listing.update(count=len(entries), truncated=True)
    return listing


def _snippet(content: str, query: str) -> str:
    """At most SNIPPET_CHARS characters, centred on the first match of the query."""
    flat = " ".join(content.split())
    at = flat.casefold().find(query.casefold())
    start = 0 if at < 0 else max(0, min(at - SNIPPET_CHARS // 3, len(flat) - SNIPPET_CHARS))
    return flat[start:start + SNIPPET_CHARS]


def chat_search(arguments: Mapping[str, Any], services: NativeServices) -> dict[str, Any]:
    """chat.search: the current user's own conversations only; the query never changes scope."""
    scope = _scope(services)
    query = " ".join(str(arguments["query"]).split())[:SEARCH_MAX_QUERY_CHARS]
    if not query:
        raise NativeToolError(EMPTY_QUERY)
    limit = _option(arguments, "max_results", SEARCH_DEFAULT_RESULTS)
    found = services.search(query, scope.user_id, limit)
    results = [{
        "conversation": str(item.get("conversation_id") or ""),
        "title": str(item.get("title") or "")[:TITLE_CHARS],
        "role": str(item.get("role") or ""),
        "snippet": _snippet(str(item.get("content") or ""), query),
    } for item in list(found)[:limit] if item.get("role") in CHAT_ROLES]
    return {"query": query, "results": results, "count": len(results)}


def cluster_status(arguments: Mapping[str, Any], services: NativeServices) -> dict[str, Any]:
    """cluster.status: configured nodes only, without addresses, credentials, or error text."""
    nodes = []
    for node in services.nodes():
        models = node.get("models")
        nodes.append({
            "node": str(node.get("node_id") or ""),
            "name": str(node.get("name") or "")[:TITLE_CHARS],
            "reachable": bool(node.get("reachable")),
            "latency_ms": node.get("latency_ms") if node.get("reachable") else None,
            "models": None if models is None else [str(model)[:TITLE_CHARS] for model in sorted(models)][:NODE_MAX_MODELS],
            "models_truncated": models is not None and len(models) > NODE_MAX_MODELS,
        })
    return {"nodes": nodes, "count": len(nodes)}
