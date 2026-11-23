#!/usr/bin/env python3
"""Serve the wallboard allowlist on loopback without exposing the source tree."""
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat

from export_wallboard_status import project, timestamp

ASSETS = {"wallboard.html": "text/html; charset=utf-8", "wallboard.css": "text/css; charset=utf-8",
          "wallboard.js": "text/javascript; charset=utf-8", "on-call.js": "text/javascript; charset=utf-8"}
LIMIT = 1024 * 1024


def read_regular(path: Path) -> bytes:
    if path.resolve() != path:
        raise ValueError("symlink path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("not a regular file")
        content = source.read(LIMIT + 1)
    if len(content) > LIMIT:
        raise ValueError("oversized file")
    return content


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, root: Path, status_file: Path, **kwargs):
        self.root, self.status_file = root, status_file
        super().__init__(*args, **kwargs)

    def log_message(self, *args):
        # Requests and query strings can contain sensitive data; do not log them.
        pass

    def do_HEAD(self):
        self.serve(head=True)

    def do_GET(self):
        self.serve(head=False)

    def serve(self, *, head: bool):
        # The base handler normalizes leading double slashes. Match the original
        # request target so only exact origin-form routes enter the allowlist.
        route = self.requestline.split()[1].split("?", 1)[0]
        name = "wallboard.html" if route == "/" else route.removeprefix("/")
        code, body, mime = 404, b"Not found\n", "text/plain; charset=utf-8"
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        hosts = self.headers.get_all("Host", [])
        host_allowed = len(hosts) == 1 and hosts[0].lower() in allowed_hosts
        try:
            if not host_allowed:
                code, body = 403, b"Host refused\n"
            elif route in {"/", *["/" + n for n in ASSETS]}:
                body, mime, code = read_regular(self.root / name), ASSETS[name], 200
            elif route == "/wallboard-status.json":
                raw = json.loads(read_regular(self.status_file))
                safe = project(raw)
                safe["published_at"] = timestamp(raw.get("published_at"))
                body, mime, code = json.dumps(safe, allow_nan=False).encode(), "application/json", 200
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, RuntimeError):
            code, body = 503, b"Source unavailable\n"
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if not head:
            self.wfile.write(body)


def make_server(root: Path, status_file: Path, port: int = 8766):
    return ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, root=root.absolute(), status_file=status_file.absolute()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be unprivileged")
    with make_server(args.root, args.status, args.port) as server:
        server.serve_forever()
