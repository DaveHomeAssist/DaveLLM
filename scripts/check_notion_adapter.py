#!/usr/bin/env python3
"""Check Notion configuration; --live makes a bounded read, never a write or model call."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from davellm_notion import NotionSettings, NotionToolError, RunLedger, read_page  # noqa: E402


def _enabled(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


async def check_adapter(environ: Mapping[str, str], *, page: str = "adapter-test",
                        live: bool = False) -> dict[str, Any]:
    """Report only safe metadata about this process, not a running router's settings."""
    settings = NotionSettings.from_values(environ.get("DAVE_NOTION_TOKEN"),
                                          environ.get("DAVE_NOTION_PAGES"))
    issues = []
    for name in ("DAVE_ENABLE_TOOLS", "DAVE_ENABLE_NOTION_TOOLS"):
        if not _enabled(environ, name):
            issues.append(f"{name} must be enabled")
    if settings.token_invalid:
        issues.append("DAVE_NOTION_TOKEN is invalid")
    elif settings.token is None:
        issues.append("DAVE_NOTION_TOKEN is unset")
    if settings.pages_invalid:
        issues.append("DAVE_NOTION_PAGES is invalid")
    elif not settings.pages:
        issues.append("DAVE_NOTION_PAGES is unset or empty")
    elif page not in settings.pages:
        issues.append("The selected page alias is not configured")

    report: dict[str, Any] = {
        "configuration_ready": not issues,
        "configured_page_count": len(settings.pages),
        "issues": issues,
        "live": "not_checked",
        "scope": "this_process_environment",
        "router_registration": "not_checked",
        "write_acceptance": "not_checked",
    }
    if not live or issues:
        return report

    try:
        result = await read_page({"page": page}, settings=settings, ledger=RunLedger())
    except NotionToolError:
        report["live"] = "failed"
        report["issues"] = ["Notion read failed; check connection access, token validity and page sharing"]
    except Exception:
        # Unexpected transport errors can contain credentials or page data. Do not echo them.
        report["live"] = "failed"
        report["issues"] = ["Unexpected Notion read failure; no remote details were printed"]
    else:
        blocks = result["blocks"]
        partial = result["truncated"] or any(block.get("children_not_read") for block in blocks)
        report["live"] = "readable_partial" if partial else "readable"
        report["blocks_read"] = len(blocks)
        report["truncated"] = result["truncated"]
        report["children_not_read"] = sum(bool(block.get("children_not_read")) for block in blocks)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", default="adapter-test", help="Configured alias, never a page ID or URL")
    parser.add_argument("--live", action="store_true", help="Explicitly permit a bounded, GET-only Notion read")
    args = parser.parse_args(argv)
    report = asyncio.run(check_adapter(os.environ, page=args.page, live=args.live))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["configuration_ready"] and report["live"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
