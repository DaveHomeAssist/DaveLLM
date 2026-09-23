"""Durable project context storage and bounded request assembly for DaveLLM."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


DEFAULT_CONTEXT_BUDGET = 16_384
DEFAULT_BRAIN_COMPACT_TOKENS = 3_072
TEXT_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
DROP_RECENT_PREFIXES = (
    "[resolved]",
    "[superseded]",
    "[tool]",
    "tool call:",
    "tool result:",
)


class ProjectContextError(ValueError):
    """Base error for a rejected project-context operation."""


class ContextBudgetError(ProjectContextError):
    """Raised when a protected component cannot fit without truncation."""


class RevisionConflictError(ProjectContextError):
    """Raised when a stale editor attempts to replace a newer BRAIN revision."""


def estimate_tokens(text: str) -> int:
    """Return the same conservative character estimate used by the router."""
    return max(0, (len(str(text or "")) + 3) // 4)


def component_quotas(total_tokens: int) -> dict[str, int]:
    """Allocate the exact P4 25/25/30/20 project-context budget."""
    total = max(0, int(total_tokens))
    instructions = total // 4
    brain = total // 4
    files = round(total * 0.30)
    artifacts = total - instructions - brain - files
    return {
        "project_instructions": instructions,
        "brain": brain,
        "file_context": files,
        "artifact_history": artifacts,
    }


def _timestamp() -> str:
    return datetime.now().isoformat()


def _query_terms(query: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-z0-9_]{3,}", str(query or "").lower())
        if term not in {"and", "for", "from", "that", "the", "this", "with"}
    }


def _rank_text(text: str, terms: set[str]) -> int:
    lowered = str(text or "").lower()
    return sum(lowered.count(term) for term in terms)


def _bounded_text(text: str, token_limit: int, *, keep_tail: bool = False) -> str:
    character_limit = max(0, int(token_limit)) * 4
    if len(text) <= character_limit:
        return text
    if character_limit <= 32:
        return ""
    marker = "[Earlier compacted text omitted]\n" if keep_tail else "\n[Remaining text omitted]"
    available = character_limit - len(marker)
    if available <= 0:
        return ""
    return f"{marker}{text[-available:]}" if keep_tail else f"{text[:available]}{marker}"


class ProjectContextStore:
    """Own normalized project components while legacy project metadata stays JSON."""

    def __init__(
        self,
        database_path: Path,
        uploads_root: Path,
        *,
        default_context_budget: int = DEFAULT_CONTEXT_BUDGET,
        default_brain_threshold: int = DEFAULT_BRAIN_COMPACT_TOKENS,
    ) -> None:
        self.database_path = Path(database_path)
        self.uploads_root = Path(uploads_root)
        self.default_context_budget = max(1_024, int(default_context_budget))
        self.default_brain_threshold = max(128, int(default_brain_threshold))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.uploads_root.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._project_locks: dict[str, threading.Lock] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_profiles (
                    project_id TEXT PRIMARY KEY,
                    instructions TEXT NOT NULL DEFAULT '',
                    context_budget_tokens INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS brain_states (
                    project_id TEXT PRIMARY KEY REFERENCES project_profiles(project_id) ON DELETE CASCADE,
                    pinned_text TEXT NOT NULL DEFAULT '',
                    active_text TEXT NOT NULL DEFAULT '',
                    recent_text TEXT NOT NULL DEFAULT '',
                    compact_threshold INTEGER NOT NULL,
                    token_count INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    last_compacted_at TEXT,
                    deleted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS brain_revisions (
                    project_id TEXT NOT NULL REFERENCES project_profiles(project_id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, revision)
                );

                CREATE TABLE IF NOT EXISTS project_files (
                    file_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES project_profiles(project_id) ON DELETE CASCADE,
                    display_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    extracted_text TEXT NOT NULL DEFAULT '',
                    token_count INTEGER NOT NULL DEFAULT 0,
                    attached INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_file_chunks (
                    file_id TEXT NOT NULL REFERENCES project_files(file_id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    PRIMARY KEY (file_id, chunk_index)
                );

                CREATE TABLE IF NOT EXISTS project_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES project_profiles(project_id) ON DELETE CASCADE,
                    conversation_id TEXT,
                    source_message_index INTEGER,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    body TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_project_artifact_source
                    ON project_artifacts(project_id, conversation_id, source_message_index)
                    WHERE conversation_id IS NOT NULL AND source_message_index IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_project_files_project
                    ON project_files(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_project_artifacts_project
                    ON project_artifacts(project_id, pinned DESC, created_at DESC);
                """
            )
            file_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(project_files)").fetchall()
            }
            if "attached" not in file_columns:
                connection.execute(
                    "ALTER TABLE project_files ADD COLUMN attached INTEGER NOT NULL DEFAULT 1"
                )

    def _lock_for(self, project_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._project_locks.setdefault(project_id, threading.Lock())

    def ensure_project(
        self,
        project_id: str,
        *,
        instructions: str = "",
        context_budget_tokens: int | None = None,
    ) -> None:
        now = _timestamp()
        budget = int(context_budget_tokens or self.default_context_budget)
        quotas = component_quotas(budget)
        threshold = min(self.default_brain_threshold, max(128, quotas["brain"] - 64))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO project_profiles
                    (project_id, instructions, context_budget_tokens, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, instructions, budget, now, now),
            )
            row = connection.execute(
                "SELECT instructions FROM project_profiles WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row and not row["instructions"] and instructions:
                connection.execute(
                    "UPDATE project_profiles SET instructions = ?, updated_at = ? WHERE project_id = ?",
                    (instructions, now, project_id),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO brain_states
                    (project_id, compact_threshold, token_count, revision, updated_at)
                VALUES (?, ?, 0, 1, ?)
                """,
                (project_id, threshold, now),
            )
            brain = connection.execute(
                "SELECT * FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if brain:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO brain_revisions
                        (project_id, revision, snapshot_json, reason, created_at)
                    VALUES (?, ?, ?, 'created', ?)
                    """,
                    (project_id, brain["revision"], json.dumps(self._brain_snapshot(brain)), now),
                )

    def delete_project(self, project_id: str) -> None:
        with self._connect() as connection:
            stored_names = [
                row["stored_name"]
                for row in connection.execute(
                    "SELECT stored_name FROM project_files WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            ]
            connection.execute("DELETE FROM project_profiles WHERE project_id = ?", (project_id,))
        project_root = self.uploads_root / project_id
        for stored_name in stored_names:
            try:
                (project_root / stored_name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            project_root.rmdir()
        except OSError:
            pass

    def get_profile(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_profiles WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if not row:
            raise ProjectContextError(f"Project context for '{project_id}' was not initialized")
        return dict(row)

    def get_instructions(self, project_id: str) -> str:
        return str(self.get_profile(project_id)["instructions"] or "")

    def update_profile(
        self,
        project_id: str,
        *,
        instructions: str | None = None,
        context_budget_tokens: int | None = None,
    ) -> dict[str, Any]:
        profile = self.get_profile(project_id)
        budget = int(context_budget_tokens or profile["context_budget_tokens"])
        if not 1_024 <= budget <= 262_144:
            raise ProjectContextError("context_budget_tokens must be between 1,024 and 262,144")
        updated_instructions = profile["instructions"] if instructions is None else instructions
        quotas = component_quotas(budget)
        if estimate_tokens(updated_instructions) > quotas["project_instructions"]:
            raise ContextBudgetError(
                "Project instructions exceed their 25 percent context allocation"
            )
        with self._connect() as connection:
            brain = connection.execute(
                "SELECT pinned_text, active_text, compact_threshold FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if brain:
                protected_tokens = estimate_tokens(
                    f"{brain['pinned_text']}\n{brain['active_text']}"
                )
                brain_allowance = quotas["brain"] + max(
                    0,
                    quotas["project_instructions"]
                    - estimate_tokens(updated_instructions),
                )
                if protected_tokens > brain_allowance:
                    raise ContextBudgetError(
                        "The smaller budget cannot fit protected BRAIN content"
                    )
                if brain["compact_threshold"] > brain_allowance:
                    raise ContextBudgetError(
                        "The smaller budget is below the current BRAIN compaction threshold"
                    )
            connection.execute(
                """
                UPDATE project_profiles
                SET instructions = ?, context_budget_tokens = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (updated_instructions, budget, _timestamp(), project_id),
            )
        return self.get_profile(project_id)

    @staticmethod
    def _brain_snapshot(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "pinned_text": row["pinned_text"],
            "active_text": row["active_text"],
            "recent_text": row["recent_text"],
            "compact_threshold": row["compact_threshold"],
            "token_count": row["token_count"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
            "last_compacted_at": row["last_compacted_at"],
            "deleted_at": row["deleted_at"],
        }

    def _brain_response(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = self._brain_snapshot(row)
        value["project_id"] = row["project_id"]
        value["should_compact"] = (
            not value["deleted_at"]
            and value["token_count"] >= value["compact_threshold"]
        )
        return value

    def get_brain(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if not row:
            raise ProjectContextError(f"BRAIN for '{project_id}' was not initialized")
        return self._brain_response(row)

    def update_brain(
        self,
        project_id: str,
        *,
        pinned_text: str | None = None,
        active_text: str | None = None,
        recent_text: str | None = None,
        compact_threshold: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self._lock_for(project_id), self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if not row:
                raise ProjectContextError(f"BRAIN for '{project_id}' was not initialized")
            if expected_revision is not None and expected_revision != row["revision"]:
                raise RevisionConflictError(
                    f"BRAIN changed from revision {expected_revision} to {row['revision']}"
                )
            pinned = row["pinned_text"] if pinned_text is None else pinned_text
            active = row["active_text"] if active_text is None else active_text
            recent = row["recent_text"] if recent_text is None else recent_text
            threshold = row["compact_threshold"] if compact_threshold is None else int(compact_threshold)
            if not 128 <= threshold <= 262_144:
                raise ProjectContextError("compact_threshold must be between 128 and 262,144")
            token_count = estimate_tokens("\n".join((pinned, active, recent)))
            revision = int(row["revision"]) + 1
            now = _timestamp()
            connection.execute(
                """
                UPDATE brain_states
                SET pinned_text = ?, active_text = ?, recent_text = ?, compact_threshold = ?,
                    token_count = ?, revision = ?, updated_at = ?, deleted_at = NULL
                WHERE project_id = ?
                """,
                (pinned, active, recent, threshold, token_count, revision, now, project_id),
            )
            updated = connection.execute(
                "SELECT * FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO brain_revisions
                    (project_id, revision, snapshot_json, reason, created_at)
                VALUES (?, ?, ?, 'edited', ?)
                """,
                (project_id, revision, json.dumps(self._brain_snapshot(updated)), now),
            )
        return self._brain_response(updated)

    @staticmethod
    def _compact_recent(recent_text: str, token_limit: int) -> str:
        seen: set[str] = set()
        retained: list[str] = []
        for raw_line in str(recent_text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            normalized = re.sub(r"\s+", " ", line).casefold()
            if normalized in seen or normalized.startswith(DROP_RECENT_PREFIXES):
                continue
            seen.add(normalized)
            retained.append(line)
        if not retained:
            return ""
        summary = "RECENT CONTEXT, COMPACTED\n" + "\n".join(f"- {line}" for line in retained)
        return _bounded_text(summary, token_limit, keep_tail=True)

    def compact_brain(
        self,
        project_id: str,
        *,
        force: bool = False,
        reason: str = "threshold",
    ) -> dict[str, Any]:
        with self._lock_for(project_id), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if not row:
                raise ProjectContextError(f"BRAIN for '{project_id}' was not initialized")
            if row["deleted_at"]:
                raise ProjectContextError("Deleted BRAIN content cannot be compacted")
            if not force and row["token_count"] < row["compact_threshold"]:
                response = self._brain_response(row)
                response["compacted"] = False
                return response

            protected_tokens = estimate_tokens(row["pinned_text"]) + estimate_tokens(row["active_text"])
            available_recent = max(0, int(row["compact_threshold"]) - protected_tokens - 48)
            compacted_recent = self._compact_recent(row["recent_text"], available_recent)
            token_count = estimate_tokens(
                "\n".join((row["pinned_text"], row["active_text"], compacted_recent))
            )
            revision = int(row["revision"]) + 1
            now = _timestamp()
            updated_count = connection.execute(
                """
                UPDATE brain_states
                SET recent_text = ?, token_count = ?, revision = ?, updated_at = ?,
                    last_compacted_at = ?
                WHERE project_id = ? AND revision = ?
                """,
                (
                    compacted_recent,
                    token_count,
                    revision,
                    now,
                    now,
                    project_id,
                    row["revision"],
                ),
            ).rowcount
            if updated_count != 1:
                raise RevisionConflictError("BRAIN changed while compaction was running")
            updated = connection.execute(
                "SELECT * FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO brain_revisions
                    (project_id, revision, snapshot_json, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, revision, json.dumps(self._brain_snapshot(updated)), reason, now),
            )
        response = self._brain_response(updated)
        response["compacted"] = True
        return response

    def list_brain_revisions(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision, reason, created_at, snapshot_json
                FROM brain_revisions
                WHERE project_id = ?
                ORDER BY revision DESC
                """,
                (project_id,),
            ).fetchall()
        return [
            {
                "revision": row["revision"],
                "reason": row["reason"],
                "created_at": row["created_at"],
                "token_count": json.loads(row["snapshot_json"]).get("token_count", 0),
            }
            for row in rows
        ]

    def restore_brain(self, project_id: str, revision: int) -> dict[str, Any]:
        with self._lock_for(project_id), self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT snapshot_json FROM brain_revisions WHERE project_id = ? AND revision = ?",
                (project_id, int(revision)),
            ).fetchone()
            if not current or not target:
                raise ProjectContextError(f"BRAIN revision {revision} was not found")
            snapshot = json.loads(target["snapshot_json"])
            new_revision = int(current["revision"]) + 1
            now = _timestamp()
            connection.execute(
                """
                UPDATE brain_states
                SET pinned_text = ?, active_text = ?, recent_text = ?, compact_threshold = ?,
                    token_count = ?, revision = ?, updated_at = ?, last_compacted_at = ?,
                    deleted_at = NULL
                WHERE project_id = ?
                """,
                (
                    snapshot.get("pinned_text", ""),
                    snapshot.get("active_text", ""),
                    snapshot.get("recent_text", ""),
                    snapshot.get("compact_threshold", self.default_brain_threshold),
                    snapshot.get("token_count", 0),
                    new_revision,
                    now,
                    snapshot.get("last_compacted_at"),
                    project_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO brain_revisions
                    (project_id, revision, snapshot_json, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    new_revision,
                    json.dumps(self._brain_snapshot(updated)),
                    f"restored revision {revision}",
                    now,
                ),
            )
        return self._brain_response(updated)

    def soft_delete_brain(self, project_id: str) -> dict[str, Any]:
        with self._lock_for(project_id), self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if not row:
                raise ProjectContextError(f"BRAIN for '{project_id}' was not initialized")
            revision = int(row["revision"]) + 1
            now = _timestamp()
            connection.execute(
                "UPDATE brain_states SET revision = ?, updated_at = ?, deleted_at = ? WHERE project_id = ?",
                (revision, now, now, project_id),
            )
            updated = connection.execute(
                "SELECT * FROM brain_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO brain_revisions
                    (project_id, revision, snapshot_json, reason, created_at)
                VALUES (?, ?, ?, 'soft deleted', ?)
                """,
                (project_id, revision, json.dumps(self._brain_snapshot(updated)), now),
            )
        return self._brain_response(updated)

    def purge_expired_brains(self, recovery_days: int) -> list[str]:
        """Permanently clear soft-deleted content after its recovery window."""
        cutoff = (datetime.now() - timedelta(days=max(1, recovery_days))).isoformat()
        purged: list[str] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT project_id FROM brain_states WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                project_id = row["project_id"]
                now = _timestamp()
                connection.execute(
                    "DELETE FROM brain_revisions WHERE project_id = ?",
                    (project_id,),
                )
                connection.execute(
                    """
                    UPDATE brain_states
                    SET pinned_text = '', active_text = '', recent_text = '', token_count = 0,
                        revision = 1, updated_at = ?, last_compacted_at = NULL, deleted_at = NULL
                    WHERE project_id = ?
                    """,
                    (now, project_id),
                )
                reset = connection.execute(
                    "SELECT * FROM brain_states WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO brain_revisions
                        (project_id, revision, snapshot_json, reason, created_at)
                    VALUES (?, 1, ?, 'recovery window expired', ?)
                    """,
                    (project_id, json.dumps(self._brain_snapshot(reset)), now),
                )
                purged.append(project_id)
        return purged

    def _is_text_upload(self, display_name: str, media_type: str) -> bool:
        return media_type.startswith("text/") or Path(display_name).suffix.lower() in TEXT_EXTENSIONS

    @staticmethod
    def _chunks(text: str, size: int = 6_000) -> list[str]:
        if not text:
            return []
        return [text[index : index + size] for index in range(0, len(text), size)]

    def add_file(
        self,
        project_id: str,
        *,
        display_name: str,
        media_type: str | None,
        content: bytes,
    ) -> dict[str, Any]:
        safe_name = Path(display_name or "reference").name.replace("\x00", "").strip()
        if not safe_name:
            raise ProjectContextError("Upload filename is required")
        detected_type = media_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        extracted = ""
        status = "stored_unindexed"
        if self._is_text_upload(safe_name, detected_type):
            try:
                extracted = content.decode("utf-8")
                status = "indexed" if extracted.strip() else "empty"
            except UnicodeDecodeError:
                status = "decode_failed"
        file_id = f"file_{uuid.uuid4().hex}"
        suffix = Path(safe_name).suffix[:16]
        stored_name = f"{file_id}{suffix}"
        project_root = self.uploads_root / project_id
        project_root.mkdir(parents=True, exist_ok=True)
        destination = project_root / stored_name
        temporary = destination.with_suffix(f"{destination.suffix}.tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        now = _timestamp()
        sha256 = hashlib.sha256(content).hexdigest()
        chunks = self._chunks(extracted)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO project_files
                        (file_id, project_id, display_name, stored_name, media_type,
                         size_bytes, sha256, status, extracted_text, token_count, attached,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        file_id,
                        project_id,
                        safe_name,
                        stored_name,
                        detected_type,
                        len(content),
                        sha256,
                        status,
                        extracted,
                        estimate_tokens(extracted),
                        now,
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO project_file_chunks (file_id, chunk_index, content, token_count)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (file_id, index, chunk, estimate_tokens(chunk))
                        for index, chunk in enumerate(chunks)
                    ],
                )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return self.get_file(project_id, file_id)

    def _file_response(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "file_id": row["file_id"],
            "project_id": row["project_id"],
            "display_name": row["display_name"],
            "media_type": row["media_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "status": row["status"],
            "token_count": row["token_count"],
            "attached": bool(row["attached"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_file(self, project_id: str, file_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_files WHERE project_id = ? AND file_id = ?",
                (project_id, file_id),
            ).fetchone()
        if not row:
            raise ProjectContextError(f"Project file '{file_id}' was not found")
        return self._file_response(row)

    def list_files(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM project_files WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [self._file_response(row) for row in rows]

    def delete_file(self, project_id: str, file_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT stored_name FROM project_files WHERE project_id = ? AND file_id = ?",
                (project_id, file_id),
            ).fetchone()
            if not row:
                raise ProjectContextError(f"Project file '{file_id}' was not found")
            connection.execute(
                "DELETE FROM project_files WHERE project_id = ? AND file_id = ?",
                (project_id, file_id),
            )
        (self.uploads_root / project_id / row["stored_name"]).unlink(missing_ok=True)

    def set_file_attached(
        self,
        project_id: str,
        file_id: str,
        attached: bool,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE project_files SET attached = ?, updated_at = ?
                WHERE project_id = ? AND file_id = ?
                """,
                (int(attached), _timestamp(), project_id, file_id),
            ).rowcount
        if not updated:
            raise ProjectContextError(f"Project file '{file_id}' was not found")
        return self.get_file(project_id, file_id)

    def reindex_file(self, project_id: str, file_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_files WHERE project_id = ? AND file_id = ?",
                (project_id, file_id),
            ).fetchone()
            if not row:
                raise ProjectContextError(f"Project file '{file_id}' was not found")
        source = self.uploads_root / project_id / row["stored_name"]
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise ProjectContextError(f"Stored project file cannot be read: {exc}") from exc
        extracted = ""
        status = "stored_unindexed"
        if self._is_text_upload(row["display_name"], row["media_type"]):
            try:
                extracted = content.decode("utf-8")
                status = "indexed" if extracted.strip() else "empty"
            except UnicodeDecodeError:
                status = "decode_failed"
        chunks = self._chunks(extracted)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE project_files
                SET status = ?, extracted_text = ?, token_count = ?, updated_at = ?
                WHERE project_id = ? AND file_id = ?
                """,
                (
                    status,
                    extracted,
                    estimate_tokens(extracted),
                    _timestamp(),
                    project_id,
                    file_id,
                ),
            )
            connection.execute(
                "DELETE FROM project_file_chunks WHERE file_id = ?",
                (file_id,),
            )
            connection.executemany(
                """
                INSERT INTO project_file_chunks (file_id, chunk_index, content, token_count)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (file_id, index, chunk, estimate_tokens(chunk))
                    for index, chunk in enumerate(chunks)
                ],
            )
        return self.get_file(project_id, file_id)

    def add_artifact(
        self,
        project_id: str,
        *,
        title: str,
        body: str,
        kind: str = "assistant_output",
        conversation_id: str | None = None,
        source_message_index: int | None = None,
        pinned: bool = False,
    ) -> dict[str, Any]:
        now = _timestamp()
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO project_artifacts
                    (artifact_id, project_id, conversation_id, source_message_index,
                     title, kind, body, token_count, pinned, archived, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    conversation_id,
                    source_message_index,
                    title.strip() or "Assistant output",
                    kind,
                    body,
                    estimate_tokens(body),
                    int(pinned),
                    now,
                    now,
                ),
            )
            if conversation_id is not None and source_message_index is not None:
                row = connection.execute(
                    """
                    SELECT * FROM project_artifacts
                    WHERE project_id = ? AND conversation_id = ? AND source_message_index = ?
                    """,
                    (project_id, conversation_id, source_message_index),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM project_artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
        return self._artifact_response(row, include_body=True)

    @staticmethod
    def _artifact_response(
        row: sqlite3.Row | dict[str, Any],
        *,
        include_body: bool = False,
    ) -> dict[str, Any]:
        body = str(row["body"] or "")
        result = {
            "artifact_id": row["artifact_id"],
            "project_id": row["project_id"],
            "conversation_id": row["conversation_id"],
            "source_message_index": row["source_message_index"],
            "title": row["title"],
            "kind": row["kind"],
            "token_count": row["token_count"],
            "pinned": bool(row["pinned"]),
            "archived": bool(row["archived"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "preview": body[:240],
        }
        if include_body:
            result["body"] = body
        return result

    def list_artifacts(
        self,
        project_id: str,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        where = "project_id = ?" if include_archived else "project_id = ? AND archived = 0"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM project_artifacts WHERE {where} ORDER BY pinned DESC, created_at DESC",
                (project_id,),
            ).fetchall()
        return [self._artifact_response(row) for row in rows]

    def get_artifact(self, project_id: str, artifact_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_artifacts WHERE project_id = ? AND artifact_id = ?",
                (project_id, artifact_id),
            ).fetchone()
        if not row:
            raise ProjectContextError(f"Project artifact '{artifact_id}' was not found")
        return self._artifact_response(row, include_body=True)

    def update_artifact(
        self,
        project_id: str,
        artifact_id: str,
        *,
        pinned: bool | None = None,
        archived: bool | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_artifact(project_id, artifact_id)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE project_artifacts
                SET pinned = ?, archived = ?, title = ?, updated_at = ?
                WHERE project_id = ? AND artifact_id = ?
                """,
                (
                    int(existing["pinned"] if pinned is None else pinned),
                    int(existing["archived"] if archived is None else archived),
                    existing["title"] if title is None else (title.strip() or existing["title"]),
                    _timestamp(),
                    project_id,
                    artifact_id,
                ),
            )
        return self.get_artifact(project_id, artifact_id)

    def delete_artifact(self, project_id: str, artifact_id: str) -> None:
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM project_artifacts WHERE project_id = ? AND artifact_id = ?",
                (project_id, artifact_id),
            ).rowcount
        if not deleted:
            raise ProjectContextError(f"Project artifact '{artifact_id}' was not found")

    def _brain_context(
        self, project_id: str, token_limit: int,
        brain_snapshot: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        brain = brain_snapshot if brain_snapshot is not None else self.get_brain(project_id)
        if brain["deleted_at"]:
            return "", 0
        pinned = brain["pinned_text"]
        active = brain["active_text"]
        protected = ""
        if pinned.strip():
            protected += f"PINNED FACTS AND DECISIONS, VERBATIM\n{pinned.strip()}"
        if active.strip():
            protected += ("\n\n" if protected else "") + f"ACTIVE GOALS, CONTRACTS, AND RISKS\n{active.strip()}"
        protected_tokens = estimate_tokens(protected)
        if protected_tokens > token_limit:
            raise ContextBudgetError(
                "Pinned and active BRAIN content exceed the protected BRAIN allocation"
            )
        recent_limit = max(0, token_limit - protected_tokens - 12)
        recent = _bounded_text(brain["recent_text"].strip(), recent_limit, keep_tail=True)
        content = protected
        if recent:
            content += ("\n\n" if content else "") + f"RECENT PROJECT CONTEXT\n{recent}"
        return content, estimate_tokens(content)

    def _file_context(self, project_id: str, query: str, token_limit: int) -> tuple[str, int]:
        terms = _query_terms(query)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.content, c.token_count, c.chunk_index, f.display_name, f.created_at
                FROM project_file_chunks c
                JOIN project_files f ON f.file_id = c.file_id
                WHERE f.project_id = ? AND f.status = 'indexed' AND f.attached = 1
                """,
                (project_id,),
            ).fetchall()
        ranked = sorted(
            rows,
            key=lambda row: (_rank_text(row["content"], terms), row["created_at"]),
            reverse=True,
        )
        blocks: list[str] = []
        remaining = token_limit
        for row in ranked:
            label = f"[File: {row['display_name']}#{row['chunk_index'] + 1}]\n"
            label_tokens = estimate_tokens(label)
            if remaining <= label_tokens:
                break
            body = _bounded_text(row["content"], remaining - label_tokens)
            if not body:
                continue
            blocks.append(f"{label}{body}")
            remaining -= estimate_tokens(blocks[-1])
            if remaining <= 0:
                break
        content = "\n\n".join(blocks)
        return content, estimate_tokens(content)

    def _artifact_context(self, project_id: str, query: str, token_limit: int) -> tuple[str, int]:
        terms = _query_terms(query)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM project_artifacts WHERE project_id = ? AND archived = 0",
                (project_id,),
            ).fetchall()
        ranked = sorted(
            rows,
            key=lambda row: (
                int(row["pinned"]),
                _rank_text(f"{row['title']}\n{row['body']}", terms),
                row["created_at"],
            ),
            reverse=True,
        )
        blocks: list[str] = []
        remaining = token_limit
        for row in ranked:
            label = f"[Artifact: {row['title']}]\n"
            label_tokens = estimate_tokens(label)
            if remaining <= label_tokens:
                break
            body = _bounded_text(row["body"], remaining - label_tokens)
            if not body:
                continue
            blocks.append(f"{label}{body}")
            remaining -= estimate_tokens(blocks[-1])
            if remaining <= 0:
                break
        content = "\n\n".join(blocks)
        return content, estimate_tokens(content)

    def build_context_messages(
        self,
        project_id: str,
        *,
        query: str,
        available_tokens: int | None = None,
        brain_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile = self.get_profile(project_id)
        configured_budget = int(profile["context_budget_tokens"])
        total_budget = min(configured_budget, int(available_tokens or configured_budget))
        if total_budget < 1_024:
            raise ContextBudgetError("The selected model has too little room for project context")
        quotas = component_quotas(total_budget)
        instruction_tokens = estimate_tokens(profile["instructions"])
        if instruction_tokens > quotas["project_instructions"]:
            raise ContextBudgetError(
                "Project instructions exceed their 25 percent context allocation for this request"
            )
        brain_limit = quotas["brain"] + max(
            0,
            quotas["project_instructions"] - instruction_tokens,
        )
        brain, brain_tokens = self._brain_context(project_id, brain_limit, brain_snapshot)
        file_limit = quotas["file_context"] + max(0, brain_limit - brain_tokens)
        files, file_tokens = self._file_context(project_id, query, file_limit)
        artifact_limit = quotas["artifact_history"] + max(
            0,
            file_limit - file_tokens,
        )
        artifacts, artifact_tokens = self._artifact_context(
            project_id, query, artifact_limit
        )
        messages = []
        if brain:
            messages.append({"role": "system", "content": f"BRAIN PROJECT CONTEXT\n{brain}"})
        if files:
            messages.append({"role": "system", "content": f"PROJECT FILE CONTEXT\n{files}"})
        if artifacts:
            messages.append({"role": "system", "content": f"PROJECT ARTIFACT HISTORY\n{artifacts}"})
        return {
            "messages": messages,
            "budget": {
                "configured_total": configured_budget,
                "request_total": total_budget,
                "quotas": quotas,
                "available_limits": {
                    "project_instructions": quotas["project_instructions"],
                    "brain": brain_limit,
                    "file_context": file_limit,
                    "artifact_history": artifact_limit,
                },
                "usage": {
                    "project_instructions": instruction_tokens,
                    "brain": brain_tokens,
                    "file_context": file_tokens,
                    "artifact_history": artifact_tokens,
                },
                "unused_tokens": max(0, artifact_limit - artifact_tokens),
            },
        }

    def capture_run_context(
        self, project_id: str, *, query: str,
        available_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Bind one assembled context to the exact BRAIN revision read at run start."""
        brain = self.get_brain(project_id)
        assembled = self.build_context_messages(
            project_id, query=query, available_tokens=available_tokens,
            brain_snapshot=brain,
        )
        identity = {
            "project_id": project_id,
            "pinned_text": brain["pinned_text"],
            "active_text": brain["active_text"],
            "recent_text": brain["recent_text"],
            "deleted_at": brain["deleted_at"],
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return {
            **assembled,
            "brain_revision": brain["revision"],
            "brain_digest": digest,
        }

    def homepage(self, project_id: str) -> dict[str, Any]:
        profile = self.get_profile(project_id)
        budget = int(profile["context_budget_tokens"])
        quotas = component_quotas(budget)
        brain = self.get_brain(project_id)
        files = self.list_files(project_id)
        artifacts = self.list_artifacts(project_id)
        return {
            "project_id": project_id,
            "context_budget": {
                "total": budget,
                "quotas": quotas,
                "usage": {
                    "project_instructions": estimate_tokens(profile["instructions"]),
                    "brain": brain["token_count"],
                    "file_context": sum(
                        item["token_count"] for item in files if item["attached"]
                    ),
                    "artifact_history": sum(item["token_count"] for item in artifacts),
                },
            },
            "components": {
                "project_instructions": {
                    "content": profile["instructions"],
                    "token_count": estimate_tokens(profile["instructions"]),
                    "updated_at": profile["updated_at"],
                },
                "file_context_uploads": files,
                "artifact_history": artifacts,
                "brain": brain,
            },
        }
