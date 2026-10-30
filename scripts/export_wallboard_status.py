#!/usr/bin/env python3
"""Export a bounded display projection without free-form operational text."""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from record_monitor_status import atomic_write, MAX_FILE_BYTES, MAX_HISTORY

NAMES = {"Local", "Dominic", "Walter", "Duncan"}
STATES = {"healthy", "degraded", "outage", "recovered"}
TARGET_STATES = {"healthy", "down", "recovered", "unresponsive"}


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def project_record(record):
    if not isinstance(record, dict) or record.get("overall") not in STATES:
        raise ValueError("invalid state")
    targets = record.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 4:
        raise ValueError("invalid targets")
    safe = []
    for target in targets:
        if not isinstance(target, dict) or target.get("name") not in NAMES or target.get("status") not in TARGET_STATES:
            raise ValueError("invalid target")
        endpoint = urlsplit(target.get("endpoint", ""))
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in {"/health", "/api/tags"}:
            raise ValueError("unsafe endpoint")
        latency, code = target.get("latency_ms"), target.get("http_status")
        if latency is not None and (type(latency) not in {int, float} or not math.isfinite(latency) or latency < 0):
            raise ValueError("invalid latency")
        if code is not None and (type(code) is not int or not 100 <= code <= 599):
            raise ValueError("invalid HTTP status")
        safe.append({"name": target["name"], "endpoint": endpoint.geturl(), "status": target["status"], "latency_ms": latency, "http_status": code})
    if len({t["name"] for t in safe}) != len(safe) or {t["name"] for t in safe} != NAMES:
        raise ValueError("duplicate target")
    if record["overall"] == "healthy" and any(t["status"] != "healthy" for t in safe):
        raise ValueError("healthy overall contradicts targets")
    recovery = record.get("recovery")
    if recovery is not None:
        if not isinstance(recovery, dict) or recovery.get("node") not in NAMES:
            raise ValueError("invalid recovery")
        elapsed = recovery.get("elapsed_seconds")
        if elapsed is not None and (type(elapsed) not in {int, float} or not math.isfinite(elapsed) or elapsed < 0):
            raise ValueError("invalid recovery duration")
        recovery = {"node": recovery["node"], "elapsed_seconds": elapsed, "steps": "Recovery recorded. Review Cluster for details."}
    return {"checked_at": timestamp(record.get("checked_at")), "overall": record["overall"], "summary": "Hourly check: " + record["overall"] + ".", "targets": safe, "recovery": recovery}


def project(payload):
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("history"), list) or len(payload["history"]) > MAX_HISTORY:
        raise ValueError("invalid recorder document")
    return {"version": 1, "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "current": project_record(payload.get("current")), "history": [project_record(r) for r in payload["history"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.source.is_symlink() or not args.source.is_file() or args.source.stat().st_size > MAX_FILE_BYTES:
        parser.error("source must be a bounded regular file")
    if args.source.resolve() == args.output.resolve() or args.output.is_symlink():
        parser.error("output must be a separate regular destination")
    try:
        payload = project(json.loads(args.source.read_text()))
    except (ValueError, TypeError, KeyError, OSError):
        parser.error("recorder document refused; output unchanged")
    atomic_write(args.output, payload)


if __name__ == "__main__":
    main()
