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
    if "10" in graph:
        inputs = graph["10"]["inputs"]
        return int(inputs["width"]), int(inputs["height"])
    inputs = graph["5"]["inputs"]
    return int(inputs["width"]), int(inputs["height"])


def validate_graph(graph: dict[str, Any]) -> None:
    required = {"3": "KSampler", "4": "CheckpointLoaderSimple", "5": "EmptyLatentImage",
                "6": "CLIPTextEncode", "7": "CLIPTextEncode", "8": "VAEDecode", "9": "PreviewImage"}
    for node_id, class_type in required.items():
        if graph.get(node_id, {}).get("class_type") != class_type:
            raise ValueError(f"node {node_id} must be {class_type}")
    if "10" in graph and graph.get("11", {}).get("class_type") != "KSampler":
        raise ValueError("quality graphs need a refinement KSampler at node 11")


class FakeComfy:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.history: dict[str, Any] = {}
        self.images: dict[str, bytes] = {}
        self.graphs: list[dict[str, Any]] = []
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
                if urlparse(self.path).path != "/prompt":
                    return self._json(404, {"error": "not found"})
                length = int(self.headers.get("Content-Length", "0"))
                graph = json.loads(self.rfile.read(length) or b"{}").get("prompt", {})
                try:
                    validate_graph(graph)
                except (ValueError, KeyError, TypeError) as exc:
                    return self._json(400, {"error": str(exc)})
                fake.graphs.append(graph)
                prompt_id = uuid.uuid4().hex
                if fake.fail:
                    fake.history[prompt_id] = {"outputs": {}, "status": {"status_str": "error"}}
                    return self._json(200, {"prompt_id": prompt_id})
                width, height = _output_size(graph)
                key = json.dumps({
                    "positive": graph["6"]["inputs"]["text"], "negative": graph["7"]["inputs"]["text"],
                    "seed": graph["3"]["inputs"]["seed"], "steps": graph["3"]["inputs"]["steps"],
                    "size": [width, height], "quality": "10" in graph,
                }, sort_keys=True).encode()
                filename = f"DaveLLM_temp_{prompt_id[:8]}.png"
                fake.images[filename] = make_png(width, height, key)
                fake.history[prompt_id] = {
                    "outputs": {"9": {"images": [{"filename": filename, "subfolder": "", "type": "temp"}]}},
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
    args = parser.parse_args()
    fake = FakeComfy().start(args.host, args.port)
    print(f"fake ComfyUI listening on {fake.url}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
