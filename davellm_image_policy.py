"""Review, approval and hardware-limit rules for DaveLLM image generation.

Implements the PLAN | DaveLLM | Image Harness Design Questions selections:

* Prompt behavior (1): ``suggest`` | ``review`` (default) | ``auto``.
* Approvals (6): ``always`` | ``material`` (default) | ``none``.
* Hardware limits (7B): never run settings outside the validated envelope;
  propose a reduced preset and wait for an explicit ``accept_reduction``.

Mode interactions are resolved conservatively: when two settings disagree,
the stricter one wins, and hardware reductions always need their own approval.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

PROMPT_BEHAVIORS = ("suggest", "review", "auto")
APPROVAL_MODES = ("always", "material", "none")
DEFAULT_PROMPT_BEHAVIOR = "review"
DEFAULT_APPROVAL_MODE = "material"

_SPACE = re.compile(r"\s+")
_TRAILING = re.compile(r"[\s.,;:!]+$")


def normalize_prompt(text: str) -> str:
    """Deterministic formatting only: case, whitespace and trailing punctuation."""
    return _TRAILING.sub("", _SPACE.sub(" ", (text or "").strip())).lower()


def is_material_change(original: str, structured: Optional[str]) -> bool:
    """True unless ``structured`` is a formatting-only variant of ``original``.

    The design rule is "when uncertain, show the change for review", so any
    wording difference beyond case, whitespace and trailing punctuation counts.
    """
    if structured is None:
        return False
    return normalize_prompt(original) != normalize_prompt(structured)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str = "ok"
    message: str = ""
    prompt_for_model: str = ""
    material_change: bool = False

    def as_detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "material_change": self.material_change}


def decide(
    *,
    original: str,
    structured: Optional[str],
    prompt_behavior: str = DEFAULT_PROMPT_BEHAVIOR,
    approval_mode: str = DEFAULT_APPROVAL_MODE,
    approved: bool = False,
) -> Decision:
    if prompt_behavior not in PROMPT_BEHAVIORS:
        raise ValueError(f"unknown prompt behavior {prompt_behavior!r}")
    if approval_mode not in APPROVAL_MODES:
        raise ValueError(f"unknown approval mode {approval_mode!r}")
    material = is_material_change(original, structured)
    model_prompt = structured if structured is not None else original

    if prompt_behavior == "suggest" and material:
        return Decision(
            False, "suggest_only",
            "Prompt behavior is 'suggest': the image model only receives your original wording.",
            original, material,
        )
    if approval_mode == "always" and not approved:
        return Decision(False, "review_required", "Approval mode is 'always': review the final prompt first.",
                        model_prompt, material)
    # A material rewrite skips review only when the user chose both automatic
    # rewriting and no approvals. Every other combination keeps the review.
    auto_ok = prompt_behavior == "auto" and approval_mode == "none"
    if material and not approved and not auto_ok:
        return Decision(False, "review_required", "The prompt changed materially: review it before generating.",
                        model_prompt, material)
    return Decision(True, "ok", "", model_prompt, material)


# -- Hardware envelope ----------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    max_pixels: int
    max_steps: int
    max_quality_pixels: int
    max_quality_steps: int

    def as_dict(self) -> dict[str, int]:
        return {
            "max_pixels": self.max_pixels,
            "max_steps": self.max_steps,
            "max_quality_pixels": self.max_quality_pixels,
            "max_quality_steps": self.max_quality_steps,
        }


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def load_limits() -> Limits:
    # Defaults are the envelope measured on Walter (RTX 3070 Laptop, 8 GB, SDXL Turbo,
    # ComfyUI --lowvram); see benchmarks/imagegen-2026-11-17/RESULTS.md.
    return Limits(
        max_pixels=_env_int("DAVE_IMAGE_MAX_PIXELS", 1024 * 1024),
        max_steps=_env_int("DAVE_IMAGE_MAX_STEPS", 8),
        max_quality_pixels=_env_int("DAVE_IMAGE_MAX_QUALITY_PIXELS", 1536 * 1536),
        max_quality_steps=_env_int("DAVE_IMAGE_MAX_QUALITY_STEPS", 8),
    )


def _fit(width: int, height: int, max_pixels: int) -> tuple[int, int]:
    if width * height <= max_pixels:
        return width, height
    scale = (max_pixels / float(width * height)) ** 0.5
    w = max(64, int(width * scale) // 64 * 64)
    h = max(64, int(height * scale) // 64 * 64)
    return w, h


def hardware_check(
    *, width: int, height: int, steps: int, limits: Limits,
    quality_size: Optional[tuple[int, int]] = None, quality_steps: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Return ``None`` when inside the envelope, else a proposed reduced preset."""
    over = []
    proposal: dict[str, Any] = {"width": width, "height": height, "steps": steps}
    if width * height > limits.max_pixels:
        over.append("resolution")
        proposal["width"], proposal["height"] = _fit(width, height, limits.max_pixels)
    if steps > limits.max_steps:
        over.append("steps")
        proposal["steps"] = limits.max_steps
    if quality_size is not None:
        qw, qh = quality_size
        if qw * qh > limits.max_quality_pixels:
            over.append("quality_resolution")
            # Shrink the scale so the refined output fits the envelope.
            base_w, base_h = proposal["width"], proposal["height"]
            proposal["quality_scale"] = round(max(1.0, (limits.max_quality_pixels / float(base_w * base_h)) ** 0.5 - 0.01), 2)
        if quality_steps is not None and quality_steps > limits.max_quality_steps:
            over.append("quality_steps")
            proposal["quality_steps"] = limits.max_quality_steps
    if not over:
        return None
    return {
        "code": "hardware_limit",
        "message": "That request is above the settings validated on this machine. "
                   "Approve the reduced preset to continue; nothing has been generated.",
        "exceeded": over,
        "proposed": proposal,
        "limits": limits.as_dict(),
    }
