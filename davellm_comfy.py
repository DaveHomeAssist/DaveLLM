"""Bounded loopback client for DaveLLM image generation through ComfyUI.

The client builds one of two fixed SDXL Turbo workflows:

* ``draft``: a single KSampler pass at the requested size.
* ``quality``: the same draft graph (same seed, same settings) followed by a
  latent upscale and a lower-denoise refinement pass, so the quality pass keeps
  the draft's composition instead of starting a new image.

Output uses ComfyUI's ``PreviewImage`` node, which writes to ComfyUI's temp
folder (cleared when ComfyUI restarts) rather than its permanent output folder.
DaveLLM's own image store (``davellm_image_store``) is the only durable copy,
so DaveLLM's delete and purge controls are authoritative.
"""

from __future__ import annotations

import base64
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

import requests

DRAFT_WORKFLOW_VERSION = "sdxl-turbo-draft-v1"
QUALITY_WORKFLOW_VERSION = "sdxl-turbo-quality-v1"
SUPPORTED_SAMPLERS = ("euler", "euler_ancestral", "dpmpp_2m", "dpmpp_sde")
SUPPORTED_SCHEDULERS = ("simple", "normal", "karras", "sgm_uniform")
_NO_PROXY = {"http": None, "https": None}


class ComfyError(RuntimeError):
    """A safe, user-facing ComfyUI failure."""


@dataclass
class QualityPass:
    scale: float = 1.5
    steps: int = 4
    denoise: float = 0.45


@dataclass
class ImageSpec:
    prompt: str
    negative_prompt: str = ""
    width: int = 768
    height: int = 768
    steps: int = 4
    cfg: float = 1.0
    sampler: str = "euler"
    scheduler: str = "simple"
    seed: Optional[int] = None
    quality: Optional[QualityPass] = field(default=None)

    @property
    def workflow_version(self) -> str:
        return QUALITY_WORKFLOW_VERSION if self.quality else DRAFT_WORKFLOW_VERSION

    def output_size(self) -> tuple[int, int]:
        if not self.quality:
            return self.width, self.height
        return _round64(self.width * self.quality.scale), _round64(self.height * self.quality.scale)

    def settings(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("prompt")
        data.pop("negative_prompt")
        return data


def _round64(value: float) -> int:
    return max(64, int(round(value / 64.0)) * 64)


def new_seed() -> int:
    return random.SystemRandom().randrange(0, 2**63)


def build_workflow(spec: ImageSpec, checkpoint: str) -> dict[str, Any]:
    """Return the ComfyUI API-format graph for ``spec``. ``spec.seed`` must be set."""
    if spec.seed is None:
        raise ValueError("seed must be resolved before building a workflow")
    if spec.sampler not in SUPPORTED_SAMPLERS:
        raise ValueError(f"unsupported sampler {spec.sampler!r}")
    if spec.scheduler not in SUPPORTED_SCHEDULERS:
        raise ValueError(f"unsupported scheduler {spec.scheduler!r}")
    graph: dict[str, Any] = {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": spec.seed, "steps": spec.steps, "cfg": spec.cfg,
            "sampler_name": spec.sampler, "scheduler": spec.scheduler, "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
        }},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {
            "width": spec.width, "height": spec.height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": spec.prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": spec.negative_prompt, "clip": ["4", 1]}},
    }
    samples = ["3", 0]
    if spec.quality:
        out_w, out_h = spec.output_size()
        graph["10"] = {"class_type": "LatentUpscale", "inputs": {
            "samples": ["3", 0], "upscale_method": "nearest-exact",
            "width": out_w, "height": out_h, "crop": "disabled"}}
        graph["11"] = {"class_type": "KSampler", "inputs": {
            "seed": spec.seed, "steps": spec.quality.steps, "cfg": spec.cfg,
            "sampler_name": spec.sampler, "scheduler": spec.scheduler, "denoise": spec.quality.denoise,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["10", 0]}}
        samples = ["11", 0]
    graph["8"] = {"class_type": "VAEDecode", "inputs": {"samples": samples, "vae": ["4", 2]}}
    graph["9"] = {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}}
    return graph


def status(base_url: str, timeout: float = 2.0) -> dict[str, Any]:
    try:
        response = requests.get(f"{base_url}/system_stats", timeout=timeout, proxies=_NO_PROXY)
        response.raise_for_status()
        stats = response.json() if response.content else {}
        system = stats.get("system", {}) if isinstance(stats, dict) else {}
        devices = stats.get("devices", []) if isinstance(stats, dict) else []
        return {
            "available": True,
            "comfyui_version": system.get("comfyui_version"),
            "device": (devices[0].get("name") if devices and isinstance(devices[0], dict) else None),
        }
    except (requests.RequestException, ValueError):
        return {"available": False}


def generate(
    base_url: str,
    checkpoint: str,
    spec: ImageSpec,
    *,
    timeout: float = 240.0,
    poll_interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run ``spec`` and return PNG bytes plus timing. Raises ``ComfyError``."""
    if spec.seed is None:
        spec.seed = new_seed()
    started = time.monotonic()
    try:
        queued = requests.post(
            f"{base_url}/prompt",
            json={"prompt": build_workflow(spec, checkpoint)},
            timeout=10,
            proxies=_NO_PROXY,
        )
        queued.raise_for_status()
        prompt_id = queued.json()["prompt_id"]
        deadline = started + timeout
        image_meta = None
        while time.monotonic() < deadline:
            history = requests.get(f"{base_url}/history/{prompt_id}", timeout=5, proxies=_NO_PROXY)
            history.raise_for_status()
            entry = history.json().get(prompt_id)
            if entry:
                images = entry.get("outputs", {}).get("9", {}).get("images", [])
                if images:
                    image_meta = images[0]
                    break
                if entry.get("status", {}).get("status_str") == "error":
                    raise ComfyError("ComfyUI could not generate that image")
            sleep(poll_interval)
        if image_meta is None:
            raise ComfyError("Image generation timed out")
        image = requests.get(
            f"{base_url}/view",
            params={
                "filename": image_meta["filename"],
                "subfolder": image_meta.get("subfolder", ""),
                "type": image_meta.get("type", "temp"),
            },
            timeout=30,
            proxies=_NO_PROXY,
        )
        image.raise_for_status()
        content_type = image.headers.get("Content-Type", "image/png").split(";")[0].strip() or "image/png"
        if not content_type.startswith("image/"):
            raise ComfyError("ComfyUI returned a non-image response")
        out_w, out_h = spec.output_size()
        return {
            "bytes": image.content,
            "content_type": content_type,
            "seed": spec.seed,
            "width": out_w,
            "height": out_h,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "comfy_prompt_id": prompt_id,
        }
    except ComfyError:
        raise
    except (KeyError, ValueError, TypeError, requests.RequestException) as exc:
        raise ComfyError("Local image generator is unavailable") from exc


def data_url(content_type: str, payload: bytes) -> str:
    return f"data:{content_type};base64,{base64.b64encode(payload).decode('ascii')}"
