"""Notion tools: read a configured page, append blocks to its end, and edit one block.

Scope. The operator names the pages DaveLLM may use in ``DAVE_NOTION_PAGES`` (a JSON
object of short names to page IDs) and shares exactly those pages with an internal
Notion connection whose secret is ``DAVE_NOTION_TOKEN``. The model names a page only by
its configured name and a block only by a ref (``b1``, ``b2``, ...) that
``notion.page.read`` issued in the same tool run. The run ledger, kept on the server,
maps each ref to its page, its Notion block ID, and the content seen when it was read;
the model never supplies a Notion ID.

Before every write the adapter reads the target again. A block must still sit under
the same configured page (its parents are walked) and must still hold the content the
run saw, or nothing is sent. This narrows the window for overwriting someone else's
edit; it is not a lock, because Notion offers none.

Outcomes. Every write ends in one of three outcomes:

- ``verified``: a read after the write shows the requested change.
- ``failed``: nothing was written. Either the adapter refused before sending, the
  connection never opened, or Notion answered with a refusal (4xx, including 429).
- ``unknown``: the request may have reached Notion, but no usable answer came back and
  a fresh read could not settle it. An append with this outcome is never repeated by
  the adapter, and the run ledger refuses the same append again for the rest of the run.

The write and its verification run in a task shielded from cancellation, so a run
deadline can stop waiting for it but cannot cut it off between the request and the
check; the late outcome is still recorded in the run ledger. Writes to one page are
serialized within the process, so a match found while checking an uncertain append
cannot be another DaveLLM run's identical write.

Transport. Requests go only to ``https://api.notion.com/v1`` with a pinned
``Notion-Version``, redirects off, environment proxies off, and a response size cap.
The secret travels only in the Authorization header; it never appears in results,
errors, or ``repr``. Only fixed messages and Notion's short error codes reach the model.

The module reads no environment and never imports ``app``; the host injects the
settings, the run ledger, and the page write guard.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Coroutine, Mapping, Optional, Sequence
from urllib.parse import urlparse

import httpx

NOTION_API_HOST = "api.notion.com"
_API_BASE = f"https://{NOTION_API_HOST}/v1"
NOTION_VERSION = "2026-03-11"
NOTION_REQUEST_TIMEOUT_SECONDS = 10.0
NOTION_MIN_REQUEST_SECONDS = 1.0
NOTION_MAX_RESPONSE_BYTES = 2_000_000
NOTION_RATE_LIMIT_MAX_WAIT_SECONDS = 5.0
NOTION_READ_TIMEOUT_SECONDS = 30.0
NOTION_WRITE_TIMEOUT_SECONDS = 60.0
NOTION_READ_BUDGET_SECONDS = 25.0
NOTION_WRITE_BUDGET_SECONDS = 50.0
NOTION_READ_MAX_REQUESTS = 30
NOTION_WRITE_MAX_REQUESTS = 30
NOTION_READ_MAX_BLOCKS = 300
NOTION_READ_MAX_DEPTH = 3
NOTION_LIST_MAX_PAGES = 10
NOTION_PAGE_SIZE = 100
NOTION_PARENT_MAX_DEPTH = 8
NOTION_APPEND_MAX_BLOCKS = 50
NOTION_TEXT_MAX_CHARS = 2000
NOTION_RICH_TEXT_MAX_ITEMS = 100
NOTION_DISPLAY_MAX_CHARS = 4000
NOTION_OUTPUT_BUDGET_BYTES = 48 * 1024
NOTION_RUN_MAX_REFS = 2000
NOTION_MAX_RUNS = 64
NOTION_MAX_PAGES = 20
NOTION_PAGE_NAME_MAX_CHARS = 40
NOTION_REF_MAX_CHARS = 8

TEXT_BLOCK_TYPES = frozenset({
    "paragraph", "heading_1", "heading_2", "heading_3", "heading_4", "bulleted_list_item",
    "numbered_list_item", "to_do", "quote", "callout", "toggle", "code",
})
APPEND_BLOCK_TYPES = (
    "paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item",
    "to_do", "quote",
)
WRITABLE_MENTIONS = frozenset({"page", "database", "user", "date"})
# Children of these are separate pages, databases, or content that may live on another page.
_NO_DESCEND = frozenset({"child_page", "child_database", "synced_block", "unsupported"})
_ANNOTATION_DEFAULTS = (
    ("bold", False), ("italic", False), ("strikethrough", False), ("underline", False),
    ("code", False), ("color", "default"),
)
_ANNOTATION_KEYS = frozenset(key for key, _ in _ANNOTATION_DEFAULTS)

PAGE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,%d}" % (NOTION_PAGE_NAME_MAX_CHARS - 1))
BLOCK_REF = re.compile(r"b[1-9][0-9]{0,5}")
_HEX_ID = re.compile(r"[0-9a-f]{32}")
_TRAILING_HEX_ID = re.compile(r"([0-9a-f]{32})$")
_ERROR_CODE = re.compile(r"[a-z_]{1,40}")

NOT_IN_RUN = "Notion tools work only inside a tool run started from DaveLLM"
NO_TOKEN = "Notion is not configured on this router (DAVE_NOTION_TOKEN is unset)"
NO_PAGES = "No Notion pages are configured on this router (DAVE_NOTION_PAGES is unset)"
BAD_PAGES = "DAVE_NOTION_PAGES is not a JSON object of page names to Notion page IDs"
UNKNOWN_REF = "Unknown block ref {ref}; refs come from notion.page.read in this run"
OTHER_PAGE_REF = "Block {ref} belongs to page '{actual}', not '{requested}'"
PAGE_BUSY = "Another write to this Notion page is still in progress; try again when it has finished"
REPEAT_REFUSED = (
    "An earlier append of these exact blocks to '{page}' in this run has an unknown outcome, so it "
    "is not repeated. Read the page to check whether it arrived, and ask the user before trying again."
)
UNREACHABLE = "Notion could not be reached"
READ_FAILED = "Notion refused the read ({code})"
LEDGER_FULL = "This run has read too many Notion blocks; start a new run"
OLD_TEXT_NOT_FOUND = "old_text was not found in block {ref}"
OLD_TEXT_REPEATED = "old_text occurs {count} times in block {ref}; include more surrounding text so it occurs once"
CROSSES_RUNS = (
    "old_text in block {ref} crosses a formatting change, a link, a mention, or an equation; "
    "edit text inside one run so its formatting is kept"
)
NOT_REWRITABLE = "Block {ref} contains {what} that cannot be written back, so its text cannot be edited"
NOT_TEXT_BLOCK = "Block {ref} is a {kind} block; only text blocks can be edited"
NOT_TO_DO = "Block {ref} is a {kind} block; only to-do blocks can be checked or unchecked"
NOTHING_TO_CHANGE = "Give old_text and new_text, or checked, or both"
NEEDS_BOTH_TEXTS = "old_text and new_text must be given together"
SAME_TEXT = "old_text and new_text are the same"
ALREADY_CHECKED = "Block {ref} is already {state}"
TOO_MANY_RUNS = "Block {ref} would need more than 100 rich text runs"


class NotionToolError(Exception):
    """A refusal or failure whose message is safe to show the model."""


class _Rejected(Exception):
    """Notion answered and refused the request, so it was not carried out."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class _Unsent(Exception):
    """The request never left: the connection did not open."""


class _Budget(Exception):
    """The request was not sent because the call ran out of time or requests."""


class _Uncertain(Exception):
    """The request may have reached Notion, but no usable answer came back."""


def notion_id(value: Any) -> Optional[str]:
    """A Notion ID in dashed lowercase form, from a bare or dashed ID or a notion.so/notion.site URL."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    compact = text.replace("-", "")
    if not _HEX_ID.fullmatch(compact):
        parsed = urlparse(text)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (host in {"notion.so", "notion.site"}
                                           or host.endswith((".notion.so", ".notion.site"))):
            return None
        match = _TRAILING_HEX_ID.search(parsed.path.rstrip("/").replace("-", ""))
        if not match:
            return None
        compact = match.group(1)
    return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"


def parse_pages(value: Optional[str]) -> Optional[dict[str, str]]:
    """``DAVE_NOTION_PAGES`` as {name: dashed page ID}; ``None`` when it is not a valid object."""
    try:
        data = json.loads(value or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or not data or len(data) > NOTION_MAX_PAGES:
        return None
    pages: dict[str, str] = {}
    for name, reference in data.items():
        page_id = notion_id(reference)
        if not isinstance(name, str) or not PAGE_NAME.fullmatch(name) or page_id is None:
            return None
        pages[name] = page_id
    if len(set(pages.values())) != len(pages):
        return None
    return pages


@dataclass(frozen=True)
class NotionSettings:
    """What the operator configured; the token is never part of ``repr``."""

    token: Optional[str] = field(default=None, repr=False)
    pages: Mapping[str, str] = field(default_factory=dict)
    pages_invalid: bool = False

    @classmethod
    def from_values(cls, token: Optional[str], pages: Optional[str]) -> "NotionSettings":
        token = (token or "").strip() or None
        if not (pages or "").strip():
            return cls(token=token)
        parsed = parse_pages(pages)
        return cls(token=token, pages=parsed or {}, pages_invalid=parsed is None)

    def page(self, name: Any) -> tuple[str, str]:
        """The configured (name, page ID) for ``name``, or a refusal naming the configured pages."""
        if not self.token:
            raise NotionToolError(NO_TOKEN)
        if self.pages_invalid:
            raise NotionToolError(BAD_PAGES)
        if not self.pages:
            raise NotionToolError(NO_PAGES)
        if isinstance(name, str) and name in self.pages:
            return name, self.pages[name]
        shown = f" '{name}'" if isinstance(name, str) and PAGE_NAME.fullmatch(name) else ""
        raise NotionToolError(f"Unknown Notion page{shown}. Configured pages: {', '.join(sorted(self.pages))}")


@dataclass
class BlockRecord:
    """What a run saw of one block; the model only ever holds ``ref``."""

    ref: str
    page: str
    page_id: str
    block_id: str
    block_type: str
    rich_text: Optional[list[Any]]
    checked: Optional[bool]


class RunLedger:
    """Refs, block snapshots, and append outcomes for one tool run."""

    def __init__(self, max_refs: int = NOTION_RUN_MAX_REFS) -> None:
        self._lock = threading.Lock()
        self._max_refs = max_refs
        self._records: dict[str, BlockRecord] = {}
        self._refs_by_block: dict[str, str] = {}
        self._appends: dict[tuple[str, str], str] = {}

    def remember(self, page: str, page_id: str, block: Mapping[str, Any]) -> BlockRecord:
        """Record (or refresh) ``block`` and return its record; one ref per block for the whole run."""
        block_id = notion_id(block.get("id"))
        if block_id is None:
            raise NotionToolError(UNREACHABLE)
        kind = str(block.get("type") or "unsupported")
        with self._lock:
            ref = self._refs_by_block.get(block_id)
            if ref is None:
                if len(self._records) >= self._max_refs:
                    raise NotionToolError(LEDGER_FULL)
                ref = f"b{len(self._records) + 1}"
                self._refs_by_block[block_id] = ref
            record = BlockRecord(ref, page, page_id, block_id, kind, _copy_json(block_rich_text(block)),
                                 block_checked(block))
            self._records[ref] = record
            return record

    def get(self, ref: Any) -> Optional[BlockRecord]:
        with self._lock:
            return self._records.get(ref) if isinstance(ref, str) else None

    def append_outcome(self, page: str, digest: str) -> Optional[str]:
        with self._lock:
            return self._appends.get((page, digest))

    def set_append_outcome(self, page: str, digest: str, outcome: str) -> None:
        with self._lock:
            # A verified or failed attempt never clears an earlier unknown one for the same content.
            if self._appends.get((page, digest)) == "unknown" and outcome != "unknown":
                return
            self._appends[(page, digest)] = outcome


class NotionLedgers:
    """One ledger per tool run; the host drops a run's ledger when the run leaves its store."""

    def __init__(self, max_runs: int = NOTION_MAX_RUNS) -> None:
        self._lock = threading.Lock()
        self._max_runs = max_runs
        self._ledgers: OrderedDict[str, RunLedger] = OrderedDict()

    def for_run(self, run_id: str) -> RunLedger:
        with self._lock:
            ledger = self._ledgers.get(run_id)
            if ledger is None:
                ledger = self._ledgers[run_id] = RunLedger()
                while len(self._ledgers) > self._max_runs:
                    self._ledgers.popitem(last=False)
            else:
                self._ledgers.move_to_end(run_id)
            return ledger

    def drop(self, run_id: str) -> None:
        with self._lock:
            self._ledgers.pop(run_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._ledgers)


class PageWriteGuard:
    """At most one DaveLLM write per Notion page at a time, across runs, in this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    def acquire(self, page_id: str) -> bool:
        with self._lock:
            if page_id in self._busy:
                return False
            self._busy.add(page_id)
            return True

    def release(self, page_id: str) -> None:
        with self._lock:
            self._busy.discard(page_id)

    def busy(self, page_id: str) -> bool:
        with self._lock:
            return page_id in self._busy


# Shielded write tasks outlive a cancelled caller; hold them so they are not collected.
_WRITES_IN_FLIGHT: set[asyncio.Task[Any]] = set()


def _copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value)) if value is not None else None


def _safe_code(value: Any) -> str:
    return value if isinstance(value, str) and _ERROR_CODE.fullmatch(value) else "error"


def block_rich_text(block: Mapping[str, Any]) -> Optional[list[Any]]:
    """The block's rich text array when its type carries one."""
    kind = block.get("type")
    body = block.get(kind) if isinstance(kind, str) else None
    rich = body.get("rich_text") if isinstance(body, dict) else None
    if kind in TEXT_BLOCK_TYPES and isinstance(rich, list):
        return [item for item in rich]
    return None


def block_checked(block: Mapping[str, Any]) -> Optional[bool]:
    body = block.get("to_do")
    if block.get("type") == "to_do" and isinstance(body, dict):
        return bool(body.get("checked"))
    return None


def _item_text(item: Mapping[str, Any]) -> str:
    """The text a run shows: content for text runs, Notion's plain text for mentions and equations."""
    if item.get("type") == "text":
        text = item.get("text")
        return str(text.get("content") or "") if isinstance(text, dict) else ""
    return str(item.get("plain_text") or "")


def rich_text_plain(items: Optional[Sequence[Any]]) -> str:
    return "".join(_item_text(item) for item in items or () if isinstance(item, dict))


def _annotations(item: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    given = item.get("annotations")
    given = given if isinstance(given, dict) else {}
    return tuple((key, given.get(key, default)) for key, default in _ANNOTATION_DEFAULTS)


def _link(item: Mapping[str, Any]) -> Optional[str]:
    text = item.get("text")
    link = text.get("link") if isinstance(text, dict) else None
    url = link.get("url") if isinstance(link, dict) else None
    return url if isinstance(url, str) else None


def _mention_identity(item: Mapping[str, Any]) -> str:
    mention = item.get("mention")
    mention = mention if isinstance(mention, dict) else {}
    kind = mention.get("type")
    target = mention.get(kind) if isinstance(kind, str) else None
    target = target if isinstance(target, dict) else {}
    if kind in {"page", "database", "user"}:
        return f"{kind}:{notion_id(target.get('id'))}"
    if kind == "date":
        return "date:" + json.dumps({key: target.get(key) for key in ("start", "end", "time_zone")}, sort_keys=True)
    return f"{kind}:" + json.dumps(target, sort_keys=True, default=str)


def _run_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    kind = item.get("type")
    if kind == "text":
        return ("text", _item_text(item), _annotations(item), _link(item))
    if kind == "equation":
        equation = item.get("equation")
        expression = equation.get("expression") if isinstance(equation, dict) else ""
        return ("equation", str(expression or ""), _annotations(item), None)
    if kind == "mention":
        return ("mention", _mention_identity(item), _annotations(item), None)
    return ("other", json.dumps(item, sort_keys=True, default=str), (), None)


def rich_text_signature(items: Optional[Sequence[Any]]) -> tuple[tuple[Any, ...], ...]:
    """Rich text as comparable runs: adjacent text with the same formatting and link merges, empty text drops.

    Notion may split or join runs differently from how they were sent, so outcomes are
    compared on this form rather than on raw arrays.
    """
    runs: list[tuple[Any, ...]] = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        key = _run_key(item)
        if key[0] == "text" and key[1] == "":
            continue
        if key[0] == "text" and runs and runs[-1][0] == "text" and runs[-1][2:] == key[2:]:
            runs[-1] = ("text", runs[-1][1] + key[1], key[2], key[3])
        else:
            runs.append(key)
    return tuple(runs)


def _writable_item(item: Mapping[str, Any], ref: str) -> dict[str, Any]:
    """One response rich text item as a request item, or a refusal when it cannot round-trip."""
    kind = item.get("type")
    if kind == "text":
        url = _link(item)
        if url is not None and not re.match(r"https?://", url):
            raise NotionToolError(NOT_REWRITABLE.format(ref=ref, what="an internal link"))
        text: dict[str, Any] = {"content": _item_text(item)}
        if url is not None:
            text["link"] = {"url": url}
        entry: dict[str, Any] = {"type": "text", "text": text}
    elif kind == "equation":
        equation = item.get("equation")
        expression = equation.get("expression") if isinstance(equation, dict) else None
        if not isinstance(expression, str):
            raise NotionToolError(NOT_REWRITABLE.format(ref=ref, what="an equation"))
        entry = {"type": "equation", "equation": {"expression": expression}}
    elif kind == "mention":
        mention = item.get("mention")
        mention = mention if isinstance(mention, dict) else {}
        mention_kind = mention.get("type")
        target = mention.get(mention_kind) if isinstance(mention_kind, str) else None
        if mention_kind not in WRITABLE_MENTIONS or not isinstance(target, dict):
            raise NotionToolError(NOT_REWRITABLE.format(
                ref=ref, what=f"a {_safe_code(mention_kind)} mention"))
        if mention_kind == "date":
            value = {key: target[key] for key in ("start", "end", "time_zone") if target.get(key) is not None}
            if not isinstance(value.get("start"), str):
                raise NotionToolError(NOT_REWRITABLE.format(ref=ref, what="a date mention"))
        else:
            target_id = notion_id(target.get("id"))
            if target_id is None:
                raise NotionToolError(NOT_REWRITABLE.format(ref=ref, what=f"a {mention_kind} mention"))
            value = {"id": target_id}
        entry = {"type": "mention", "mention": {"type": mention_kind, mention_kind: value}}
    else:
        raise NotionToolError(NOT_REWRITABLE.format(ref=ref, what="rich text"))
    annotations = dict(_annotations(item))
    if any(annotations[key] != default for key, default in _ANNOTATION_DEFAULTS):
        entry["annotations"] = annotations
    return entry


def _split_long_text(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Text runs longer than Notion's per-run limit become consecutive runs with the same formatting."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        content = entry["text"]["content"] if entry["type"] == "text" else None
        if content is None or len(content) <= NOTION_TEXT_MAX_CHARS:
            out.append(entry)
            continue
        for start in range(0, len(content), NOTION_TEXT_MAX_CHARS):
            piece = json.loads(json.dumps(entry))
            piece["text"]["content"] = content[start:start + NOTION_TEXT_MAX_CHARS]
            out.append(piece)
    return out


def rewrite_rich_text(items: Sequence[Any], old: str, new: str, ref: str) -> list[dict[str, Any]]:
    """``items`` as request items with the one occurrence of ``old`` replaced by ``new``.

    Every other run is sent back with its formatting, links, mentions, and equations.
    The occurrence must sit inside one run of plain or uniformly formatted text.
    """
    pairs: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            raise NotionToolError(NOT_REWRITABLE.format(ref=ref, what="rich text"))
        entry = _writable_item(item, ref)
        text = _item_text(item)
        if (entry["type"] == "text" and pairs and pairs[-1][1]["type"] == "text"
                and pairs[-1][1].get("annotations") == entry.get("annotations")
                and pairs[-1][1]["text"].get("link") == entry["text"].get("link")):
            merged = pairs[-1][1]
            merged["text"]["content"] += entry["text"]["content"]
            pairs[-1] = (pairs[-1][0] + text, merged)
        else:
            pairs.append((text, entry))
    full = "".join(text for text, _ in pairs)
    count = full.count(old)
    if count == 0:
        raise NotionToolError(OLD_TEXT_NOT_FOUND.format(ref=ref))
    if count > 1:
        raise NotionToolError(OLD_TEXT_REPEATED.format(count=count, ref=ref))
    start = full.index(old)
    end = start + len(old)
    offset = 0
    for index, (text, entry) in enumerate(pairs):
        if entry["type"] == "text" and offset <= start and end <= offset + len(text):
            local = start - offset
            entry["text"]["content"] = text[:local] + new + text[local + len(old):]
            entries = [item for _, item in pairs]
            if entry["text"]["content"] == "":
                del entries[index]
            entries = _split_long_text(entries)
            if len(entries) > NOTION_RICH_TEXT_MAX_ITEMS:
                raise NotionToolError(TOO_MANY_RUNS.format(ref=ref))
            return entries
        offset += len(text)
    raise NotionToolError(CROSSES_RUNS.format(ref=ref))


def plain_rich_text(text: str) -> list[dict[str, Any]]:
    return _split_long_text([{"type": "text", "text": {"content": text}}]) if text else []


def _retry_after(headers: httpx.Headers) -> Optional[float]:
    try:
        seconds = float(headers.get("retry-after", ""))
    except ValueError:
        return None
    return seconds if 0 <= seconds <= NOTION_RATE_LIMIT_MAX_WAIT_SECONDS else None


class NotionApi:
    """A client bound to the fixed Notion API, one call's deadline, and one call's request budget."""

    def __init__(self, token: str, *, budget_seconds: float, max_requests: int) -> None:
        self._client = httpx.AsyncClient(
            base_url=_API_BASE,
            follow_redirects=False,
            trust_env=False,
            timeout=NOTION_REQUEST_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Accept": "application/json",
            },
        )
        self.deadline = time.monotonic() + budget_seconds
        self.max_requests = max_requests
        self.requests = 0

    async def __aenter__(self) -> "NotionApi":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self._client.aclose()

    async def call(self, method: str, path: str, *, params: Optional[dict[str, Any]] = None,
                   body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """One API call; a 429 with a short Retry-After is retried once, because Notion did not carry it out."""
        for attempt in range(2):
            remaining = self.deadline - time.monotonic()
            if remaining < NOTION_MIN_REQUEST_SECONDS or self.requests >= self.max_requests:
                raise _Budget()
            self.requests += 1
            status, headers, payload = await self._send(method, path, params, body,
                                                        min(NOTION_REQUEST_TIMEOUT_SECONDS, remaining))
            if 200 <= status < 300:
                if not isinstance(payload, dict):
                    raise _Uncertain()
                return payload
            code = _safe_code(payload.get("code")) if isinstance(payload, dict) else "error"
            if status == 429 and attempt == 0:
                wait = _retry_after(headers)
                if wait is not None and time.monotonic() + wait + NOTION_MIN_REQUEST_SECONDS < self.deadline:
                    await asyncio.sleep(wait)
                    continue
            if 400 <= status < 500:
                raise _Rejected(status, code)
            raise _Uncertain()  # 3xx (never followed) and 5xx do not prove the request was dropped
        raise _Uncertain()

    async def _send(self, method: str, path: str, params: Optional[dict[str, Any]],
                    body: Optional[dict[str, Any]], timeout: float) -> tuple[int, httpx.Headers, Any]:
        try:
            async with self._client.stream(method, path, params=params, json=body,
                                           timeout=httpx.Timeout(timeout)) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > NOTION_MAX_RESPONSE_BYTES:
                        raise _Uncertain()
                try:
                    payload = json.loads(bytes(data)) if data else None
                except ValueError:
                    payload = None
                return response.status_code, response.headers, payload
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise _Unsent() from None
        except httpx.HTTPError:
            raise _Uncertain() from None


def _require(settings: NotionSettings, ledger: Optional[RunLedger], page: Any) -> tuple[RunLedger, str, str]:
    if ledger is None:
        raise NotionToolError(NOT_IN_RUN)
    name, page_id = settings.page(page)
    return ledger, name, page_id


def _read_error(exc: Exception) -> NotionToolError:
    if isinstance(exc, _Rejected):
        return NotionToolError(READ_FAILED.format(code=exc.code))
    return NotionToolError(UNREACHABLE)


def _page_title(page: Mapping[str, Any]) -> str:
    properties = page.get("properties")
    for value in (properties.values() if isinstance(properties, dict) else ()):
        if isinstance(value, dict) and value.get("type") == "title":
            return rich_text_plain(value.get("title") if isinstance(value.get("title"), list) else [])
    return ""


async def _list_children(api: NotionApi, parent_id: str, *, max_pages: int) -> tuple[list[dict[str, Any]], bool]:
    """Children of ``parent_id`` in order, and whether more remained after ``max_pages`` pages."""
    blocks: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"page_size": NOTION_PAGE_SIZE}
        if cursor:
            params["start_cursor"] = cursor
        data = await api.call("GET", f"/blocks/{parent_id}/children", params=params)
        results = data.get("results")
        for block in results if isinstance(results, list) else ():
            if isinstance(block, dict) and notion_id(block.get("id")) and not block.get("in_trash"):
                blocks.append(block)
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not isinstance(cursor, str) or not cursor:
            return blocks, False
    return blocks, True


async def _collect(api: NotionApi, page_id: str) -> tuple[list[tuple[int, dict[str, Any]]], bool]:
    """The page's blocks in document order with their depth, up to the read limits."""
    found: list[tuple[int, dict[str, Any]]] = []
    truncated = False

    async def walk(parent_id: str, depth: int) -> None:
        nonlocal truncated
        try:
            children, more = await _list_children(api, parent_id, max_pages=NOTION_LIST_MAX_PAGES)
        except _Budget:
            truncated = True
            return
        truncated = truncated or more
        for block in children:
            if truncated and len(found) >= NOTION_READ_MAX_BLOCKS:
                return
            if len(found) >= NOTION_READ_MAX_BLOCKS:
                truncated = True
                return
            found.append((depth, block))
            if (block.get("has_children") and depth + 1 < NOTION_READ_MAX_DEPTH
                    and block.get("type") not in _NO_DESCEND):
                await walk(str(notion_id(block.get("id"))), depth + 1)

    await walk(page_id, 0)
    return found, truncated


def _display(block: Mapping[str, Any]) -> str:
    kind = block.get("type")
    rich = block_rich_text(block)
    if rich is not None:
        return rich_text_plain(rich)
    body = block.get(kind) if isinstance(kind, str) else None
    body = body if isinstance(body, dict) else {}
    if kind in {"child_page", "child_database"}:
        return str(body.get("title") or "")
    if kind == "table_row":
        cells = body.get("cells")
        return " | ".join(rich_text_plain(cell) for cell in (cells if isinstance(cells, list) else ())
                          if isinstance(cell, list))
    if kind == "equation":
        return str(body.get("expression") or "")
    return ""


def _describe(record: BlockRecord, block: Mapping[str, Any], depth: int) -> dict[str, Any]:
    entry: dict[str, Any] = {"ref": record.ref, "type": record.block_type}
    if depth:
        entry["depth"] = depth
    if record.checked is not None:
        entry["checked"] = record.checked
    text = _display(block)
    if len(text) > NOTION_DISPLAY_MAX_CHARS:
        entry["text"] = text[:NOTION_DISPLAY_MAX_CHARS]
        entry["clipped"] = True
    elif text:
        entry["text"] = text
    if record.rich_text is not None:
        runs = rich_text_signature(record.rich_text)
        if any(run[0] != "text" or run[3] is not None or run[2] != _ANNOTATION_DEFAULTS for run in runs):
            entry["formatted"] = True
        try:
            for item in record.rich_text:
                _writable_item(item, record.ref)
        except NotionToolError:
            entry["text_editable"] = False
    return entry


def _encoded(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


async def read_page(arguments: Mapping[str, Any], *, settings: NotionSettings,
                    ledger: Optional[RunLedger]) -> dict[str, Any]:
    """notion.page.read: the page title and its blocks, each with a ref for later edits in this run."""
    run, name, page_id = _require(settings, ledger, arguments.get("page"))
    assert settings.token is not None
    try:
        async with NotionApi(settings.token, budget_seconds=NOTION_READ_BUDGET_SECONDS,
                             max_requests=NOTION_READ_MAX_REQUESTS) as api:
            page = await api.call("GET", f"/pages/{page_id}")
            if page.get("in_trash"):
                raise NotionToolError(f"Notion page '{name}' is in the trash")
            title = _page_title(page)
            blocks, truncated = await _collect(api, page_id)
    except (_Rejected, _Unsent, _Uncertain, _Budget) as exc:
        raise _read_error(exc) from None
    entries = [_describe(run.remember(name, page_id, block), block, depth) for depth, block in blocks]
    payload: dict[str, Any] = {"page": name, "title": title, "blocks": entries, "truncated": truncated}
    while entries and _encoded(payload) > NOTION_OUTPUT_BUDGET_BYTES:
        entries.pop()
        payload["truncated"] = True
    return payload


@dataclass
class _Outcome:
    kind: str  # verified | failed | unknown
    detail: str
    payload: dict[str, Any] = field(default_factory=dict)


def _finish(outcome: _Outcome) -> dict[str, Any]:
    if outcome.kind == "verified":
        return {"outcome": "verified", **outcome.payload}
    if outcome.kind == "failed":
        raise NotionToolError(f"Nothing was written: {outcome.detail}")
    raise NotionToolError(
        f"Outcome unknown: {outcome.detail}. Do not repeat this write; read the page to check what happened."
    )


async def _shielded(guard: PageWriteGuard, page_id: str, work: Coroutine[Any, Any, _Outcome]) -> _Outcome:
    """Run ``work`` under the page guard, shielded so a cancelled caller cannot cut it off midway."""
    if not guard.acquire(page_id):
        work.close()
        raise NotionToolError(PAGE_BUSY)

    async def guarded() -> _Outcome:
        try:
            return await work
        finally:
            guard.release(page_id)

    task = asyncio.ensure_future(guarded())
    _WRITES_IN_FLIGHT.add(task)
    task.add_done_callback(_WRITES_IN_FLIGHT.discard)
    return await asyncio.shield(task)


def _append_blocks_argument(value: Any) -> list[tuple[str, str, Optional[bool]]]:
    if not isinstance(value, list) or not value:
        raise NotionToolError("blocks must list at least one block")
    if len(value) > NOTION_APPEND_MAX_BLOCKS:
        raise NotionToolError(f"blocks may list at most {NOTION_APPEND_MAX_BLOCKS} blocks")
    blocks = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise NotionToolError(f"Block {index} must be an object")
        kind, text, checked = item.get("type"), item.get("text"), item.get("checked")
        if kind not in APPEND_BLOCK_TYPES:
            raise NotionToolError(f"Block {index} has an unsupported type")
        if not isinstance(text, str):
            raise NotionToolError(f"Block {index} needs text")
        if len(text) > NOTION_TEXT_MAX_CHARS:
            raise NotionToolError(f"Block {index} text exceeds {NOTION_TEXT_MAX_CHARS} characters")
        if checked is not None and kind != "to_do":
            raise NotionToolError(f"Block {index} is not a to_do, so it cannot be checked")
        if checked is not None and not isinstance(checked, bool):
            raise NotionToolError(f"Block {index} checked must be true or false")
        blocks.append((kind, text, bool(checked) if kind == "to_do" else None))
    return blocks


def _append_digest(page: str, blocks: Sequence[tuple[str, str, Optional[bool]]]) -> str:
    canonical = json.dumps([page, [list(block) for block in blocks]], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _child(kind: str, text: str, checked: Optional[bool]) -> dict[str, Any]:
    body: dict[str, Any] = {"rich_text": plain_rich_text(text)}
    if kind == "to_do":
        body["checked"] = bool(checked)
    return {"object": "block", "type": kind, kind: body}


def _matches_request(block: Mapping[str, Any], request: tuple[str, str, Optional[bool]]) -> bool:
    kind, text, checked = request
    return (block.get("type") == kind
            and rich_text_signature(block_rich_text(block)) == rich_text_signature(plain_rich_text(text))
            and block_checked(block) == checked)


async def _bot_id(api: NotionApi) -> Optional[str]:
    """The connection's own bot user, asked fresh each time it is needed (only after an uncertain append)."""
    me = await api.call("GET", "/users/me")
    return notion_id(me.get("id"))


async def _append_work(token: str, run: RunLedger, name: str, page_id: str,
                       blocks: list[tuple[str, str, Optional[bool]]], digest: str) -> _Outcome:
    outcome = _Outcome("unknown", "the append did not finish")
    try:
        outcome = await _append_attempt(token, run, name, page_id, blocks)
    except Exception:
        outcome = _Outcome("unknown", "the append did not finish")
    finally:
        run.set_append_outcome(name, digest, outcome.kind)
    return outcome


async def _append_attempt(token: str, run: RunLedger, name: str, page_id: str,
                          blocks: list[tuple[str, str, Optional[bool]]]) -> _Outcome:
    async with NotionApi(token, budget_seconds=NOTION_WRITE_BUDGET_SECONDS,
                         max_requests=NOTION_WRITE_MAX_REQUESTS) as api:
        try:
            before, more = await _list_children(api, page_id, max_pages=NOTION_LIST_MAX_PAGES)
        except _Rejected as exc:
            return _Outcome("failed", f"Notion refused to read the page before writing ({exc.code})")
        except (_Unsent, _Uncertain, _Budget):
            return _Outcome("failed", "Notion could not be reached to read the page before writing")
        if more:
            return _Outcome("failed", "the page has more top-level blocks than this tool can check after an append")
        body = {"children": [_child(*block) for block in blocks], "position": {"type": "end"}}
        try:
            response = await api.call("PATCH", f"/blocks/{page_id}/children", body=body)
        except _Rejected as exc:
            return _Outcome("failed", f"Notion refused the append ({exc.code})")
        except (_Unsent, _Budget):
            return _Outcome("failed", "the request could not be sent to Notion")
        except _Uncertain:
            return await _reconcile_append(api, run, name, page_id, blocks, before)
        results = response.get("results")
        created = [notion_id(item.get("id")) for item in results if isinstance(item, dict)] \
            if isinstance(results, list) else []
        try:
            after, _ = await _list_children(api, page_id, max_pages=NOTION_LIST_MAX_PAGES)
        except (_Rejected, _Unsent, _Uncertain, _Budget):
            return _Outcome("unknown", "Notion accepted the append, but reading the page back failed")
        by_id = {notion_id(block.get("id")): block for block in after}
        saved = [by_id[block_id] for block_id in created if block_id in by_id]
        if (len(created) == len(saved) == len(blocks)
                and all(_matches_request(block, request) for block, request in zip(saved, blocks))):
            return _verified_append(run, name, page_id, saved)
        return _Outcome("unknown", "Notion accepted the append, but the page does not show exactly those blocks")


async def _reconcile_append(api: NotionApi, run: RunLedger, name: str, page_id: str,
                            blocks: list[tuple[str, str, Optional[bool]]],
                            before: list[dict[str, Any]]) -> _Outcome:
    """After an uncertain append, look for exactly these blocks, by this connection, right after the old end.

    Server timestamps are not compared with the local clock: a block that follows the
    old last block was created after the baseline read, whatever either clock says.
    """
    try:
        after, _ = await _list_children(api, page_id, max_pages=NOTION_LIST_MAX_PAGES)
        bot = await _bot_id(api)
    except (_Rejected, _Unsent, _Uncertain, _Budget):
        return _Outcome("unknown", "the connection to Notion failed during the append and the page could not be checked")
    ids = [notion_id(block.get("id")) for block in after]
    tail = notion_id(before[-1].get("id")) if before else None
    if tail is not None and tail not in ids:
        return _Outcome("unknown", "the connection to Notion failed during the append and the page changed meanwhile")
    added = after[ids.index(tail) + 1:] if tail is not None else after
    if not added:
        return _Outcome("unknown", "the connection to Notion failed during the append and the blocks are not on the page yet")
    candidate = added[:len(blocks)]
    if (bot is not None and len(candidate) == len(blocks)
            and all(_matches_request(block, request) for block, request in zip(candidate, blocks))
            and all(_created_by(block) == bot for block in candidate)):
        return _verified_append(run, name, page_id, candidate)
    return _Outcome("unknown", "the connection to Notion failed during the append and the new blocks do not match exactly")


def _created_by(block: Mapping[str, Any]) -> Optional[str]:
    author = block.get("created_by")
    return notion_id(author.get("id")) if isinstance(author, dict) else None


def _verified_append(run: RunLedger, name: str, page_id: str, saved: Sequence[Any]) -> _Outcome:
    refs = [run.remember(name, page_id, block).ref for block in saved]
    return _Outcome("verified", "", {"page": name, "blocks_added": len(refs), "refs": refs})


async def append_blocks(arguments: Mapping[str, Any], *, settings: NotionSettings, ledger: Optional[RunLedger],
                        guard: PageWriteGuard) -> dict[str, Any]:
    """notion.page.append: add plain-text blocks to the end of a configured page and verify them."""
    run, name, page_id = _require(settings, ledger, arguments.get("page"))
    assert settings.token is not None
    blocks = _append_blocks_argument(arguments.get("blocks"))
    digest = _append_digest(name, blocks)
    if run.append_outcome(name, digest) in {"unknown", "in_flight"}:
        raise NotionToolError(REPEAT_REFUSED.format(page=name))
    work = _append_work(settings.token, run, name, page_id, blocks, digest)
    run.set_append_outcome(name, digest, "in_flight")
    try:
        outcome = await _shielded(guard, page_id, work)
    except NotionToolError:
        run.set_append_outcome(name, digest, "failed")
        raise
    return _finish(outcome)


def _edit_plan(record: BlockRecord, arguments: Mapping[str, Any]) -> tuple[Optional[list[dict[str, Any]]], Optional[bool]]:
    """The new rich text and checked state from the run's snapshot, or a refusal before anything is sent."""
    old, new, checked = arguments.get("old_text"), arguments.get("new_text"), arguments.get("checked")
    if old is None and new is None and checked is None:
        raise NotionToolError(NOTHING_TO_CHANGE)
    if (old is None) != (new is None):
        raise NotionToolError(NEEDS_BOTH_TEXTS)
    rich_text = None
    if old is not None:
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise NotionToolError(NEEDS_BOTH_TEXTS)
        if old == new:
            raise NotionToolError(SAME_TEXT)
        if record.rich_text is None:
            raise NotionToolError(NOT_TEXT_BLOCK.format(ref=record.ref, kind=record.block_type))
        rich_text = rewrite_rich_text(record.rich_text, old, new, record.ref)
    if checked is not None:
        if not isinstance(checked, bool):
            raise NotionToolError("checked must be true or false")
        if record.block_type != "to_do":
            raise NotionToolError(NOT_TO_DO.format(ref=record.ref, kind=record.block_type))
        if rich_text is None and checked == record.checked:
            raise NotionToolError(ALREADY_CHECKED.format(ref=record.ref, state="checked" if checked else "unchecked"))
    return rich_text, checked


async def _owning_page(api: NotionApi, block: Mapping[str, Any]) -> Optional[str]:
    """The page a block sits on now, walking parent blocks; ``None`` past the depth limit or off-page."""
    parent = block.get("parent")
    for _ in range(NOTION_PARENT_MAX_DEPTH):
        parent = parent if isinstance(parent, dict) else {}
        if parent.get("type") == "page_id":
            return notion_id(parent.get("page_id"))
        parent_id = notion_id(parent.get("block_id")) if parent.get("type") == "block_id" else None
        if parent_id is None:
            return None
        above = await api.call("GET", f"/blocks/{parent_id}")
        if above.get("in_trash"):
            return None
        parent = above.get("parent")
    return None


def _state(block: Mapping[str, Any]) -> tuple[Any, ...]:
    return (block.get("type"), rich_text_signature(block_rich_text(block)), block_checked(block))


async def _update_work(token: str, run: RunLedger, record: BlockRecord,
                       rich_text: Optional[list[dict[str, Any]]], checked: Optional[bool]) -> _Outcome:
    try:
        return await _update_attempt(token, run, record, rich_text, checked)
    except Exception:
        return _Outcome("unknown", "the edit did not finish")


async def _update_attempt(token: str, run: RunLedger, record: BlockRecord,
                          rich_text: Optional[list[dict[str, Any]]], checked: Optional[bool]) -> _Outcome:
    seen = (record.block_type, rich_text_signature(record.rich_text), record.checked)
    target = (record.block_type,
              rich_text_signature(rich_text) if rich_text is not None else seen[1],
              checked if checked is not None else record.checked)
    async with NotionApi(token, budget_seconds=NOTION_WRITE_BUDGET_SECONDS,
                         max_requests=NOTION_WRITE_MAX_REQUESTS) as api:
        try:
            current = await api.call("GET", f"/blocks/{record.block_id}")
            if current.get("in_trash"):
                return _Outcome("failed", f"block {record.ref} is in the trash")
            if await _owning_page(api, current) != record.page_id:
                return _Outcome("failed", f"block {record.ref} is no longer on page '{record.page}'")
        except _Rejected as exc:
            return _Outcome("failed", f"Notion refused to read block {record.ref} before writing ({exc.code})")
        except (_Unsent, _Uncertain, _Budget):
            return _Outcome("failed", f"Notion could not be reached to check block {record.ref} before writing")
        if _state(current) != seen:
            return _Outcome("failed", f"block {record.ref} changed after it was read; read the page again")
        body: dict[str, Any] = {}
        if rich_text is not None:
            body["rich_text"] = rich_text
        if checked is not None:
            body["checked"] = checked
        try:
            await api.call("PATCH", f"/blocks/{record.block_id}", body={record.block_type: body})
            accepted = True
        except _Rejected as exc:
            return _Outcome("failed", f"Notion refused the edit ({exc.code})")
        except (_Unsent, _Budget):
            return _Outcome("failed", "the request could not be sent to Notion")
        except _Uncertain:
            accepted = False
        try:
            saved = await api.call("GET", f"/blocks/{record.block_id}")
        except (_Rejected, _Unsent, _Uncertain, _Budget):
            if accepted:
                return _Outcome("unknown", "Notion accepted the edit, but reading the block back failed")
            return _Outcome("unknown", "the connection to Notion failed during the edit and the block could not be checked")
        if _state(saved) == target and not saved.get("in_trash"):
            fresh = run.remember(record.page, record.page_id, saved)
            payload: dict[str, Any] = {"page": record.page, "block": fresh.ref, "text": _display(saved)[:NOTION_DISPLAY_MAX_CHARS]}
            if fresh.checked is not None:
                payload["checked"] = fresh.checked
            return _Outcome("verified", "", payload)
        if accepted:
            return _Outcome("unknown", f"Notion accepted the edit, but block {record.ref} now reads differently")
        if _state(saved) == seen:
            return _Outcome("unknown", f"the connection to Notion failed during the edit and block {record.ref} still shows the old content")
        return _Outcome("unknown", f"the connection to Notion failed during the edit and block {record.ref} changed")


async def update_block(arguments: Mapping[str, Any], *, settings: NotionSettings, ledger: Optional[RunLedger],
                       guard: PageWriteGuard) -> dict[str, Any]:
    """notion.block.update: replace text inside one block and/or check a to-do, then verify it."""
    run, name, page_id = _require(settings, ledger, arguments.get("page"))
    assert settings.token is not None
    ref = arguments.get("block")
    record = run.get(ref)
    if record is None:
        shown = ref if isinstance(ref, str) and BLOCK_REF.fullmatch(ref) else "(invalid)"
        raise NotionToolError(UNKNOWN_REF.format(ref=shown))
    if record.page != name or record.page_id != page_id:
        raise NotionToolError(OTHER_PAGE_REF.format(ref=record.ref, actual=record.page, requested=name))
    rich_text, checked = _edit_plan(record, arguments)
    return _finish(await _shielded(guard, page_id, _update_work(settings.token, run, record, rich_text, checked)))
