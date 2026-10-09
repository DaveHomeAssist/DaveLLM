"""Bounded pure utilities and existing owner-scoped DaveLLM services."""
from __future__ import annotations

import ast
import json
import math
import operator
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from daveharness.schema import validate_json_schema
from davellm_files import display_path, read_admitted_bytes, decode_text, search_files, read_lines
from davellm_git import open_repository
from davellm_toolpack import ToolpackError, digest, _SENSITIVE

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow}
_UNITS = {"m": ("length", 1), "cm": ("length", .01), "mm": ("length", .001),
          "ft": ("length", .3048), "in": ("length", .0254), "s": ("time", 1),
          "min": ("time", 60), "h": ("time", 3600), "kg": ("mass", 1), "g": ("mass", .001),
          "lb": ("mass", .45359237)}


def calculator(expression):
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 64:
        raise ToolpackError("Expression exceeds the arithmetic budget")

    def value(node, depth=0):
        if depth > 12:
            raise ToolpackError("Expression exceeds the arithmetic budget")
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            result = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = value(node.operand, depth + 1) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = value(node.left, depth + 1), value(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ToolpackError("Exponent exceeds the arithmetic budget")
            result = _OPS[type(node.op)](left, right)
        else:
            raise ToolpackError("Only numeric arithmetic is allowed")
        if isinstance(result, complex) or not math.isfinite(result) or abs(result) > 1e18:
            raise ToolpackError("Result exceeds the arithmetic budget")
        return result
    return value(tree.body)


def aware(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ToolpackError("An offset-aware ISO timestamp is required")
    return result


def conversation(host, identifier):
    binding = host.binding()
    record = host.router["CONVERSATIONS"].get(identifier)
    if not record or record.get("user_id", "default") != binding.user_id:
        raise ToolpackError("Conversation is not available to this run owner")
    return record


def source_path(host, alias, requested=None):
    source = host.configured("sources", alias)
    if not isinstance(source, dict) or host.binding().user_id not in source.get("users", []):
        raise ToolpackError("Source is not available to this run owner")
    base = host.router["resolve_extended_tool_path"](source["path"])
    if requested is None:
        return base
    relative = Path(requested)
    if relative.is_absolute() or ".." in relative.parts:
        raise ToolpackError("Path is not allowed")
    admitted = host.router["resolve_extended_tool_path"](str(base / relative))
    if admitted != base and not admitted.is_relative_to(base):
        raise ToolpackError("Path is not allowed")
    return admitted


def source_text(host, alias, requested=None):
    data = read_admitted_bytes(source_path(host, alias, requested), 65536)
    text = decode_text(data)
    if text is None:
        raise ToolpackError("Source is not UTF-8 text")
    return text


def execute_local(name, args, host):
    if name in ("project.notepad.snapshot", "project.notepad.write", "memory.propose"):
        with host.native_lock:
            return _execute_local(name, args, host)
    return _execute_local(name, args, host)


def _execute_local(name, args, host):
    if name == "calc.eval":
        result = calculator(args["expression"])
        source, target = args["from_unit"], args["to_unit"]
        if source or target:
            if source not in _UNITS or target not in _UNITS or _UNITS[source][0] != _UNITS[target][0]:
                raise ToolpackError("Units must be a supported compatible pair")
            result *= _UNITS[source][1] / _UNITS[target][1]
        return {"value": result, "unit": target}
    if name == "time.convert":
        return {"timestamp": aware(args["timestamp"]).astimezone(ZoneInfo(args["zone"])).isoformat()}
    if name == "json.validate":
        document, schema = json.loads(args["document"]), json.loads(args["schema"])
        validate_json_schema(document, schema)
        return {"valid": True, "schema_dialect": "DaveHarness supported subset"}
    if name == "secret.scan":
        findings = [{"line": args["text"].count("\n", 0, m.start()) + 1, "kind": "credential_candidate"}
                    for m in _SENSITIVE.finditer(args["text"])][:50]
        return {"findings": findings, "clean": not findings, "note": "Heuristic, not proof of absence"}
    if name == "prompt.lint":
        findings = []
        for rule, pattern in (("PRMT-1", r"\[[A-Z][A-Z _]+\]|<insert[^>]*>|\{\{[^}]+\}\}"),
                              ("PRMT-2", r"(?im)^\s*(inputs?|output.schema|trigger|fill.in)\s*:"),
                              ("PRMT-5", r"(?i)choose one of|insert (?:your|the)|optional modes")):
            if re.search(pattern, args["text"]):
                findings.append({"rule": rule, "finding": "Possible prompt scaffolding"})
        return {"findings": findings, "note": "Deterministic hints, not semantic compliance proof"}
    if name == "dmx.patch_check":
        conflicts, invalid = [], []
        fixtures = args["fixtures"]
        for i, fixture in enumerate(fixtures):
            end = fixture["address"] + fixture["footprint"] - 1
            if end > 512:
                invalid.append(fixture["name"])
            for other in fixtures[:i]:
                if other["universe"] == fixture["universe"] and max(other["address"], fixture["address"]) <= min(other["address"] + other["footprint"] - 1, end):
                    conflicts.append([other["name"], fixture["name"]])
        return {"valid": not invalid and not conflicts, "out_of_range": invalid, "overlaps": conflicts[:50]}
    if name == "video.gap_find":
        takes = sorted([(aware(t["start"]), aware(t["end"]), t["name"]) for t in args["takes"]])
        if any(end <= start for start, end, _ in takes):
            raise ToolpackError("Each take must end after it starts")
        gaps, overlaps = [], []
        end = None
        for start, finish, title in takes:
            if end is not None:
                delta = (start - end).total_seconds()
                if delta:
                    (gaps if delta > 0 else overlaps).append({"before": title, "seconds": abs(delta)})
            end = max(end, finish) if end is not None else finish
        return {"gaps": gaps, "overlaps": overlaps}
    if name == "setlist.parse":
        songs = []
        lines = args["text"].splitlines()
        for line in lines[:50]:
            line = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line).strip()
            if not line:
                continue
            match = re.search(r"\s+(\d{1,3}):([0-5]\d)\s*$", line)
            songs.append({"title": line[:match.start()] if match else line,
                          "seconds": int(match[1]) * 60 + int(match[2]) if match else None})
        return {"songs": songs, "truncated": len(lines) > 50}
    if name in ("glossary.lookup", "midi.map.lookup", "av.inventory.lookup", "runner.select"):
        group = {"glossary.lookup": "glossary", "midi.map.lookup": "midi", "av.inventory.lookup": "av_inventory", "runner.select": "runners"}[name]
        values = host.config.get(group, {})
        wanted = args.get("term", args.get("control", args.get("query", args.get("capability", "")))).casefold()
        if name == "runner.select":
            owner = host.binding().user_id
            matches = []
            for key, record in list(values.items())[:200]:
                if not isinstance(record, dict) or owner not in record.get("users", []):
                    continue
                capabilities = [value for value in record.get("capabilities", [])
                                if isinstance(value, str) and 1 <= len(value) <= 80][:50]
                if any(wanted == value.casefold() for value in capabilities):
                    matches.append({"name": key, "capabilities": capabilities})
            limit = args.get("limit", 50)
            return {"matches": matches[:limit], "observed_reachability": "Unknown", "truncated": len(matches) > limit}
        matches = [{"name": key, "record": record} for key, record in list(values.items())[:200]
                   if wanted in key.casefold()]
        return {"matches": matches[:args.get("limit", 50)], "observed_reachability": "Unknown", "truncated": len(matches) > args.get("limit", 50)}
    if name in ("memory.recall", "claude.memory.read", "rules.lookup", "machine.access.lookup", "comms.log.read", "nextsteps.read"):
        path = source_path(host, args["source"], args.get("path"))
        if name == "memory.recall":
            return search_files({"path": str(path), "query": args["query"], "max_matches": args["limit"]},
                                resolve=host.router["resolve_extended_tool_path"], roots=host.router["TOOL_ROOTS"])
        if name == "comms.log.read":
            raw = read_admitted_bytes(path, 1048576)
            text = decode_text(raw)
            if text is None or text.count("# 📡 Recorded Agent Output") != 1:
                raise ToolpackError("Source is not a bounded communications page")
            section = text.split("# 📡 Recorded Agent Output", 1)[1].split("\n# ", 1)[0]
            entries = ["## " + entry for entry in re.split(r"(?m)^## ", section)[1:]]
            return {"entries": [entry[:4000] for entry in entries[:args["limit"]]], "digest": digest(text),
                    "truncated": len(entries) > args["limit"] or any(len(entry) > 4000 for entry in entries[:args["limit"]])}
        text = source_text(host, args["source"], args.get("path"))
        if name == "nextsteps.read":
            return {"record": json.loads(text), "digest": digest(text)}
        if name in ("rules.lookup", "machine.access.lookup"):
            wanted = args.get("rule", args.get("machine", "")).casefold()
            lines = text.splitlines()
            selected = [line for line in lines if wanted in line.casefold()][:30]
            return {"matches": selected, "digest": digest(text), "note": "Literal source matches; no inferred authority"}
        return {"text": text[:16000], "digest": digest(text), "truncated": len(text) > 16000}
    if name == "code.symbols":
        raw = read_admitted_bytes(host.router["resolve_extended_tool_path"](args["path"]), 65536)
        text = decode_text(raw)
        if text is None:
            raise ToolpackError("Source is not UTF-8 text")
        tree = ast.parse(text)
        return {"symbols": [{"name": n.name, "line": n.lineno, "kind": type(n).__name__}
                            for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))][:50]}
    if name in ("git.worktrees", "agent.claim.check", "git.write_preview"):
        resolve, roots = host.router["resolve_extended_tool_path"], host.router["TOOL_ROOTS"]
        repo = open_repository({"path": args["repository"]}, resolve, roots)
        if name == "git.write_preview":
            from davellm_git import check_file, DIFF_SAFETY
            if not args["files"]:
                raise ToolpackError("Preview requires named files")
            files = [check_file(value) for value in args["files"]]
            tracked = repo.git("ls-files", "--error-unmatch", "--", *files)
            if tracked.returncode or tracked.truncated:
                raise ToolpackError("Preview requires tracked named files")
            patch = repo.output("diff", *DIFF_SAFETY, "--", *files)
            if patch.truncated:
                raise ToolpackError("Diff exceeds the approval preview budget")
            text = patch.stdout.decode("utf-8", "replace")
            return {"diff": text, "digest": digest(text), "files": files, "head": repo.output("rev-parse", "HEAD").stdout.decode().strip()}
        claim = repo.top / ".agent-claim"
        present = claim.exists()
        if name == "agent.claim.check":
            return {"claimed": present, "age_seconds": max(0, __import__("time").time() - claim.stat().st_mtime) if present else None}
        raw = repo.output("worktree", "list", "--porcelain").stdout.decode("utf-8", "replace")
        entries = []
        for record in raw.split("\n\n")[:50]:
            lines = record.splitlines()
            if not lines or not lines[0].startswith("worktree "):
                continue
            target = resolve(lines[0][9:])
            entries.append({"path": display_path(target, roots), "claimed": (target / ".agent-claim").exists()})
        return {"worktrees": entries}
    binding = host.binding()
    if name == "project.notepad.snapshot":
        if not binding.project_id:
            raise ToolpackError("This tool requires a project-scoped run")
        project = host.router["get_project"](binding.project_id, binding.user_id)
        text = str(project.get("notepad") or "")
        if len(text) > 20000:
            raise ToolpackError("Notepad exceeds the bounded write snapshot budget")
        return {"text": text, "digest": digest(text)}
    if name in ("chat.read", "chat.export"):
        record = conversation(host, args["conversation"])
        messages = [m for m in record.get("messages", []) if m.get("role") in ("user", "assistant")]
        if name == "chat.read":
            start, limit = args["offset"], args["limit"]
            selected = [{"role": m["role"], "content": str(m.get("content", ""))[:4000]} for m in messages[start:start + limit]]
            return {"messages": selected, "next_offset": start + limit if len(messages) > start + limit else None,
                    "truncated": any(len(str(m.get("content", ""))) > 4000 for m in messages[start:start + limit])}
        text = "\n\n".join("## " + m["role"] + "\n\n" + str(m.get("content", ""))[:4000] for m in messages[:50])
        return {"markdown": text[:16000], "truncated": len(text) > 16000 or len(messages) > 50, "file_written": False}
    if name == "token.count":
        node = host.node(args["node"])
        profile = host.router["NODE_PROFILES"].get(node.id)
        estimate = host.router["estimate_tokens"](args["text"])
        limit = profile.prompt_token_limit if profile else None
        return {"estimate": estimate, "exact": False, "configured_limit": limit,
                "over_limit": estimate > limit if limit else None}
    if name == "vector.search":
        results = host.router["search_conversation_messages"](args["query"], binding.user_id, args["limit"],
                                                             roles=frozenset({"user", "assistant"}), existing_only=True, fallback_chars=1000)
        indexed = []
        alias = host.config.get("vector_database_source")
        data = read_admitted_bytes(source_path(host, alias), 1048576) if alias else None
        with sqlite3.connect(":memory:") as connection:
            if data:
                connection.deserialize(data)
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_documents'").fetchone()
            if exists:
                query = host.router["get_simple_embedding"](args["query"])
                for source, path, text in connection.execute("SELECT source,path,content FROM tool_documents WHERE owner=? LIMIT 500", (binding.user_id,)):
                    similarity = host.router["cosine_similarity"](query, host.router["get_simple_embedding"](str(text)[:16000]))
                    if similarity > .35:
                        indexed.append({"source": source, "path": path, "text": str(text)[:1000], "similarity": similarity})
        indexed.sort(key=lambda row: row["similarity"], reverse=True)
        return {"results": results, "documents": indexed[:args["limit"]], "embedding": "existing deterministic store; no inference"}
    if name == "perf.read":
        with sqlite3.connect("file:" + str(host.router["PERFORMANCE_DB"]) + "?mode=ro", uri=True, timeout=1) as connection:
            rows = connection.execute("SELECT model_id,tokens,cost,latency_ms,created_at FROM performance WHERE user_id=? ORDER BY id DESC LIMIT ?", (binding.user_id, args["limit"])).fetchall()
        costs, truncated = [], False
        # Legacy cost rows have no owner field: admit only still-owned conversations.
        from davellm_files import open_admitted_file
        import os
        path = host.router["COST_LOG"]
        if path.exists():
            descriptor = open_admitted_file(path)
            try:
                size = os.fstat(descriptor).st_size
                offset = max(0, size - 262144)
                os.lseek(descriptor, offset, os.SEEK_SET)
                lines = os.read(descriptor, 262144).decode("utf-8", "replace").splitlines()
                if offset:
                    lines = lines[1:]
                truncated = bool(offset) or len(lines) > 1000
                for line in reversed(lines[-1000:]):
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    record = host.router["CONVERSATIONS"].get(row.get("conversation_id")) if isinstance(row, dict) else None
                    if record and record.get("user_id", "default") == binding.user_id:
                        costs.append({key: row.get(key) for key in ("model", "node", "tokens", "cost", "timestamp")})
                        if len(costs) == args["limit"]:
                            break
            finally:
                os.close(descriptor)
        return {"performance": [{"model": r[0], "tokens": r[1], "cost": r[2], "latency_ms": r[3], "at": r[4]} for r in rows],
                "costs": costs, "cost_scan_truncated": truncated, "scope": "run owner"}
    if name in ("project.notepad.write", "memory.propose", "project.brain.pin"):
        if not binding.project_id:
            raise ToolpackError("This tool requires a project-scoped run")
        project = host.router["get_project"](binding.project_id, binding.user_id)
        if name == "project.brain.pin":
            current = host.router["PROJECT_CONTEXT"].get_brain(binding.project_id)
            if current["revision"] != args["expected_revision"] or binding.brain_revision != args["expected_revision"]:
                raise ToolpackError("BRAIN changed; read the current revision before proposing a new call")
            updated = host.router["PROJECT_CONTEXT"].update_brain(binding.project_id,
                pinned_text=current["pinned_text"] + "\n" + args["text"], expected_revision=args["expected_revision"])
            fresh = host.router["PROJECT_CONTEXT"].get_brain(binding.project_id)
            return {"outcome": "verified" if fresh["revision"] == updated["revision"] and fresh["pinned_text"] == updated["pinned_text"] else "unknown", "revision": updated["revision"]}
        before = str(project.get("notepad") or "")
        if digest(before) != args["expected_digest"]:
            raise ToolpackError("Notepad changed; nothing was written")
        after = args["text"] if name == "project.notepad.write" else before + "\nMemory proposal: " + args["text"]
        host.router["update_project_notepad"](binding.project_id, host.router["NotepadUpdate"](content=after), binding.user_id)
        return {"outcome": "verified" if host.router["get_project_notepad"](binding.project_id, binding.user_id)["content"] == after else "unknown", "digest": digest(after)}
    raise ToolpackError("Tool implementation is unavailable")
