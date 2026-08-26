#!/usr/bin/env python3
"""Authenticated local CLI for inspecting and operating project BRAIN state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def request_json(
    api_base: str,
    api_key: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{api_base.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"DaveLLM API unavailable: {exc.reason}") from exc
    return json.loads(data) if data else {}


def read_value(value: str | None, file_path: str | None) -> str | None:
    if file_path:
        return Path(file_path).read_text(encoding="utf-8")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read, edit, pin, compact, delete, and restore project BRAIN state.",
    )
    parser.add_argument(
        "--api-base",
        default=os.getenv("DAVE_API_BASE", "http://127.0.0.1:8000"),
        help="DaveLLM router origin (default: %(default)s)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("show", "compact", "delete", "revisions"):
        command = subparsers.add_parser(name)
        command.add_argument("project_id")

    restore = subparsers.add_parser("restore")
    restore.add_argument("project_id")
    restore.add_argument("revision", type=int)

    edit = subparsers.add_parser("edit")
    edit.add_argument("project_id")
    pinned_source = edit.add_mutually_exclusive_group()
    pinned_source.add_argument("--pinned")
    pinned_source.add_argument("--pinned-file")
    active_source = edit.add_mutually_exclusive_group()
    active_source.add_argument("--active")
    active_source.add_argument("--active-file")
    recent_source = edit.add_mutually_exclusive_group()
    recent_source.add_argument("--recent")
    recent_source.add_argument("--recent-file")
    edit.add_argument("--compact-threshold", type=int)
    edit.add_argument("--expected-revision", type=int)

    pin = subparsers.add_parser("pin")
    pin.add_argument("project_id")
    pin_source = pin.add_mutually_exclusive_group(required=True)
    pin_source.add_argument("--text")
    pin_source.add_argument("--file")
    return parser


def run(args: argparse.Namespace, api_key: str) -> dict[str, Any]:
    project = quote(args.project_id, safe="")
    brain_path = f"/projects/{project}/brain"
    if args.command == "show":
        return request_json(args.api_base, api_key, "GET", brain_path)
    if args.command == "compact":
        return request_json(args.api_base, api_key, "POST", f"{brain_path}/compact")
    if args.command == "delete":
        return request_json(args.api_base, api_key, "DELETE", brain_path)
    if args.command == "revisions":
        return request_json(args.api_base, api_key, "GET", f"{brain_path}/revisions")
    if args.command == "restore":
        return request_json(
            args.api_base,
            api_key,
            "POST",
            f"{brain_path}/revisions/{args.revision}/restore",
        )
    if args.command == "pin":
        current = request_json(args.api_base, api_key, "GET", brain_path)
        addition = read_value(args.text, args.file) or ""
        prior = str(current.get("pinned_text") or "").rstrip()
        pinned = f"{prior}\n{addition.strip()}".strip()
        return request_json(
            args.api_base,
            api_key,
            "PUT",
            brain_path,
            {
                "pinned_text": pinned,
                "expected_revision": current["revision"],
            },
        )
    if args.command == "edit":
        payload: dict[str, Any] = {}
        field_sources = (
            ("pinned_text", args.pinned, args.pinned_file),
            ("active_text", args.active, args.active_file),
            ("recent_text", args.recent, args.recent_file),
        )
        for field, value, file_path in field_sources:
            resolved = read_value(value, file_path)
            if resolved is not None:
                payload[field] = resolved
        if args.compact_threshold is not None:
            payload["compact_threshold"] = args.compact_threshold
        if args.expected_revision is not None:
            payload["expected_revision"] = args.expected_revision
        if not payload:
            raise RuntimeError("edit requires at least one value or threshold")
        return request_json(args.api_base, api_key, "PUT", brain_path, payload)
    raise RuntimeError(f"Unsupported command: {args.command}")


def main() -> int:
    args = build_parser().parse_args()
    api_key = os.getenv("DAVE_API_KEY", "")
    if not api_key:
        print("DAVE_API_KEY is required", file=sys.stderr)
        return 2
    try:
        result = run(args, api_key)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
