#!/usr/bin/env python3
"""Report nonsecret Tools100 configuration readiness without contacting anything."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from davellm_toolpack_catalog import ALL_SPECS, BY_NAME  # noqa: E402

MAX_CONFIG_BYTES = 65_536
READY, MISSING, UNKNOWN = "Ready", "Missing", "Unknown"


def _selected_names(config: Mapping[str, Any]) -> set[str]:
    selection = config.get("enabled_tools")
    if (not isinstance(selection, list) or not selection or len(selection) > 200
            or not all(isinstance(item, str) and item for item in selection)):
        return set()
    chosen: set[str] = set()
    for item in selection:
        if item.endswith(".*"):
            matches = {spec.name for spec in ALL_SPECS if spec.name.startswith(item[:-1])}
        else:
            matches = {item} & set(BY_NAME)
        if not matches:
            return set()
        chosen.update(matches)
    return chosen


def _strings(value: Any, *, nonempty: bool = True) -> bool:
    return isinstance(value, list) and (bool(value) or not nonempty) and all(
        isinstance(item, str) and bool(item) for item in value
    )


def _mapping(value: Any) -> bool:
    return isinstance(value, dict) and bool(value)


def _absolute(value: Any) -> bool:
    return isinstance(value, str) and Path(value).is_absolute()


def _owned_mapping(value: Any) -> bool:
    return _mapping(value) and all(
        isinstance(item, dict) and _absolute(item.get("path")) and _strings(item.get("users"))
        for item in value.values()
    )


def _runners(value: Any) -> bool:
    required_paths = ("python", "script", "config")
    return _mapping(value) and all(
        isinstance(item, dict)
        and item.get("os") == "posix"
        and isinstance(item.get("ssh_alias"), str) and bool(item["ssh_alias"])
        and isinstance(item.get("host"), str) and bool(item["host"])
        and all(_absolute(item.get(key)) for key in required_paths)
        and _strings(item.get("users"))
        and _strings(item.get("tools"))
        and _strings(item.get("capabilities"))
        for item in value.values()
    )


def _notion_databases(value: Any) -> bool:
    return _mapping(value) and all(
        isinstance(item, dict)
        and isinstance(item.get("id"), str) and bool(item["id"])
        and _strings(item.get("users"))
        and _strings(item.get("writable_properties"), nonempty=False)
        for item in value.values()
    )


def _services(value: Any) -> bool:
    return _mapping(value) and all(
        isinstance(item, dict)
        and isinstance(item.get("url"), str)
        and item["url"].startswith(("https://", "http://"))
        for item in value.values()
    )


def _notifications(value: Any) -> bool:
    return _mapping(value) and all(
        isinstance(item, dict)
        and isinstance(item.get("url"), str) and item["url"].startswith("https://")
        and isinstance(item.get("topic"), str) and bool(item["topic"])
        for item in value.values()
    )


def _repositories(value: Any) -> bool:
    return _mapping(value) and all(
        isinstance(item, str) and item.count("/") == 1 and all(item.split("/"))
        for item in value.values()
    )


def _wake_targets(value: Any) -> bool:
    return _mapping(value) and all(
        isinstance(item, dict)
        and isinstance(item.get("mac"), str) and bool(item["mac"])
        and isinstance(item.get("broadcast"), str) and bool(item["broadcast"])
        for item in value.values()
    )


VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "sources": _owned_mapping,
    "runners": _runners,
    "notion_databases": _notion_databases,
    "services": _services,
    "notifications": _notifications,
    "repositories": _repositories,
    "wake_targets": _wake_targets,
}

for _key in (
    "expected_models", "allowed_model_pulls", "model_storage", "vercel_projects",
    "calendars", "drive_folders", "drive_files", "osc_targets", "glossary", "midi",
    "av_inventory",
):
    VALIDATORS[_key] = _mapping
VALIDATORS["google_users"] = _strings
VALIDATORS["vector_database_source"] = lambda value: isinstance(value, str) and bool(value)


GROUPS: tuple[dict[str, Any], ...] = (
    {"id": "EX-01", "name": "cluster.models", "tools": ("ollama.ps", "model.inventory_drift", "node.ctx_check", "ollama.version_check"), "keys": ("expected_models",), "runtime": True},
    {"id": "EX-03", "name": "node.diagnose / node.wake", "tools": ("node.diagnose", "node.wake"), "keys": ("wake_targets",), "runtime": True},
    {"id": "EX-16", "name": "server.ops", "tools": ("service.health", "node.disk", "mac.disk.report", "mac.pressure", "docker.ps", "docker.logs", "tailscale.status"), "keys": ("services", "runners"), "runner_tools": ("node.disk", "mac.disk.report", "mac.pressure", "docker.ps", "docker.logs", "tailscale.status")},
    {"id": "EX-14", "name": "notify.push", "tools": ("notify.push",), "keys": ("notifications",), "secret": True, "live": True},
    {"id": "EX-13", "name": "ops.governance", "tools": ("rules.lookup", "machine.access.lookup", "comms.log.read", "nextsteps.read", "nextsteps.update"), "keys": ("sources", "runners"), "runner_tools": ("nextsteps.update",)},
    {"id": "EX-10", "name": "github", "tools": ("gh.pr.status", "gh.pr.comments", "gh.ci.logs", "gh.issue.create", "pages.deploy_status", "vercel.deploy_status"), "keys": ("repositories",), "secret": True, "live": True},
    {"id": "EX-20", "name": "data.tools", "tools": ("calc.eval", "time.convert", "json.validate", "data.query", "diagram.render"), "runner_tools": ("data.query", "diagram.render")},
    {"id": "EX-06", "name": "chat.history", "tools": ("chat.read", "chat.export", "perf.read", "token.count", "chat.summarize"), "inference_tools": ("chat.summarize",)},
    {"id": "EX-09", "name": "Notion adapter v2", "prefixes": ("notion.",), "keys": ("notion_databases",), "secret": True, "live": True},
    {"id": "EX-07", "name": "memory", "tools": ("memory.propose", "memory.recall", "claude.memory.read", "vector.search", "docs.index", "project.notepad.write", "project.brain.pin"), "keys": ("sources", "vector_database_source"), "runner_tools": ("docs.index",)},
    {"id": "EX-15", "name": "home.assistant", "prefixes": ("ha.",), "keys": ("services",), "secret": True, "live": True},
    {"id": "EX-12", "name": "code.check", "tools": ("code.symbols", "code.grep_regex", "test.run", "lint.run", "dep.audit"), "keys": ("sources", "runners"), "runner_tools": ("code.grep_regex", "test.run", "lint.run", "dep.audit")},
    {"id": "EX-04", "name": "route.plan", "tools": ("route.suggest", "token.count"), "runtime": True},
    {"id": "EX-05", "name": "model.ask", "tools": ("model.ask", "model.consensus"), "inference": True, "runtime": True},
    {"id": "EX-21", "name": "quality", "tools": ("prompt.lint", "secret.scan", "perf.read", "cluster.benchmark", "eval.run"), "inference_tools": ("cluster.benchmark", "eval.run")},
    {"id": "EX-02", "name": "cluster.model.manage", "tools": ("model.delete", "model.unload", "model.warm", "model.pull"), "keys": ("allowed_model_pulls", "model_storage"), "runner_tools": ("model.pull",), "live": True},
    {"id": "EX-11", "name": "git.write", "tools": ("git.worktrees", "git.write_preview", "git.commit", "git.push", "agent.claim.check"), "keys": ("sources", "runners"), "runner_tools": ("git.commit", "git.push"), "secret_tools": ("git.push",)},
    {"id": "EX-08", "name": "docs.ask", "tools": ("docs.index", "docs.ask", "pdf.read", "ocr.image"), "keys": ("sources", "vector_database_source", "runners"), "runner_tools": ("docs.index", "pdf.read", "ocr.image"), "inference_tools": ("docs.ask",)},
    {"id": "EX-17", "name": "google", "prefixes": ("gmail.", "calendar.", "drive.", "contacts."), "keys": ("google_users",), "secret": True, "live": True},
    {"id": "EX-18", "name": "av.show", "tools": ("dmx.patch_check", "artnet.send", "osc.send", "resolume.status", "midi.map.lookup", "av.inventory.lookup"), "keys": ("osc_targets", "midi", "av_inventory"), "runner_tools": ("artnet.send",), "live": True},
    {"id": "EX-19", "name": "media", "tools": ("video.gap_find", "setlist.parse", "video.probe", "audio.transcribe", "ocr.image", "image.describe"), "runner_tools": ("video.probe", "audio.transcribe", "ocr.image"), "inference_tools": ("image.describe",)},
)


RUNNER_EXECUTABLES = ("git", "docker", "tailscale", "vm_stat", "sysctl", "ps", "ffprobe", "tesseract", "whisper", "mmdc")


def _safe_json(path: Path) -> tuple[str, Any | None, str]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return MISSING, None, "not_found"
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        return MISSING, None, "not_private_regular_file"
    if before.st_uid != os.geteuid() or before.st_mode & 0o022 or before.st_size > MAX_CONFIG_BYTES:
        return MISSING, None, "not_private_regular_file"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return MISSING, None, "file_changed"
            if (opened.st_uid != os.geteuid() or opened.st_mode & 0o022
                    or not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_CONFIG_BYTES):
                return MISSING, None, "not_private_regular_file"
            raw = source.read(MAX_CONFIG_BYTES + 1)
    except (OSError, PermissionError):
        return MISSING, None, "unreadable"
    if len(raw) > MAX_CONFIG_BYTES:
        return MISSING, None, "too_large"
    try:
        return READY, json.loads(raw), "valid_json"
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return MISSING, None, "invalid_json"


def _file_report(path: Path, expected: Callable[[Any], bool]) -> tuple[dict[str, Any], Any | None]:
    status, value, reason = _safe_json(path)
    if status == READY and not expected(value):
        status, value, reason = MISSING, None, "invalid_shape"
    return {"status": status, "reason": reason}, value


def _requirement(name: str, status: str) -> dict[str, str]:
    return {"name": name, "status": status}


def _aggregate(requirements: Sequence[Mapping[str, str]]) -> str:
    statuses = {item["status"] for item in requirements}
    if MISSING in statuses:
        return MISSING
    if UNKNOWN in statuses:
        return UNKNOWN
    return READY


def _selected(group: Mapping[str, Any], enabled: set[str]) -> list[str]:
    names = set(group.get("tools", ()))
    prefixes = tuple(group.get("prefixes", ()))
    return sorted(name for name in enabled if name in names or name.startswith(prefixes))


def check_readiness(data_dir: Path, runner_paths: Mapping[str, Path] | None = None) -> dict[str, Any]:
    toolpack_report, toolpack = _file_report(
        data_dir / "toolpack.json",
        lambda value: (
            isinstance(value, dict)
            and _strings(value.get("enabled_tools"))
            and len(value["enabled_tools"]) <= 200
            and bool(_selected_names(value))
        ),
    )
    roots_report, roots = _file_report(
        data_dir / "tool-roots.json",
        lambda value: _strings(value) and all(_absolute(item) for item in value),
    )
    config = toolpack if isinstance(toolpack, dict) else {}
    enabled = _selected_names(config) if toolpack_report["status"] == READY else set()
    selection_status = READY if enabled else MISSING

    key_status: dict[str, str] = {}
    for key, validator in VALIDATORS.items():
        key_status[key] = READY if key in config and validator(config[key]) else MISSING

    runner_reports: dict[str, Any] = {}
    for alias, path in sorted((runner_paths or {}).items()):
        report, runner = _file_report(path, lambda value: isinstance(value, dict))
        if isinstance(runner, dict):
            fields = {
                "owners": READY if _strings(runner.get("users")) else MISSING,
                "tools": READY if _strings(runner.get("tools")) else MISSING,
                "capabilities": READY if _strings(runner.get("capabilities")) else MISSING,
                "executables": {
                    key: READY if _absolute(runner.get("executables", {}).get(key)) else MISSING
                    for key in RUNNER_EXECUTABLES
                },
            }
            report["fields"] = fields
            report["status"] = _aggregate([
                _requirement("owners", fields["owners"]),
                _requirement("tools", fields["tools"]),
                _requirement("capabilities", fields["capabilities"]),
            ])
        runner_reports[alias] = report

    groups = []
    for group in GROUPS:
        selected = _selected(group, enabled)
        requirements = [_requirement("enabled_tools", READY if selected else MISSING)]
        for key in group.get("keys", ()):
            requirements.append(_requirement(key, key_status[key]))
        if set(selected) & set(group.get("runner_tools", ())):
            if key_status["runners"] == MISSING:
                runner_status = MISSING
            elif not runner_reports:
                runner_status = UNKNOWN
            elif any(item["status"] == READY for item in runner_reports.values()):
                runner_status = READY
            else:
                runner_status = MISSING
            requirements.append(_requirement("runner_config", runner_status))
        if group.get("secret") or set(selected) & set(group.get("secret_tools", ())):
            requirements.append(_requirement("credential_grant", UNKNOWN))
        if group.get("inference") or set(selected) & set(group.get("inference_tools", ())):
            requirements.append(_requirement("inference_acceptance", UNKNOWN))
        if group.get("runtime"):
            requirements.append(_requirement("runtime_capability", UNKNOWN))
        if group.get("live"):
            requirements.append(_requirement("live_acceptance", UNKNOWN))
        groups.append({
            "group": group["id"],
            "name": group["name"],
            "status": _aggregate(requirements),
            "enabled_tools": selected,
            "requirements": requirements,
        })

    all_requirements = [item for group in groups for item in group["requirements"]]
    return {
        "overall": _aggregate(all_requirements),
        "scope": "local_nonsecret_files_only",
        "files": {"toolpack": toolpack_report, "tool_roots": roots_report},
        "enabled_tools": {"status": selection_status, "count": len(enabled), "names": sorted(enabled)},
        "configuration_keys": key_status,
        "runner_configs": runner_reports,
        "groups": groups,
        "secrets": "not_read",
        "network": "not_used",
        "live_acceptance": "not_checked",
    }


def _runner_arguments(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        alias, separator, raw_path = value.partition("=")
        if not separator or not alias or not Path(raw_path).is_absolute():
            raise ValueError("--runner-config must be ALIAS=/absolute/path.json")
        result[alias] = Path(raw_path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path,
        default=Path.home() / "Library" / "Application Support" / "DaveLLM",
        help="Directory containing optional toolpack.json and tool-roots.json files",
    )
    parser.add_argument(
        "--runner-config", action="append", default=[], metavar="ALIAS=/ABSOLUTE/PATH",
        help="Explicitly inspect one local nonsecret runner-config snapshot; never contacts the runner",
    )
    args = parser.parse_args(argv)
    try:
        runners = _runner_arguments(args.runner_config)
    except ValueError as exc:
        parser.error(str(exc))
    report = check_readiness(args.data_dir, runners)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["overall"] == READY else 1


if __name__ == "__main__":
    raise SystemExit(main())
