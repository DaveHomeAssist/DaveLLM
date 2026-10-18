#!/usr/bin/env python3
"""Persist one structured DaveLLM on-call check for the Cluster interface."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_HISTORY = 168
MAX_FILE_BYTES = 1024 * 1024
OVERALL_STATES = ("healthy", "degraded", "outage", "recovered")
TARGET_STATES = ("healthy", "down", "recovered", "unresponsive")


def default_data_dir() -> Path:
    configured = os.environ.get("DAVE_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "Library" / "Application Support" / "DaveLLM").resolve()


def parse_optional_number(value: str, *, integer: bool = False) -> int | float | None:
    if value.lower() in {"none", "null", "-"}:
        return None
    number = int(value) if integer else float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("numeric values must be non-negative")
    return number


def target_record(values: list[str]) -> dict:
    name, endpoint, status, latency_text, http_text = values
    if status not in TARGET_STATES:
        raise argparse.ArgumentTypeError(f"target status must be one of {', '.join(TARGET_STATES)}")
    if not endpoint.startswith(("http://", "https://")):
        raise argparse.ArgumentTypeError("target endpoint must use http or https")
    try:
        latency = parse_optional_number(latency_text)
        http_status = parse_optional_number(http_text, integer=True)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if http_status is not None and not 100 <= http_status <= 599:
        raise argparse.ArgumentTypeError("HTTP status must be between 100 and 599")
    return {
        "name": name,
        "endpoint": endpoint,
        "status": status,
        "latency_ms": latency,
        "http_status": http_status,
    }


def read_existing(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "current": None, "history": []}
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("existing monitor file exceeds 1 MiB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("history"), list):
        raise ValueError("existing monitor file has an invalid shape")
    return data


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".on_call_status.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--overall", choices=OVERALL_STATES, required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument(
        "--target", action="append", nargs=5, metavar=("NAME", "ENDPOINT", "STATUS", "LATENCY_MS", "HTTP_STATUS"),
        required=True, help="repeat for each checked target; use 'none' when latency or HTTP status is unavailable",
    )
    parser.add_argument("--recovery-node")
    parser.add_argument("--recovery-steps")
    parser.add_argument("--recovery-seconds", type=float)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.recovery_seconds is not None and args.recovery_seconds < 0:
        raise SystemExit("--recovery-seconds must be non-negative")
    targets = [target_record(values) for values in args.target]
    if len({target["name"].casefold() for target in targets}) != len(targets):
        raise SystemExit("target names must be unique")
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    current = {
        "checked_at": checked_at,
        "overall": args.overall,
        "summary": args.summary,
        "targets": targets,
        "recovery": {
            "node": args.recovery_node,
            "steps": args.recovery_steps,
            "elapsed_seconds": args.recovery_seconds,
        } if any((args.recovery_node, args.recovery_steps, args.recovery_seconds is not None)) else None,
    }
    output = args.data_dir.expanduser().resolve() / "on_call_status.json"
    existing = read_existing(output)
    history = [item for item in existing.get("history", []) if isinstance(item, dict)]
    history.append(current)
    atomic_write(output, {"version": 1, "current": current, "history": history[-MAX_HISTORY:]})
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
