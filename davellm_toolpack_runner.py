"""Operator-configured isolated jobs. Remote helper enforces its own deadline.

No model-selected command, environment, host or configuration path is accepted.
The CLI reads a private operator-owned configuration; stdout is one bounded JSON
envelope. Optional media/data dependencies live on runners, not in the router.
"""
from __future__ import annotations

import csv
import base64
import io
import json
import os
import re
import shutil
import signal
import stat
import socket
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from davellm_edit import edit_file, _read_file, _write_temp, _unchanged, _discard
from davellm_files import (admit_path, anchor_path, has_secret_component, read_admitted_bytes,
                           open_directory_descriptor, walk_tree, decode_text)
from davellm_git import open_repository, DIFF_SAFETY, check_file
from davellm_toolpack import ToolpackError, digest, redact, _SENSITIVE
from davellm_toolpack_catalog import BY_NAME


class JobRunner:
    def __init__(self, config, user):
        self.config, self.user = config, user
        self.effect_started = False
        self.roots = [Path(root).resolve() for root in config.get("roots", [])]
        if not self.roots or any(not Path(root).is_absolute() for root in config.get("roots", [])) or user not in config.get("users", []):
            raise ToolpackError("Runner roots or user are not configured")

    def resolve(self, path):
        requested = anchor_path(str(path), self.roots)
        def inside(value):
            result = Path(value).resolve()
            if not any(result == root or result.is_relative_to(root) for root in self.roots):
                raise ToolpackError("Path is not allowed")
            return result
        return admit_path(requested, inside)

    def source(self, alias):
        target = self.config.get("sources", {}).get(alias)
        if not isinstance(target, dict) or self.user not in target.get("users", []):
            raise ToolpackError("Source is not approved for this user")
        return self.resolve(target["path"])

    def executable(self, name):
        configured = self.config.get("executables", {}).get(name)
        if not configured or not Path(configured).is_absolute() or not Path(configured).is_file():
            raise ToolpackError("Required runner executable is not configured")
        return configured

    def command(self, argv, *, cwd=None, input_data=None):
        # The outer CLI watchdog bounds this helper, its readers and all children.
        process = subprocess.Popen(argv, cwd=cwd, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                                   stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        completed = False
        try:
            if input_data is not None:
                process.stdin.write(input_data)
                process.stdin.close()
            data = process.stdout.read(65537)
            if len(data) > 65536:
                raise ToolpackError("Command output exceeded the budget")
            if process.wait(timeout=15):
                raise ToolpackError("Configured command failed")
            completed = True
            return data
        finally:
            if not completed:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if process.poll() is None:
                process.wait()

    def text(self, path, limit=65536):
        result = decode_text(read_admitted_bytes(path, limit))
        if result is None:
            raise ToolpackError("Source is not UTF-8 text")
        return result

    def execute(self, name, args):
        if name not in self.config.get("tools", []):
            raise ToolpackError("Operation is not configured on this runner")
        if name in ("node.disk", "mac.disk.report"):
            location = self.source(self.config["storage_source"])
            usage = shutil.disk_usage(location)
            mount = self.config.get("mount_path")
            return {"free_bytes": usage.free, "total_bytes": usage.total,
                    "mount_ready": os.path.ismount(self.resolve(mount)) if mount else None,
                    "mount_state": "observed" if mount else "Unknown"}
        if name == "mac.pressure":
            memory = self.command([self.executable("vm_stat")]).decode("utf-8", "replace")
            swap = self.command([self.executable("sysctl"), "vm.swapusage"]).decode("utf-8", "replace")
            processes = self.command([self.executable("ps"), "-eo", "pid,pcpu,pmem,comm"]).decode("utf-8", "replace")
            return {"memory": memory[:4000], "swap": swap[:1000], "processes": processes.splitlines()[:30]}
        if name in ("docker.ps", "docker.logs"):
            containers = self.config.get("containers", {})
            if name == "docker.logs":
                target = containers.get(args["container"])
                if not target or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", target):
                    raise ToolpackError("Container is not allowlisted")
                raw = self.command([self.executable("docker"), "logs", "--tail", str(args["lines"]), "--since", "10m", target])
                return {"container": args["container"], "log": redact(raw.decode("utf-8", "replace"))}
            raw = self.command([self.executable("docker"), "ps", "--format", "{{json .}}"])
            reverse = {value: key for key, value in containers.items()}
            rows = [json.loads(line) for line in raw.splitlines()]
            return {"containers": [{"container": reverse[row["Names"]], "status": row.get("Status")}
                                   for row in rows if row.get("Names") in reverse][:50]}
        if name == "tailscale.status":
            status = json.loads(self.command([self.executable("tailscale"), "status", "--json"]))
            return {"peers": [{"name": row.get("HostName"), "online": row.get("Online")}
                              for row in list(status.get("Peer", {}).values())[:50]]}
        if name in ("test.run", "lint.run", "dep.audit"):
            recipe = self.config.get("recipes", {}).get(name, {}).get(args["recipe"])
            if not recipe or not isinstance(recipe.get("argv"), list) or not 1 <= len(recipe["argv"]) <= 30:
                raise ToolpackError("Recipe is not configured")
            if not Path(recipe["argv"][0]).is_absolute() or any(not isinstance(arg, str) for arg in recipe["argv"]):
                raise ToolpackError("Recipe argv is invalid")
            result = self.command(recipe["argv"], cwd=self.source(recipe["source"]))
            return {"recipe": args["recipe"], "output": redact(result.decode("utf-8", "replace")), "outcome": "verified"}
        if name == "code.grep_regex":
            pattern = re.compile(args["pattern"])
            root = self.source(args["source"])
            tree = walk_tree(root, root, max_depth=3, max_entries=100)
            found = []
            for entry in tree.entries:
                if entry.kind != "file":
                    continue
                path = self.resolve(str(root / entry.path))
                text = self.text(path)
                for index, line in enumerate(text.splitlines()[:2000], 1):
                    if pattern.search(line[:4000]):
                        found.append({"path": str(path.relative_to(root)), "line": index, "text": line[:300]})
                    if len(found) >= 50:
                        return {"matches": found, "truncated": True}
            return {"matches": found, "truncated": tree.truncated}
        if name == "pdf.read":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(read_admitted_bytes(self.resolve(args["path"]), 1024 * 1024)), strict=True)
            start = args["start_page"] - 1
            return {"pages": [{"page": index + 1, "text": (reader.pages[index].extract_text() or "")[:8000]}
                              for index in range(start, min(start + args["pages"], len(reader.pages)))],
                    "page_count": len(reader.pages), "truncated": start + args["pages"] < len(reader.pages)}
        if name == "data.query":
            import duckdb
            path = self.resolve(args["path"])
            text = self.text(path)
            rows = list(csv.DictReader(io.StringIO(text))) if path.suffix.casefold() == ".csv" else json.loads(text)
            if not isinstance(rows, list) or len(rows) > 1000 or any(not isinstance(row, dict) for row in rows):
                raise ToolpackError("Data must be at most 1000 object rows")
            columns = sorted({key for row in rows for key in row})
            if not columns or len(columns) > 30 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", column) for column in columns):
                raise ToolpackError("Data columns exceed the bounded table contract")
            with duckdb.connect(":memory:", config={"enable_external_access": "false", "autoload_known_extensions": "false", "autoinstall_known_extensions": "false", "threads": "1", "memory_limit": "128MB"}) as connection:
                connection.execute("CREATE TABLE records (" + ",".join('"' + column + '" VARCHAR' for column in columns) + ")")
                connection.executemany("INSERT INTO records VALUES (" + ",".join("?" for _ in columns) + ")", [[str(row.get(column, ""))[:2000] for column in columns] for row in rows])
                statements = connection.extract_statements(args["sql"])
                if len(statements) != 1 or str(statements[0].type).split(".")[-1] != "SELECT":
                    raise ToolpackError("Only one read-only SELECT is permitted")
                result = connection.execute(args["sql"])
                values = result.fetchmany(51)
                return {"columns": [column[0] for column in result.description], "rows": values[:50], "truncated": len(values) > 50}
        if name in ("video.probe", "audio.transcribe", "ocr.image"):
            path = self.resolve(args["path"])
            # Decoders receive a private copy read through descriptor admission,
            # never a pathname that can be swapped after its containment check.
            data = read_admitted_bytes(path, 64 * 1024 * 1024)
            with tempfile.TemporaryDirectory(prefix="davellm-media-") as scratch:
                copy = Path(scratch) / ("input" + path.suffix)
                copy.write_bytes(data)
                if name == "video.probe":
                    formats = {".mp4": "mov", ".mov": "mov", ".m4v": "mov", ".m4a": "mov", ".mkv": "matroska", ".webm": "matroska",
                               ".wav": "wav", ".mp3": "mp3", ".flac": "flac", ".aiff": "aiff", ".ogg": "ogg"}
                    format_name = formats.get(path.suffix.casefold())
                    if not format_name:
                        raise ToolpackError("Only standalone media container types are supported; playlists are refused")
                    argv = [self.executable("ffprobe"), "-v", "error", "-protocol_whitelist", "file", "-f", format_name]
                    if format_name == "mov":
                        argv.extend(["-enable_drefs", "0", "-use_absolute_path", "0"])
                    argv.extend(["-show_format", "-show_streams", "-of", "json", str(copy)])
                    record = json.loads(self.command(argv))
                    if isinstance(record.get("format"), dict):
                        record["format"].pop("filename", None)
                    return {"metadata": record}
                if name == "ocr.image":
                    if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
                            or data.startswith((b"II*\0", b"MM\0*")) or data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
                        raise ToolpackError("OCR requires a standalone PNG, JPEG, TIFF or WebP image")
                    return {"text": self.command([self.executable("tesseract"), str(copy), "stdout"]).decode("utf-8", "replace")}
                if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                    raise ToolpackError("Transcription requires standalone WAV input; playlists are refused")
                language = args["language"]
                if not re.fullmatch(r"[a-z]{2,3}|auto", language):
                    raise ToolpackError("Transcription language is invalid")
                model = self.source(self.config["whisper_model_source"])
                return {"text": self.command([self.executable("whisper"), "-m", str(model), "-f", str(copy), "-l", language, "-nt"]).decode("utf-8", "replace")}
        if name == "artnet.discover":
            target = self.config.get("interfaces", {}).get(args["interface"])
            if not target:
                raise ToolpackError("Art-Net interface is not allowlisted")
            replies = []
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sender.bind((target["bind"], 0))
                sender.settimeout(.25)
                sender.sendto(b"Art-Net\0\x00\x20\x00\x0e\x00\x00", (target["broadcast"], 6454))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and len(replies) < 10:
                    try:
                        data, _ = sender.recvfrom(1500)
                    except socket.timeout:
                        continue
                    if data.startswith(b"Art-Net\0\x00\x21") and len(data) >= 44:
                        replies.append({"short_name": data[26:44].split(b"\0")[0].decode("utf-8", "replace")})
            return {"nodes": replies, "discovery_seconds": 3, "physical_acceptance": "Unknown"}
        if name in ("git.commit", "git.push"):
            return self.git_write(name, args)
        if name == "comms.log.append":
            path = self.source(args["source"])
            before = self.text(path, 1048576)
            if digest(before) != args["expected_digest"] or before.count("# 📡 Recorded Agent Output") != 1:
                raise ToolpackError("Log changed or has no unique insertion anchor")
            anchor = "# 📡 Recorded Agent Output\n"
            self.effect_started = True
            update = edit_file({"path": str(path), "old_text": anchor, "new_text": anchor + "\n" + args["text"] + "\n\n---\n", "expected_count": 1}, resolve=self.resolve, roots=self.roots)
            after = self.text(path, 1048576)
            return {"outcome": "verified" if before.replace(anchor, "", 1) in after.replace(anchor + "\n" + args["text"] + "\n\n---\n", "", 1) else "unknown", "digest": digest(after)}
        if name == "nextsteps.update":
            path = self.source(args["source"])
            before = self.text(path)
            after = json.loads(args["record_json"])
            current = json.loads(before)
            if digest(before) != args["expected_digest"] or not isinstance(after, dict) or after.get("id") != current.get("id"):
                raise ToolpackError("Project record changed or has a different identity")
            updater = self.config.get("nextsteps_updater")
            if not updater:
                raise ToolpackError("Governed updater is not configured")
            node = self.executable("node")
            self.effect_started = True
            edit_file({"path": str(path), "old_text": before, "new_text": json.dumps(after, indent=2) + "\n", "expected_count": 1}, resolve=self.resolve, roots=self.roots)
            self.command([node, updater, "--project", after["id"], "--source", str(path)])
            self.command([node, updater, "--project", after["id"], "--source", str(path), "--check"])
            return {"outcome": "verified", "project": after["id"], "check": "passed"}
        if name == "vector.index":
            root = self.source(args["source"])
            tree = walk_tree(root, root, max_depth=3, max_entries=100)
            records = []
            for entry in tree.entries[:40]:
                if entry.kind == "file":
                    path = self.resolve(str(root / entry.path))
                    records.append((self.user, args["source"], str(path.relative_to(root)), self.text(path)[:16000]))
            database = self.source(self.config["vector_database_source"])
            # This is a dedicated offline index, never the active conversation DB.
            directory = open_directory_descriptor(database.parent)
            temporary = None
            try:
                before, data = _read_file(directory, database.name)
                if any(os.path.lexists(database.with_name(database.name + suffix)) for suffix in ("-wal", "-shm", "-journal")):
                    raise ToolpackError("Index is active; dedicated offline database required")
                with sqlite3.connect(":memory:") as connection:
                    if data:
                        connection.deserialize(data)
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if tables - {"tool_documents"}:
                        raise ToolpackError("Dedicated tool index required; existing application tables are preserved")
                    connection.execute("CREATE TABLE IF NOT EXISTS tool_documents (owner TEXT, source TEXT, path TEXT, content TEXT, PRIMARY KEY(owner,source,path))")
                    connection.execute("DELETE FROM tool_documents WHERE owner=? AND source=?", (self.user, args["source"]))
                    connection.executemany("INSERT INTO tool_documents VALUES (?,?,?,?)", records)
                    connection.commit()
                    updated = connection.serialize()
                    if len(updated) > 1048576:
                        raise ToolpackError("Index exceeds the one MiB budget")
                temporary = _write_temp(directory, updated, stat.S_IMODE(before.st_mode))
                _unchanged(directory, database.name, before, data)
                self.effect_started = True
                os.rename(temporary, database.name, src_dir_fd=directory, dst_dir_fd=directory)
                temporary = None
                os.fsync(directory)
                _, actual = _read_file(directory, database.name)
                verified = actual == updated
            finally:
                if temporary:
                    _discard(directory, temporary)
                os.close(directory)
            return {"outcome": "verified" if verified else "unknown", "indexed": len(records), "truncated": tree.truncated or len(tree.entries) > 40,
                    "embedding": "Dedicated retrievable text index; no external inference"}
        if name == "diagram.render":
            return self.diagram(args)
        raise ToolpackError("Runner implementation is unavailable")

    def git_write(self, name, args):
        path = self.source(args["repository"])
        repo = open_repository({"path": str(path)}, self.resolve, self.roots)
        if (repo.top / ".agent-claim").exists():
            raise ToolpackError("Repository is claimed; nothing was written")
        claim = repo.top / ".agent-claim"
        descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        claimed = os.fstat(descriptor)
        os.write(descriptor, b"agent: davellm-tools\nsession: approved Git operation\nscope: named files or configured push only\n")
        os.close(descriptor)
        try:
            if repo.output("diff", "--cached", "--name-only").stdout:
                raise ToolpackError("Existing staged work is preserved; refusing Git write")
            if name == "git.commit":
                if not args["files"] or len(set(args["files"])) != len(args["files"]):
                    raise ToolpackError("Commit needs distinct named files")
                files = [check_file(value) for value in args["files"]]
                tracked = repo.git("ls-files", "--error-unmatch", "--", *files)
                if tracked.returncode or tracked.truncated:
                    raise ToolpackError("Commit requires tracked named files; the index is unchanged")
                for value in files:
                    content = self.text(self.resolve(str(repo.top / value)))
                    if _SENSITIVE.search(content):
                        raise ToolpackError("Named file contains a credential candidate")
                diff = repo.output("diff", *DIFF_SAFETY, "--", *files)
                if diff.truncated or digest(diff.stdout.decode("utf-8", "replace")) != args["expected_digest"]:
                    raise ToolpackError("Diff changed or exceeded the preview budget")
                # Named index only: unrelated staged work was refused above; hooks and filters are disabled by run_git.
                self.effect_started = True
                repo.output("add", "--", *files)
                staged = repo.output("diff", "--cached", *DIFF_SAFETY, "--", *files)
                named = repo.output("diff", "--cached", "--name-only").stdout.decode().splitlines()
                if staged.truncated or digest(staged.stdout.decode("utf-8", "replace")) != args["expected_digest"] or set(named) - set(files) or _SENSITIVE.search(staged.stdout.decode("utf-8", "replace")):
                    raise ToolpackError("Staged snapshot no longer matches the approved named-file preview; inspect the index")
                author = self.config.get("git_author", {})
                # Commit the validated index, never restage working files through
                # commit's pathspec mode after checking the approved snapshot.
                repo.output("-c", "user.name=" + author.get("name", "DaveLLM operator"), "-c", "user.email=" + author.get("email", "operator@localhost"), "commit", "--no-gpg-sign", "-m", args["message"])
                return {"outcome": "verified", "head": repo.output("rev-parse", "HEAD").stdout.decode().strip()}
            branch = args["branch"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]{0,119}", branch) or "//" in branch or branch.endswith("/"):
                raise ToolpackError("Push branch is invalid")
            head = repo.output("rev-parse", "HEAD").stdout.decode().strip()
            current = repo.output("symbolic-ref", "--short", "HEAD").stdout.decode().strip()
            if head != args["expected_head"] or current != branch or repo.output("status", "--porcelain", "--", ".", ":(exclude).agent-claim").stdout:
                raise ToolpackError("Push requires the exact approved head and a clean matching branch")
            destination = self.config.get("git_destinations", {}).get(args["repository"])
            if not destination or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", destination):
                raise ToolpackError("Push destination is not configured")
            local = repo.output("config", "--local", "--list", "-z").stdout
            if any(record.split(b"\n", 1)[0].lower().startswith((b"url.", b"http.", b"https.", b"include.")) for record in local.split(b"\0")):
                raise ToolpackError("Repository network overrides are refused for an approved push")
            token = os.getenv("DAVE_GITHUB_TOKEN", "")
            if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
                raise ToolpackError("Runner-held GitHub credential is not configured")
            repo.env["GIT_ALLOW_PROTOCOL"] = "https"
            count = int(repo.env["GIT_CONFIG_COUNT"])
            settings = [("http." + destination + ".extraHeader", "Authorization: Basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()),
                        ("http.followRedirects", "false"), ("http.sslVerify", "true"), ("http.proxy", "")]
            for index, (key, value) in enumerate(settings, count):
                repo.env[f"GIT_CONFIG_KEY_{index}"] = key
                repo.env[f"GIT_CONFIG_VALUE_{index}"] = value
            repo.env["GIT_CONFIG_COUNT"] = str(count + len(settings))
            self.effect_started = True
            try:
                repo.output("-c", "protocol.https.allow=always", "push", "--no-verify", "--porcelain", destination, head + ":refs/heads/" + branch)
                remote = repo.output("-c", "protocol.https.allow=always", "ls-remote", destination, "refs/heads/" + branch).stdout.decode()
            finally:
                for index in range(count, count + len(settings)):
                    repo.env.pop(f"GIT_CONFIG_KEY_{index}", None)
                    repo.env.pop(f"GIT_CONFIG_VALUE_{index}", None)
                repo.env["GIT_CONFIG_COUNT"] = str(count)
            return {"outcome": "verified" if remote.split() and remote.split()[0] == head else "unknown", "head": head}
        finally:
            current_claim = claim.lstat()
            if (current_claim.st_dev, current_claim.st_ino) == (claimed.st_dev, claimed.st_ino):
                claim.unlink()

    def diagram(self, args):
        if re.search(r"(?i)click\s|%%\{init|<script|javascript:|<foreignObject", args["source"]):
            raise ToolpackError("Interactive or HTML Mermaid directives are refused")
        lines = [line.strip() for line in args["source"].splitlines() if line.strip()]
        # Reject fetch-capable syntax before rendering, not just in the output.
        node = r'[A-Za-z][A-Za-z0-9_]{0,39}(?:\["?[\w .,!?()/-]{1,80}"?\])?'
        edge = node + r'(?:\s*-->\s*' + node + r')*;?'
        if not lines or not re.fullmatch(r"flowchart (?:LR|RL|TD|TB|BT);?", lines[0]) or len(lines) > 80 or any(not re.fullmatch(edge, line) for line in lines[1:]):
            raise ToolpackError("Only bounded text-only Mermaid flowcharts are supported")
        output = self.resolve(args["output"])
        if output.suffix.casefold() != ".svg" or output.exists():
            raise ToolpackError("Output must be a new SVG inside a runner tool root")
        with tempfile.TemporaryDirectory(prefix="davellm-diagram-") as scratch:
            source, target = Path(scratch) / "source.mmd", Path(scratch) / "output.svg"
            source.write_text(args["source"], encoding="utf-8")
            config = Path(scratch) / "mermaid.json"
            config.write_text(json.dumps({"securityLevel": "strict", "htmlLabels": False, "flowchart": {"htmlLabels": False}, "maxTextSize": 4000}))
            self.command([self.executable("mmdc"), "-c", str(config), "-i", str(source), "-o", str(target)])
            data = read_admitted_bytes(target, 128 * 1024)
            if re.search(rb"(?i)<script|<foreignObject|\bon\w+\s*=|(?:href|src)\s*=\s*[\"'](?:https?:|file:|javascript:)", data):
                raise ToolpackError("Rendered SVG contains active or external content")
            parent = open_directory_descriptor(output.parent)
            try:
                fd = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                self.effect_started = True
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
            finally:
                os.close(parent)
        return {"outcome": "verified", "output": str(output.relative_to(next(root for root in self.roots if output.is_relative_to(root)))), "bytes": len(data)}
