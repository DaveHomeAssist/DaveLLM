"""Local-disk store for DaveLLM generated images and their provenance.

Every saved image is two files under one directory on the serving host:
``<id>.png`` and ``<id>.json`` (the manifest). Nothing is uploaded anywhere.
The directory defaults to ``<DAVE_DATA_DIR>/images`` and can be moved with
``DAVE_IMAGE_DIR``. ``DAVE_IMAGE_RETENTION_DAYS`` (default 90, ``0`` keeps
images until deleted) bounds how long prompts and images stay on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_RETENTION_DAYS = 90
IMAGE_ID_RE = re.compile(r"^img-\d{8}T\d{6}Z-[0-9a-f]{8}$")
MANIFEST_SCHEMA = "davellm.image.manifest/1"


class ImageNotFound(KeyError):
    pass


def parse_retention_days(raw: Optional[str]) -> int:
    if raw is None or raw.strip() == "":
        return DEFAULT_RETENTION_DAYS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(0, value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ImageStore:
    def __init__(self, root: Path, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.root = Path(root).expanduser()
        self.retention_days = max(0, int(retention_days))
        self._lock = threading.Lock()

    # -- paths -------------------------------------------------------------
    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(self.root, 0o700)
            except OSError:
                pass

    def _paths(self, image_id: str) -> tuple[Path, Path]:
        if not IMAGE_ID_RE.match(image_id or ""):
            raise ImageNotFound(image_id)
        return self.root / f"{image_id}.png", self.root / f"{image_id}.json"

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    # -- operations ----------------------------------------------------------
    def save(self, png: bytes, manifest: dict[str, Any], *, now: Optional[datetime] = None) -> dict[str, Any]:
        moment = now or _utc_now()
        image_id = f"img-{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        record = {
            "schema": MANIFEST_SCHEMA,
            **manifest,
            "id": image_id,
            "created_utc": _iso(moment),
            "png_sha256": hashlib.sha256(png).hexdigest(),
            "bytes": len(png),
        }
        with self._lock:
            self._ensure_root()
            image_path, manifest_path = self._paths(image_id)
            self._write_atomic(image_path, png)
            self._write_atomic(manifest_path, json.dumps(record, indent=2, sort_keys=True).encode("utf-8"))
        self.prune()
        return record

    def manifest(self, image_id: str) -> dict[str, Any]:
        _image, manifest_path = self._paths(image_id)
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ImageNotFound(image_id) from exc

    def image_path(self, image_id: str) -> Path:
        image_path, _manifest = self._paths(image_id)
        if not image_path.is_file():
            raise ImageNotFound(image_id)
        return image_path

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        items = []
        for path in self.root.glob("img-*.json"):
            if not IMAGE_ID_RE.match(path.stem):
                continue
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        items.sort(key=lambda item: item.get("created_utc", ""), reverse=True)
        return items

    def delete(self, image_id: str) -> bool:
        image_path, manifest_path = self._paths(image_id)
        removed = False
        with self._lock:
            for path in (image_path, manifest_path):
                try:
                    path.unlink()
                    removed = True
                except FileNotFoundError:
                    pass
        if not removed:
            raise ImageNotFound(image_id)
        return True

    def purge(self) -> int:
        if not self.root.is_dir():
            return 0
        count = 0
        with self._lock:
            for path in list(self.root.glob("img-*")):
                if path.suffix in {".png", ".json"} and IMAGE_ID_RE.match(path.stem):
                    try:
                        path.unlink()
                        if path.suffix == ".json":
                            count += 1
                    except FileNotFoundError:
                        pass
        return count

    def prune(self, *, now: Optional[datetime] = None) -> int:
        """Delete images older than the retention window. Returns how many."""
        if self.retention_days <= 0 or not self.root.is_dir():
            return 0
        cutoff = _iso((now or _utc_now()) - timedelta(days=self.retention_days))
        removed = 0
        for record in self.list():
            created = record.get("created_utc", "")
            if created and created < cutoff:
                try:
                    self.delete(record["id"])
                    removed += 1
                except (ImageNotFound, KeyError):
                    pass
        return removed

    def summary(self) -> dict[str, Any]:
        return {"count": len(self.list()), "retention_days": self.retention_days, "location": "local-disk"}
