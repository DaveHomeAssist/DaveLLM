"""Bounded loopback client for DaveLLM image generation through ComfyUI.

The client builds fixed SDXL Turbo workflows:

* ``draft`` (text to image): a single KSampler pass at the requested size.
* ``img2img``: the source image is scaled and center-cropped to the requested
  size, VAE-encoded, and sampled with ``denoise = strength``.
* ``inpaint``: like ``img2img``, plus a mask (white = repaint) scaled and cropped
  the same way and applied with ``SetLatentNoiseMask``, so only the painted
  area changes. SDXL Turbo is not an inpainting checkpoint; this is the
  standard approach for a regular checkpoint, not a dedicated inpaint model.
* ``quality``: any of the above (same seed, same settings) followed by a
  latent upscale and a lower-denoise refinement pass, so the quality pass keeps
  the draft's composition instead of starting a new image.

Every graph also renders a 256-pixel thumbnail on the GPU (nodes 20-21), so
the router never decodes images itself.

Source images and masks are uploaded to ComfyUI's *temp* folder (cleared when
ComfyUI restarts), never its permanent input folder.

Output uses ComfyUI's ``PreviewImage`` node, which writes to ComfyUI's temp
folder (cleared when ComfyUI restarts) rather than its permanent output folder.
DaveLLM's own image store (``davellm_image_store``) is the only durable copy,
so DaveLLM's delete and purge controls are authoritative.
"""

from __future__ import annotations

import base64
import json
import os
import random
import socket
import struct
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

WORKFLOW_VERSIONS = {
    ("txt2img", False): "sdxl-turbo-draft-v2",
    ("txt2img", True): "sdxl-turbo-quality-v2",
    ("img2img", False): "sdxl-turbo-img2img-v1",
    ("img2img", True): "sdxl-turbo-img2img-quality-v1",
    ("inpaint", False): "sdxl-turbo-inpaint-v1",
    ("inpaint", True): "sdxl-turbo-inpaint-quality-v1",
}
DRAFT_WORKFLOW_VERSION = WORKFLOW_VERSIONS[("txt2img", False)]
QUALITY_WORKFLOW_VERSION = WORKFLOW_VERSIONS[("txt2img", True)]
MODES = ("txt2img", "img2img", "inpaint")
THUMB_SIDE = 256
SUPPORTED_SAMPLERS = ("euler", "euler_ancestral", "dpmpp_2m", "dpmpp_sde")
SUPPORTED_SCHEDULERS = ("auto", "simple", "normal", "karras", "sgm_uniform")
_NO_PROXY = {"http": None, "https": None}


class ComfyError(RuntimeError):
    """A safe, user-facing ComfyUI failure."""


class ComfyCancelled(ComfyError):
    """The job was cancelled before it finished."""


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
    scheduler: str = "auto"
    seed: Optional[int] = None
    quality: Optional[QualityPass] = field(default=None)
    mode: str = "txt2img"
    strength: float = 1.0
    # ComfyUI annotated file names ("name.png [temp]"), set by upload_image.
    source_image: Optional[str] = None
    mask_image: Optional[str] = None

    @property
    def workflow_version(self) -> str:
        return WORKFLOW_VERSIONS[(self.mode, bool(self.quality))]

    def total_steps(self) -> int:
        return self.steps + (self.quality.steps if self.quality else 0)

    def output_size(self) -> tuple[int, int]:
        if not self.quality:
            return self.width, self.height
        return _round64(self.width * self.quality.scale), _round64(self.height * self.quality.scale)

    def settings(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("prompt", "negative_prompt", "source_image", "mask_image"):
            data.pop(key)
        if self.mode == "txt2img":
            data.pop("strength")
        return data


def _round64(value: float) -> int:
    return max(64, int(round(value / 64.0)) * 64)


def new_seed() -> int:
    return random.SystemRandom().randrange(0, 2**63)


def thumb_size(width: int, height: int, side: int = THUMB_SIDE) -> tuple[int, int]:
    scale = min(1.0, side / float(max(width, height)))
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def build_workflow(spec: ImageSpec, checkpoint: str) -> dict[str, Any]:
    """Return the ComfyUI API-format graph for ``spec``. ``spec.seed`` must be set."""
    if spec.seed is None:
        raise ValueError("seed must be resolved before building a workflow")
    if spec.mode not in MODES:
        raise ValueError(f"unsupported mode {spec.mode!r}")
    if spec.mode != "txt2img" and not spec.source_image:
        raise ValueError(f"{spec.mode} needs a source image")
    if spec.mode == "inpaint" and not spec.mask_image:
        raise ValueError("inpaint needs a mask")
    if not 0.0 < spec.strength <= 1.0:
        raise ValueError("strength must be in (0, 1]")
    if spec.sampler not in SUPPORTED_SAMPLERS:
        raise ValueError(f"unsupported sampler {spec.sampler!r}")
    if spec.scheduler not in SUPPORTED_SCHEDULERS:
        raise ValueError(f"unsupported scheduler {spec.scheduler!r}")
    regular_scheduler = "simple" if spec.scheduler == "auto" else spec.scheduler
    graph: dict[str, Any] = {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": spec.seed, "steps": spec.steps, "cfg": spec.cfg,
            "sampler_name": spec.sampler, "scheduler": regular_scheduler,
            "denoise": 1.0 if spec.mode == "txt2img" else spec.strength,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
        }},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": spec.prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": spec.negative_prompt, "clip": ["4", 1]}},
    }
    fit = {"upscale_method": "lanczos", "width": spec.width, "height": spec.height, "crop": "center"}
    if spec.mode == "txt2img":
        graph["5"] = {"class_type": "EmptyLatentImage", "inputs": {
            "width": spec.width, "height": spec.height, "batch_size": 1}}
    else:
        graph["12"] = {"class_type": "LoadImage", "inputs": {"image": spec.source_image}}
        graph["14"] = {"class_type": "ImageScale", "inputs": {"image": ["12", 0], **fit}}
        graph["13"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["14", 0], "vae": ["4", 2]}}
        graph["3"]["inputs"]["latent_image"] = ["13", 0]
    if spec.mode == "inpaint":
        # The mask goes through the same scale-and-crop as the image so they stay aligned.
        graph["15"] = {"class_type": "LoadImageMask", "inputs": {"image": spec.mask_image, "channel": "red"}}
        graph["17"] = {"class_type": "MaskToImage", "inputs": {"mask": ["15", 0]}}
        graph["18"] = {"class_type": "ImageScale", "inputs": {"image": ["17", 0], **fit}}
        graph["19"] = {"class_type": "ImageToMask", "inputs": {"image": ["18", 0], "channel": "red"}}
        graph["16"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["13", 0], "mask": ["19", 0]}}
        graph["3"]["inputs"]["latent_image"] = ["16", 0]
    # SDTurboScheduler exposes ten distilled timesteps. Keep longer runs and
    # partial-denoise editing passes on their existing regular schedule.
    if (spec.scheduler == "auto" and spec.mode == "txt2img" and spec.steps <= 10
            and checkpoint == "sd_xl_turbo_1.0_fp16.safetensors"):
        graph["22"] = {"class_type": "SDTurboScheduler", "inputs": {
            "model": ["4", 0], "steps": spec.steps, "denoise": 1.0}}
        graph["23"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": spec.sampler}}
        graph["3"] = {"class_type": "SamplerCustom", "inputs": {
            "add_noise": True, "noise_seed": spec.seed, "cfg": spec.cfg, "model": ["4", 0],
            "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
            "sampler": ["23", 0], "sigmas": ["22", 0]}}
    samples = ["3", 0]
    if spec.quality:
        out_w, out_h = spec.output_size()
        graph["10"] = {"class_type": "LatentUpscale", "inputs": {
            "samples": ["3", 0], "upscale_method": "nearest-exact",
            "width": out_w, "height": out_h, "crop": "disabled"}}
        graph["11"] = {"class_type": "KSampler", "inputs": {
            "seed": spec.seed, "steps": spec.quality.steps, "cfg": spec.cfg,
            "sampler_name": spec.sampler, "scheduler": regular_scheduler, "denoise": spec.quality.denoise,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["10", 0]}}
        samples = ["11", 0]
    graph["8"] = {"class_type": "VAEDecode", "inputs": {"samples": samples, "vae": ["4", 2]}}
    graph["9"] = {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}}
    tw, th = thumb_size(*spec.output_size())
    graph["20"] = {"class_type": "ImageScale", "inputs": {
        "image": ["8", 0], "upscale_method": "area", "width": tw, "height": th, "crop": "disabled"}}
    graph["21"] = {"class_type": "PreviewImage", "inputs": {"images": ["20", 0]}}
    return graph


def upload_image(base_url: str, payload: bytes, *, kind: str = "source", timeout: float = 30.0) -> str:
    """Upload a PNG/JPEG to ComfyUI's temp folder; return the annotated name LoadImage expects."""
    name = f"davellm-{kind}-{uuid.uuid4().hex[:12]}.png"
    try:
        response = requests.post(
            f"{base_url}/upload/image",
            files={"image": (name, payload, "image/png")},
            data={"type": "temp", "overwrite": "true"},
            timeout=timeout, proxies=_NO_PROXY,
        )
        response.raise_for_status()
        info = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ComfyError("Could not hand the source image to the local generator") from exc
    stored = info.get("name") or name
    subfolder = info.get("subfolder") or ""
    return f"{subfolder + '/' if subfolder else ''}{stored} [temp]"


def interrupt(base_url: str, prompt_id: Optional[str] = None) -> None:
    """Stop ``prompt_id`` if it is running and drop it if it is still queued. Best effort."""
    for path, body in (("/queue", {"delete": [prompt_id]} if prompt_id else None),
                       ("/interrupt", {"prompt_id": prompt_id} if prompt_id else {})):
        if body is None:
            continue
        try:
            requests.post(f"{base_url}{path}", json=body, timeout=5, proxies=_NO_PROXY)
        except requests.RequestException:
            pass


# -- progress over ComfyUI's websocket ---------------------------------------------


class _WebSocket:
    """Just enough RFC 6455 client to read ComfyUI's progress messages (stdlib only)."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buffer = b""

    @classmethod
    def connect(cls, base_url: str, client_id: str, timeout: float = 3.0) -> "_WebSocket":
        parsed = urlparse(base_url)
        host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
        sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock.sendall((
            f"GET /ws?clientId={client_id} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii"))
        ws = cls(sock)
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("websocket handshake closed")
            header += chunk
        head, ws._buffer = header.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            sock.close()
            raise OSError("websocket upgrade refused")
        return ws

    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("websocket closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)

    def frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read(2)
        opcode, length = first & 0x0F, second & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._read(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read(8))
        mask = self._read(4) if second & 0x80 else None
        payload = self._read(length)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bool(first & 0x80), opcode, payload

    def messages(self):
        """Yield complete text messages; answer pings; stop on close."""
        pieces: list[bytes] = []
        text = False
        while True:
            fin, opcode, payload = self.frame()
            if opcode == 0x8:
                return
            if opcode == 0x9:
                self._send(0xA, payload[:125])
                continue
            if opcode in (0x1, 0x2):
                pieces, text = [payload], opcode == 0x1
            elif opcode == 0x0:
                pieces.append(payload)
            else:
                continue
            if fin and text:
                yield b"".join(pieces).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._send(0x8)
        except OSError:
            pass
        self.sock.close()


class ProgressWatcher:
    """Turns ComfyUI ``progress`` events for one prompt into (value, max) over all passes."""

    def __init__(self, base_url: str, spec: ImageSpec, on_progress: Callable[[int, int], None]):
        self.client_id = uuid.uuid4().hex
        self.prompt_id: Optional[str] = None
        self.spec = spec
        self.on_progress = on_progress
        self.connected = False
        self._stop = threading.Event()
        self._ws: Optional[_WebSocket] = None
        try:
            self._ws = _WebSocket.connect(base_url, self.client_id)
            self._ws.sock.settimeout(1.0)
            self.connected = True
        except OSError:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def handle(self, message: str) -> None:
        try:
            event = json.loads(message)
        except ValueError:
            return
        if event.get("type") != "progress":
            return
        data = event.get("data") or {}
        # Until /prompt answers we do not know our id; skipping an early step is harmless.
        if "prompt_id" in data and data.get("prompt_id") != self.prompt_id:
            return
        offset = self.spec.steps if str(data.get("node")) == "11" else 0
        try:
            self.on_progress(offset + int(data.get("value", 0)), self.spec.total_steps())
        except (TypeError, ValueError):
            pass

    def _run(self) -> None:
        assert self._ws is not None
        while not self._stop.is_set():
            try:
                for message in self._ws.messages():
                    self.handle(message)
                    if self._stop.is_set():
                        break
                return
            except socket.timeout:
                continue
            except OSError:
                return

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.close()


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
    on_progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> dict[str, Any]:
    """Run ``spec`` and return PNG bytes plus timing. Raises ``ComfyError``.

    ``on_progress(value, max)`` receives sampler steps across all passes when
    ComfyUI's websocket is reachable. Setting ``cancel`` interrupts the prompt
    and raises ``ComfyCancelled``.
    """
    if spec.seed is None:
        spec.seed = new_seed()
    started = time.monotonic()
    watcher = ProgressWatcher(base_url, spec, on_progress) if on_progress else None
    prompt_id = None
    try:
        body: dict[str, Any] = {"prompt": build_workflow(spec, checkpoint)}
        if watcher and watcher.connected:
            body["client_id"] = watcher.client_id
        queued = requests.post(f"{base_url}/prompt", json=body, timeout=10, proxies=_NO_PROXY)
        queued.raise_for_status()
        prompt_id = queued.json()["prompt_id"]
        if watcher:
            watcher.prompt_id = prompt_id
        deadline = started + timeout
        image_meta = None
        thumb_meta = None
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                interrupt(base_url, prompt_id)
                raise ComfyCancelled("Cancelled")
            history = requests.get(f"{base_url}/history/{prompt_id}", timeout=5, proxies=_NO_PROXY)
            history.raise_for_status()
            entry = history.json().get(prompt_id)
            if entry:
                images = entry.get("outputs", {}).get("9", {}).get("images", [])
                if images:
                    image_meta = images[0]
                    thumbs = entry.get("outputs", {}).get("21", {}).get("images", [])
                    thumb_meta = thumbs[0] if thumbs else None
                    break
                if entry.get("status", {}).get("status_str") == "error":
                    if cancel is not None and cancel.is_set():
                        raise ComfyCancelled("Cancelled")
                    raise ComfyError("ComfyUI could not generate that image")
            sleep(poll_interval)
        if image_meta is None:
            raise ComfyError("Image generation timed out")
        image = _fetch_view(base_url, image_meta)
        content_type = image.headers.get("Content-Type", "image/png").split(";")[0].strip() or "image/png"
        if not content_type.startswith("image/"):
            raise ComfyError("ComfyUI returned a non-image response")
        thumb = None
        if thumb_meta:
            try:
                thumb = _fetch_view(base_url, thumb_meta).content
            except requests.RequestException:
                thumb = None
        out_w, out_h = spec.output_size()
        return {
            "bytes": image.content,
            "thumbnail": thumb,
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
    finally:
        if watcher:
            watcher.stop()


def _fetch_view(base_url: str, meta: dict[str, Any]) -> requests.Response:
    response = requests.get(
        f"{base_url}/view",
        params={"filename": meta["filename"], "subfolder": meta.get("subfolder", ""), "type": meta.get("type", "temp")},
        timeout=30, proxies=_NO_PROXY,
    )
    response.raise_for_status()
    return response


def data_url(content_type: str, payload: bytes) -> str:
    return f"data:{content_type};base64,{base64.b64encode(payload).decode('ascii')}"
