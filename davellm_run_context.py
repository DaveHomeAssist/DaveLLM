"""DL-TOOLUX-01: what a tool run knows before its first step, and the nudges after a bad step.

The context names the date, the tool root's layout and the node ids so the model can look things
up instead of guessing them. It never carries node URLs, file contents or protected names: the
folder map comes from ``walk_tree``, which skips secrets, ignored folders and escaping symlinks.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from davellm_files import walk_tree

RUN_CONTEXT_MAX_ENTRIES = 120
RUN_CONTEXT_MAX_MAP_CHARS = 3_000
FILE_TOOL_PREFIXES = ("file.", "md.", "git.", "code.", "secret.", "agent.claim.")
NODE_TOOL_PREFIXES = ("cluster.", "ollama.", "node.", "model.", "route.")
WORKING_RULES = (
    "Look before you guess: list or search for a file, node or name you have not seen in this run.",
    "Make the tool calls the request needs without announcing a plan first.",
    "When you have what you need, answer the user directly and briefly, and say what you could not find.",
)
CONTINUE_NUDGE = (
    "You have the tool results above. Continue: make the next tool call the request still needs, "
    "or answer the user directly."
)
TEXT_CALL_NUDGE = (
    "Your last reply wrote a tool call as text, so it did not run. Make it as a structured tool call, "
    "or answer the user directly."
)
# Tool-call markup some models (qwen3-coder) leave in the reply text instead of returning a call.
TEXT_TOOL_CALL = re.compile(r"</?tool_call>|<function=[A-Za-z0-9_.\-]+>")


def folder_map(root: Path) -> str:
    """Top-level entries of ``root`` with the names inside each folder, root-relative and bounded."""
    walk = walk_tree(root, root, max_depth=2, max_entries=RUN_CONTEXT_MAX_ENTRIES)
    children: dict[str, list[str]] = {}
    for entry in walk.entries:
        if entry.depth == 2:
            parent, _, name = entry.display.rpartition("/")
            children.setdefault(parent, []).append(name + ("/" if entry.kind == "dir" else ""))
    lines = []
    for entry in walk.entries:
        if entry.depth != 1:
            continue
        if entry.kind == "dir":
            inside = children.get(entry.display, [])
            lines.append(f"{entry.display}/" + (f" ({', '.join(inside)})" if inside else ""))
        else:
            lines.append(entry.display)
    text = "\n".join(lines)
    if len(text) > RUN_CONTEXT_MAX_MAP_CHARS:
        text = text[:RUN_CONTEXT_MAX_MAP_CHARS].rsplit("\n", 1)[0]
        return text + "\n(more entries not shown)"
    if walk.truncated:
        text += "\n(more entries not shown)"
    return text


def _wants(selected: set[str], prefixes: Sequence[str]) -> bool:
    return not selected or any(name.startswith(prefixes) for name in selected)


def tool_run_context(
    *, now: datetime, roots: Sequence[Path], node_ids: Sequence[str], selected_tools: Iterable[str],
) -> str:
    """The system message a lifecycle run starts with. An empty selection means every tool."""
    selected = set(selected_tools)
    offset = now.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"
    parts = [
        "Tool run context from DaveLLM, not from the user.",
        f"Now: {now:%A %Y-%m-%d %H:%M} {now.tzname() or ''} ({zone}).".replace("  ", " "),
    ]
    if roots and _wants(selected, FILE_TOOL_PREFIXES):
        if len(roots) == 1:
            parts.append("Files: give paths relative to the tool root. Its entries, with what each folder holds:\n"
                         + (folder_map(roots[0]) or "(empty)"))
        else:
            parts.append("Files: several tool roots are configured, so give absolute paths inside one of: "
                         + ", ".join(str(root) for root in roots))
    if node_ids and _wants(selected, NODE_TOOL_PREFIXES):
        parts.append("Cluster node ids: " + ", ".join(node_ids) + ".")
    parts.append("Working rules:\n" + "\n".join(f"- {rule}" for rule in WORKING_RULES))
    return "\n\n".join(parts)


def with_run_context(messages: Sequence[Mapping[str, Any]], context: str) -> list[dict[str, Any]]:
    """Insert the context after the leading system messages, so the primary one stays first."""
    items = [dict(message) for message in messages]
    index = next((i for i, message in enumerate(items) if message.get("role") != "system"), len(items))
    return [*items[:index], {"role": "system", "content": context}, *items[index:]]


def needs_continue_nudge(messages: Sequence[Mapping[str, Any]], reply: Mapping[str, Any]) -> bool:
    """True when the step after tool results came back with neither text nor a tool call."""
    message = reply.get("message") if isinstance(reply.get("message"), Mapping) else {}
    content = message.get("content")
    return (bool(messages) and messages[-1].get("role") == "tool"
            and not message.get("tool_calls")
            and not (isinstance(content, str) and content.strip()))


def looks_like_text_tool_call(content: Any) -> bool:
    """True when reply text carries tool-call markup; such text never ran and is not an answer."""
    return isinstance(content, str) and TEXT_TOOL_CALL.search(content) is not None


def wrote_tool_call_as_text(reply: Mapping[str, Any]) -> bool:
    """True when the model put a tool call in its text and returned no structured call."""
    message = reply.get("message") if isinstance(reply.get("message"), Mapping) else {}
    return not message.get("tool_calls") and looks_like_text_tool_call(message.get("content"))
