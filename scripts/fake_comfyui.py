"""A tiny stand-in for ComfyUI's HTTP API, for tests, CI and offline demos.

It accepts the same API-format graphs DaveLLM sends, checks their shape, and
returns a deterministic PNG derived from the prompt, seed and output size, so
"same seed, same settings" produces byte-identical output exactly like a
deterministic real workflow should. It never loads a model.

Run standalone:  python scripts/fake_comfyui.py --port 8189
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def make_png(width: int, height: int, key: bytes) -> bytes:
    digest = hashlib.sha256(key).digest()
    r0, g0, b0, r1, g1, b1 = digest[:6]
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        t = y / max(1, height - 1)
        band = ((y // 32) + digest[6]) % 2
        for x in range(width):
            u = x / max(1, width - 1)
            r = int(r0 * (1 - u) + r1 * u)
            g = int(g0 * (1 - t) + g1 * t)
            b = int((b0 + b1) / 2) ^ (24 if band and ((x // 32) % 2) else 0)
            rows += bytes((r & 255, g & 255, b & 255))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


def _output_size(graph: dict[str, Any]) -> tuple[int, int]:
    node = graph.get("10") or graph.get("5") or graph.get("14")
    inputs = node["inputs"]
    return int(inputs["width"]), int(inputs["height"])


def validate_graph(graph: dict[str, Any], uploads: dict[str, bytes] | None = None) -> None:
    custom = graph.get("3", {}).get("class_type") == "SamplerCustom"
    required = {"3": "SamplerCustom" if custom else "KSampler", "4": "CheckpointLoaderSimple",
                "6": "CLIPTextEncode", "7": "CLIPTextEncode", "8": "VAEDecode", "9": "PreviewImage",
                "20": "ImageScale", "21": "PreviewImage"}
    if custom:
        required.update({"22": "SDTurboScheduler", "23": "KSamplerSelect"})
    for node_id, class_type in required.items():
        if graph.get(node_id, {}).get("class_type") != class_type:
            raise ValueError(f"node {node_id} must be {class_type}")
    if "10" in graph and graph.get("11", {}).get("class_type") != "KSampler":
        raise ValueError("quality graphs need a refinement KSampler at node 11")
    latent = graph["3"]["inputs"]["latent_image"][0]
    if latent == "5":
        if graph["5"].get("class_type") != "EmptyLatentImage":
            raise ValueError("text-to-image graphs need EmptyLatentImage at node 5")
    elif latent in ("13", "16"):
        for node_id, class_type in {"12": "LoadImage", "14": "ImageScale", "13": "VAEEncode"}.items():
            if graph.get(node_id, {}).get("class_type") != class_type:
                raise ValueError(f"node {node_id} must be {class_type}")
        if uploads is not None and graph["12"]["inputs"]["image"] not in uploads:
            raise ValueError("source image was not uploaded")
        if latent == "16":
            for node_id, class_type in {"15": "LoadImageMask", "17": "MaskToImage", "18": "ImageScale",
                                        "19": "ImageToMask", "16": "SetLatentNoiseMask"}.items():
                if graph.get(node_id, {}).get("class_type") != class_type:
                    raise ValueError(f"node {node_id} must be {class_type}")
            if uploads is not None and graph["15"]["inputs"]["image"] not in uploads:
                raise ValueError("mask was not uploaded")
    else:
        raise ValueError("unknown latent source")


def _multipart(body: bytes, content_type: str) -> dict[str, tuple[str | None, bytes]]:
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"').encode()
    fields = {}
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, value = part.split(b"\r\n\r\n", 1)
        value = value[:-2] if value.endswith(b"\r\n") else value
        headers = head.decode("latin-1")
        if 'name="' not in headers:
            continue
        name = headers.split('name="', 1)[1].split('"', 1)[0]
        filename = headers.split('filename="', 1)[1].split('"', 1)[0] if 'filename="' in headers else None
        fields[name] = (filename, value)
    return fields


class FakeComfy:
    def __init__(self, *, fail: bool = False, delay: float = 0.0):
        self.fail = fail
        self.delay = delay  # seconds before a prompt "finishes", for queue and cancel tests
        self.history: dict[str, Any] = {}
        self.ready_at: dict[str, float] = {}
        self.interrupted: set[str] = set()
        self.images: dict[str, bytes] = {}
        self.uploads: dict[str, bytes] = {}
        self.upload_types: list[str] = []
        self.graphs: list[dict[str, Any]] = []
        self.interrupt_calls = 0
        self.timeline: list[tuple[float, float]] = []  # (queued, finishes) per prompt
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self, host: str = "127.0.0.1", port: int = 0) -> "FakeComfy":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # keep test output quiet
                return

            def _json(self, code: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/system_stats":
                    return self._json(200, {"system": {"comfyui_version": "fake-0.1"},
                                            "devices": [{"name": "fake-cpu"}]})
                if parsed.path.startswith("/history/"):
                    prompt_id = parsed.path.rsplit("/", 1)[-1]
                    with fake._lock:
                        if prompt_id in fake.interrupted:
                            entry = {"outputs": {}, "status": {"status_str": "error"}}
                        elif time.monotonic() < fake.ready_at.get(prompt_id, 0):
                            entry = None
                        else:
                            entry = fake.history.get(prompt_id)
                    return self._json(200, {prompt_id: entry} if entry else {})
                if parsed.path == "/view":
                    name = parse_qs(parsed.query).get("filename", [""])[0]
                    data = fake.images.get(name)
                    if data is None:
                        return self._json(404, {"error": "not found"})
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return None
                return self._json(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                path = urlparse(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                if path == "/upload/image":
                    fields = _multipart(raw, self.headers.get("Content-Type", ""))
                    filename, data = fields["image"]
                    kind = fields.get("type", (None, b"input"))[1].decode()
                    fake.upload_types.append(kind)
                    fake.uploads[f"{filename} [{kind}]"] = data
                    return self._json(200, {"name": filename, "subfolder": "", "type": kind})
                if path == "/interrupt":
                    fake.interrupt_calls += 1
                    body = json.loads(raw or b"{}")
                    with fake._lock:
                        targets = [body["prompt_id"]] if body.get("prompt_id") else list(fake.ready_at)
                        for prompt_id in targets:
                            if time.monotonic() < fake.ready_at.get(prompt_id, 0):
                                fake.interrupted.add(prompt_id)
                    return self._json(200, {})
                if path == "/queue":
                    return self._json(200, {})
                if path != "/prompt":
                    return self._json(404, {"error": "not found"})
                graph = json.loads(raw or b"{}").get("prompt", {})
                try:
                    validate_graph(graph, fake.uploads)
                except (ValueError, KeyError, TypeError) as exc:
                    return self._json(400, {"error": str(exc)})
                fake.graphs.append(graph)
                prompt_id = uuid.uuid4().hex
                if fake.fail:
                    fake.history[prompt_id] = {"outputs": {}, "status": {"status_str": "error"}}
                    return self._json(200, {"prompt_id": prompt_id})
                width, height = _output_size(graph)
                source = graph.get("12", {}).get("inputs", {}).get("image")
                mask = graph.get("15", {}).get("inputs", {}).get("image")
                key = json.dumps({
                    "positive": graph["6"]["inputs"]["text"], "negative": graph["7"]["inputs"]["text"],
                    "seed": graph["3"]["inputs"].get("noise_seed", graph["3"]["inputs"].get("seed")),
                    "steps": graph.get("22", graph["3"])["inputs"]["steps"],
                    "denoise": graph.get("22", graph["3"])["inputs"]["denoise"],
                    "size": [width, height], "quality": "10" in graph,
                    "source": hashlib.sha256(fake.uploads[source]).hexdigest() if source else None,
                    "mask": hashlib.sha256(fake.uploads[mask]).hexdigest() if mask else None,
                }, sort_keys=True).encode()
                filename = f"DaveLLM_temp_{prompt_id[:8]}.png"
                thumb_name = f"DaveLLM_thumb_{prompt_id[:8]}.png"
                fake.images[filename] = make_png(width, height, key)
                tw, th = int(graph["20"]["inputs"]["width"]), int(graph["20"]["inputs"]["height"])
                fake.images[thumb_name] = make_png(tw, th, key)
                with fake._lock:
                    fake.ready_at[prompt_id] = time.monotonic() + fake.delay
                    fake.timeline.append((time.monotonic(), fake.ready_at[prompt_id]))
                    fake.history[prompt_id] = {
                        "outputs": {"9": {"images": [{"filename": filename, "subfolder": "", "type": "temp"}]},
                                    "21": {"images": [{"filename": thumb_name, "subfolder": "", "type": "temp"}]}},
                        "status": {"status_str": "success"},
                    }
                return self._json(200, {"prompt_id": prompt_id})

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8189)
    parser.add_argument("--delay", type=float, default=0.0, help="seconds each prompt takes, to see the queue")
    args = parser.parse_args()
    fake = FakeComfy(delay=args.delay).start(args.host, args.port)
    print(f"fake ComfyUI listening on {fake.url}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
