#!/usr/bin/env python3
"""Send one bounded, stateless text request through an existing local DaveLLM router."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import math
import os
import subprocess
import sys
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class CommandError(Exception):
    """An operator-facing failure that must not include credentials or raw bodies."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_origin(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        port = parts.port
    except ValueError:
        loopback = False
    if (
        not loopback
        or parts.scheme not in {"http", "https"}
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or port == 0
    ):
        raise argparse.ArgumentTypeError("use a loopback router origin, for example http://127.0.0.1:8000")
    return value.rstrip("/")


def token_limit(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 32768:
        raise argparse.ArgumentTypeError("max tokens must be between 1 and 32768")
    return result


def timeout_seconds(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or not 0 < result <= 600:
        raise argparse.ArgumentTypeError("timeout must be finite and greater than 0, up to 600 seconds")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", type=local_origin,
                        default=os.getenv("DAVE_API_BASE", "http://127.0.0.1:8000"))
    parser.add_argument("--node", required=True, help="existing configured node ID")
    parser.add_argument("--model", required=True, help="model ID installed on that node")
    parser.add_argument("--max-tokens", type=token_limit, default=128)
    parser.add_argument("--timeout", type=timeout_seconds, default=180,
                        help="completion request timeout in seconds (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="print the router reply as JSON")
    parser.add_argument("prompt", help="one text prompt, quoted as a single argument")
    return parser


def load_api_key() -> str:
    key = os.getenv("DAVE_API_KEY", "").strip()
    if key:
        return key
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password",
                 "-a", os.getenv("DAVE_KEYCHAIN_ACCOUNT", getpass.getuser()),
                 "-s", os.getenv("DAVE_KEYCHAIN_SERVICE", "com.davellm.api-key"), "-w"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise CommandError("The existing DaveLLM Keychain item is unavailable.") from None
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise CommandError("DAVE_API_KEY or the existing macOS DaveLLM Keychain item is required.")


def request_json(opener: Any, api_base: str, api_key: str, path: str,
                 timeout: float, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(api_base + path, data=body, headers=headers,
                      method="POST" if body is not None else "GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        hints = {
            400: "The node, model or request was rejected. Check the selected inventory.",
            401: "Authentication failed. Check the existing DaveLLM credential.",
            404: "The node or required router endpoint is unavailable.",
            409: "Model inventory is not ready. Check the existing node.",
            429: "The router rate limit was reached. Wait before running again.",
            503: "The existing router or selected node is unavailable.",
            504: "The selected node timed out.",
        }
        hint = "Redirect refused." if 300 <= exc.code < 400 else hints.get(exc.code, "Router request failed.")
        raise CommandError(f"HTTP {exc.code}: {hint}") from None
    except (TimeoutError, URLError):
        raise CommandError("Router request timed out or could not connect. No retry was sent.") from None
    except (HTTPException, OSError, ValueError):
        raise CommandError("The router returned an unreadable or invalid JSON reply.") from None
    if not isinstance(result, dict):
        raise CommandError("The router returned an invalid reply shape.")
    return result


def ask(args: argparse.Namespace, api_key: str) -> dict[str, Any]:
    opener = build_opener(ProxyHandler({}), NoRedirect())
    inventory = request_json(
        opener, args.api_base, api_key, f"/nodes/{quote(args.node, safe='')}/models",
        min(args.timeout, 15),
    )
    models = inventory.get("models")
    if inventory.get("error") or not isinstance(models, list):
        raise CommandError("The selected node's model inventory is unavailable.")
    if not any(isinstance(model, dict) and model.get("id") == args.model for model in models):
        raise CommandError("The selected model is not installed on that node. No inference was sent.")
    result = request_json(opener, args.api_base, api_key, "/eval/chat", args.timeout, {
        "node_id": args.node,
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
        "temperature": 0,
    })
    content = result.get("response")
    if result.get("reason") or not isinstance(content, str) or not content.strip():
        raise CommandError("The model returned no usable text. No tool was executed or retry sent.")
    if result.get("done_reason") != "stop":
        raise CommandError("The model did not finish normally. Check the output limit; no retry was sent.")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.prompt.strip() or len(args.prompt) > 100_000:
        parser.error("prompt must contain text and be at most 100000 characters")
    try:
        result = ask(args, load_api_key())
    except CommandError as exc:
        print(f"davellm-ask: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("davellm-ask: interrupted; the router may still finish the request.", file=sys.stderr)
        return 130
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else result["response"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
