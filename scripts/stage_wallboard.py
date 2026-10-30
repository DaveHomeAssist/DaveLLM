#!/usr/bin/env python3
"""Prepare or verify an inert wallboard bundle. Never installs or starts services."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from serve_wallboard import ASSETS, read_regular

SCRIPTS = ("serve_wallboard.py", "export_wallboard_status.py", "record_monitor_status.py")
UNIT = "davellm-wallboard.service"
FILES = (*ASSETS, *SCRIPTS, UNIT)


def verify(output: Path) -> None:
    output = output.absolute()
    receipt = json.loads(read_regular(output / "manifest.json"))
    if set(receipt) != set(FILES) or {p.name for p in output.iterdir()} != {*FILES, "manifest.json"}:
        raise ValueError("unexpected bundle contents")
    for name in FILES:
        if hashlib.sha256(read_regular(output / name)).hexdigest() != receipt[name]:
            raise ValueError("bundle changed")


def stage(output: Path) -> None:
    output = output.absolute()
    if output.parent.resolve() != output.parent:
        raise ValueError("symlink parent")
    repository = Path(__file__).resolve().parents[1]
    # Validate all inputs before creating the new destination.
    inputs = {name: read_regular(repository / "static" / name) for name in ASSETS}
    inputs.update({name: read_regular(repository / "scripts" / name) for name in SCRIPTS})
    inputs[UNIT] = read_regular(repository / "deploy" / "wallboard" / UNIT)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        for name, content in inputs.items():
            (output / name).write_bytes(content)
        (output / "manifest.json").write_text(json.dumps({n: hashlib.sha256(c).hexdigest() for n, c in inputs.items()}, indent=2) + "\n")
        verify(output)
    except Exception:
        # This directory was exclusively created by this invocation.
        shutil.rmtree(output)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    try:
        verify(args.output) if args.verify else stage(args.output)
    except (OSError, ValueError, RuntimeError):
        parser.exit(1, "Bundle refused; no service changed.\n")
    print("Bundle verified; no service installed or started.")
