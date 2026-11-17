"""Aspect-ratio, style and negative-prompt presets for DaveLLM image generation.

Aspect presets are computed, not stored: each ratio is snapped to multiples of
64 and capped by the hardware limits, at two pixel budgets ("standard", the
fast 768x768-class budget, and "max", the validated draft limit).

Style and negative presets are user data in ``<DAVE_DATA_DIR>/image_presets.json``
on the serving host. A style preset appends its text to the prompt sent to the
model; it is the user's own explicit choice, not a rewrite, so it does not
trigger the rewrite review. The manifest records which preset was used.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Optional

ASPECT_RATIOS = (
    ("1:1", 1, 1), ("4:3", 4, 3), ("3:4", 3, 4), ("3:2", 3, 2), ("2:3", 2, 3),
    ("16:9", 16, 9), ("9:16", 9, 16), ("21:9", 21, 9), ("5:4", 5, 4), ("4:5", 4, 5),
)
STANDARD_PIXELS = 768 * 768
MAX_PRESETS = 50
PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

DEFAULT_STYLES = [
    {"id": "photo", "name": "Photo", "text": "photograph, natural light, sharp focus, realistic detail"},
    {"id": "cinematic", "name": "Cinematic", "text": "cinematic still, dramatic lighting, shallow depth of field, film grain"},
    {"id": "watercolor", "name": "Watercolor", "text": "watercolor painting, soft washes, paper texture"},
    {"id": "line-art", "name": "Line art", "text": "clean black ink line art, white background, minimal shading"},
    {"id": "product", "name": "Product shot", "text": "studio product photo, seamless background, softbox lighting"},
    {"id": "concept", "name": "Concept art", "text": "digital concept art, painterly, atmospheric perspective"},
]
DEFAULT_NEGATIVES = [
    {"id": "clean", "name": "Clean", "text": "text, watermark, logo, signature"},
    {"id": "quality", "name": "Quality", "text": "blurry, low quality, jpeg artifacts, oversaturated"},
    {"id": "anatomy", "name": "Anatomy", "text": "extra fingers, deformed hands, extra limbs"},
]


def snap64(value: float) -> int:
    return max(64, int(value) // 64 * 64)


def size_for_ratio(rw: int, rh: int, budget: int, limit: int) -> tuple[int, int]:
    """Largest multiple-of-64 size near ``rw:rh`` with ``w*h <= min(budget, limit)``."""
    cap = min(budget, limit)
    w = snap64((cap * rw / rh) ** 0.5)
    h = snap64((cap * rh / rw) ** 0.5)
    while w * h > cap and (w > 64 or h > 64):
        if w >= h and w > 64:
            w -= 64
        else:
            h -= 64
    return w, h


def aspect_presets(max_pixels: int) -> list[dict[str, Any]]:
    out = []
    for label, rw, rh in ASPECT_RATIOS:
        for tier, budget in (("standard", STANDARD_PIXELS), ("max", max_pixels)):
            w, h = size_for_ratio(rw, rh, budget, max_pixels)
            if any(p["ratio"] == label and p["width"] == w and p["height"] == h for p in out):
                continue
            out.append({"id": f"{label}-{tier}", "ratio": label, "tier": tier, "width": w, "height": h})
    return out


def _clean_list(items: Any, kind: str) -> list[dict[str, str]]:
    if not isinstance(items, list) or len(items) > MAX_PRESETS:
        raise ValueError(f"{kind} must be a list of at most {MAX_PRESETS} presets")
    seen, out = set(), []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"each {kind} preset must be an object")
        name = " ".join(str(item.get("name", "")).split())[:60]
        text = " ".join(str(item.get("text", "")).split())[:500]
        pid = str(item.get("id") or "").strip().lower() or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        if not pid or not PRESET_ID_RE.match(pid):
            pid = f"p-{secrets.token_hex(3)}"
        if not name or not text:
            raise ValueError(f"each {kind} preset needs a name and text")
        if pid in seen:
            raise ValueError(f"duplicate {kind} preset id {pid!r}")
        seen.add(pid)
        out.append({"id": pid, "name": name, "text": text})
    return out


class PresetStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, list[dict[str, str]]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {"styles": _clean_list(data.get("styles", []), "styles"),
                    "negatives": _clean_list(data.get("negatives", []), "negatives")}
        except (OSError, ValueError, AttributeError):
            return {"styles": [dict(p) for p in DEFAULT_STYLES], "negatives": [dict(p) for p in DEFAULT_NEGATIVES]}

    def save(self, *, styles: Optional[list] = None, negatives: Optional[list] = None) -> dict[str, list]:
        with self._lock:
            current = self.load()
            if styles is not None:
                current["styles"] = _clean_list(styles, "styles")
            if negatives is not None:
                current["negatives"] = _clean_list(negatives, "negatives")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
            tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            return current

    def reset(self) -> dict[str, list]:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        return self.load()

    def style(self, preset_id: str) -> Optional[dict[str, str]]:
        return next((p for p in self.load()["styles"] if p["id"] == preset_id), None)


def apply_style(prompt: str, style: Optional[dict[str, str]]) -> str:
    if not style:
        return prompt
    return f"{prompt.rstrip().rstrip(',')}, {style['text']}"
