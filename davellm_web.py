"""Extended web tools: ``web.search`` through a private SearXNG and ``web.read`` as readable text.

``web.search`` talks only to the operator-configured ``DAVE_SEARCH_URL`` (normally a
SearXNG instance on the tailnet), so it deliberately skips the public-address check: that
address is chosen by the operator, never by the model. The result URLs it returns are only
ever opened through ``web.read`` or ``web.fetch``, which validate every hop.

``web.read`` repeats ``web.fetch``'s transport rules (public address check on every
redirect hop, redirect limit, byte cap) but returns the page's visible text instead of
raw HTML, so the useful part of a page is not lost to the output limit. ``web.fetch``
itself is a qualified built-in and stays unchanged.

The module reads no environment and never imports ``app``; the host injects the search
URL, the public-address validator and the limits.
"""

from __future__ import annotations

import asyncio
import json
import re
from html.parser import HTMLParser
from typing import Callable, Dict, Optional
from urllib.parse import urljoin, urlparse

import httpx

WEB_SEARCH_DEFAULT_RESULTS = 5
WEB_SEARCH_MAX_RESULTS = 10
WEB_SEARCH_MAX_QUERY_CHARS = 500
WEB_SNIPPET_CHARS = 300
WEB_TIMEOUT_SECONDS = 10.0
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
TEXT_TYPES = ("text/plain", "application/json", "text/markdown", "text/csv")
HTML_TYPES = ("text/html", "application/xhtml+xml")

# Elements whose text is never part of the readable page.
_SKIPPED = frozenset({"script", "style", "noscript", "svg", "template", "iframe", "object", "canvas"})
# Elements that start a new line in the readable text.
_BLOCKS = frozenset({
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
    "nav", "main", "aside", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dt", "dd",
    "figcaption", "title", "form", "hr",
})


class WebToolError(Exception):
    """A failure whose message is safe to show the model."""


def normalize_search_url(value: Optional[str]) -> Optional[str]:
    """``DAVE_SEARCH_URL`` as a base URL, or ``None`` when unset or not http(s)."""
    if not value or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip().rstrip("/")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED:
            self.skip_depth += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIPPED:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text of an HTML document: one line per block, runs of whitespace collapsed."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = (re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    note = f"\n[truncated: showing {limit} of {len(text)} characters]"
    return text[: max(0, limit - len(note))] + note


async def search_web(search_url: Optional[str], params: Dict, *, output_limit: int) -> str:
    """Top results for ``query`` from SearXNG as numbered title / URL / snippet entries."""
    if not search_url:
        raise WebToolError("web.search is not configured on this router (DAVE_SEARCH_URL is unset).")
    query = str(params.get("query") or "").strip()
    if not query:
        raise WebToolError("Missing query")
    if len(query) > WEB_SEARCH_MAX_QUERY_CHARS:
        raise WebToolError(f"Query exceeds {WEB_SEARCH_MAX_QUERY_CHARS} characters")
    count = params.get("max_results")
    count = WEB_SEARCH_DEFAULT_RESULTS if count is None else max(1, min(int(count), WEB_SEARCH_MAX_RESULTS))

    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=WEB_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{search_url}/search", params={"q": query, "format": "json", "safesearch": 1}
            )
    except httpx.TimeoutException:
        raise WebToolError("The search service timed out") from None
    except httpx.HTTPError:
        raise WebToolError("The search service could not be reached") from None
    if resp.status_code != 200:
        raise WebToolError(f"The search service returned HTTP {resp.status_code}")
    try:
        results = resp.json().get("results") or []
    except (ValueError, AttributeError):
        raise WebToolError("The search service returned an invalid response") from None

    lines, seen = [], set()
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if urlparse(url).scheme not in {"http", "https"} or url in seen:
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip() or url
        snippet = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()[:WEB_SNIPPET_CHARS]
        lines.append(f"{len(seen)}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else ""))
        if len(seen) >= count:
            break
    if not lines:
        return f'No results for "{query}".'
    return _clip(f'Results for "{query}":\n\n' + "\n\n".join(lines), output_limit)


async def read_page(
    params: Dict,
    *,
    validate_public_url: Callable[[str], None],
    max_bytes: int,
    max_redirects: int,
    output_limit: int,
) -> str:
    """A public page as readable text, with ``web.fetch``'s per-hop address checks."""
    url = str(params.get("url") or "").strip()
    if not url:
        raise WebToolError("Missing url")
    current = url
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=WEB_TIMEOUT_SECONDS) as client:
            for hop in range(max_redirects + 1):
                try:
                    await asyncio.to_thread(validate_public_url, current)
                except Exception as exc:  # the validator's message names the refused address class
                    raise WebToolError(str(exc)) from None
                async with client.stream("GET", current) as resp:
                    if resp.status_code in REDIRECT_CODES:
                        location = resp.headers.get("location")
                        if not location:
                            raise WebToolError("Redirect response is missing Location")
                        if hop >= max_redirects:
                            raise WebToolError("Too many redirects")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise WebToolError(f"The page returned HTTP {resp.status_code}")
                    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if content_type and not content_type.startswith(HTML_TYPES + TEXT_TYPES):
                        raise WebToolError(f"Unsupported content type: {content_type}")
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise WebToolError(f"Response exceeds {max_bytes} byte limit")
                    text = bytes(body).decode(resp.encoding or "utf-8", errors="replace")
                    if content_type.startswith(HTML_TYPES) or (not content_type and text.lstrip()[:1] == "<"):
                        text = html_to_text(text)
                    if content_type == "application/json":
                        try:
                            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
                        except ValueError:
                            pass
                    header = f"{current}\n\n" if current != url else ""
                    return _clip(header + text, output_limit)
    except httpx.TimeoutException:
        raise WebToolError("The page timed out") from None
    except httpx.HTTPError:
        raise WebToolError("The page could not be reached") from None
    raise WebToolError("Fetch did not produce a response")
