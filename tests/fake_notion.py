"""A stateful stand-in for the parts of the Notion API the adapter uses, mounted with respx.

It keeps pages, blocks, and child order; answers the same shapes as Notion (rich text
with plain_text and full annotations, minute-rounded timestamps, created_by, cursor
pagination); validates the request limits the adapter must respect; and lets a test
inject one fault per matching request: a refusal, a rate limit, a lost connection
before or after the change is applied, a server error after applying, or a redirect.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx
import respx

API = "https://api.notion.com/v1"
ANNOTATIONS = {"bold": False, "italic": False, "strikethrough": False, "underline": False,
               "code": False, "color": "default"}
_ID = r"[0-9a-f-]{36}"


def new_id() -> str:
    return str(uuid.uuid4())


def rt(text: str, *, link: Optional[str] = None, **annotations: Any) -> dict[str, Any]:
    """A rich text item as Notion returns it."""
    return {
        "type": "text",
        "text": {"content": text, "link": {"url": link} if link else None},
        "annotations": {**ANNOTATIONS, **annotations},
        "plain_text": text,
        "href": link,
    }


def page_mention(page_id: str, title: str, **annotations: Any) -> dict[str, Any]:
    return {
        "type": "mention",
        "mention": {"type": "page", "page": {"id": page_id}},
        "annotations": {**ANNOTATIONS, **annotations},
        "plain_text": title,
        "href": f"https://www.notion.so/{page_id.replace('-', '')}",
    }


def user_mention(user_id: str, name: str) -> dict[str, Any]:
    return {
        "type": "mention",
        "mention": {"type": "user", "user": {"object": "user", "id": user_id, "name": name}},
        "annotations": dict(ANNOTATIONS),
        "plain_text": f"@{name}",
        "href": None,
    }


def date_mention(start: str) -> dict[str, Any]:
    return {
        "type": "mention",
        "mention": {"type": "date", "date": {"start": start, "end": None, "time_zone": None}},
        "annotations": dict(ANNOTATIONS),
        "plain_text": start,
        "href": None,
    }


def link_preview_mention(url: str) -> dict[str, Any]:
    return {
        "type": "mention",
        "mention": {"type": "link_preview", "link_preview": {"url": url}},
        "annotations": dict(ANNOTATIONS),
        "plain_text": url,
        "href": url,
    }


def equation(expression: str) -> dict[str, Any]:
    return {
        "type": "equation",
        "equation": {"expression": expression},
        "annotations": dict(ANNOTATIONS),
        "plain_text": expression,
        "href": None,
    }


@dataclass
class Fault:
    method: str
    pattern: str
    mode: str
    status: int = 400
    code: str = "validation_error"
    retry_after: str = "0"
    times: int = 1
    skip: int = 0


@dataclass
class FakeNotion:
    bot_id: str = field(default_factory=new_id)
    human_id: str = field(default_factory=new_id)
    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    faults: list[Fault] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime(2026, 10, 1, 12, 0, 30, tzinfo=timezone.utc))
    page_size_cap: int = 100
    mention_titles: dict[str, str] = field(default_factory=dict)
    on_request: Optional[Callable[[httpx.Request], None]] = None
    delays: list[tuple[str, str, float, bool]] = field(default_factory=list)

    # ---- building state -------------------------------------------------
    def stamp(self) -> str:
        return self.now.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:00.000Z")

    def add_page(self, title: str = "Adapter Test", page_id: Optional[str] = None,
                 parent_page: Optional[str] = None) -> str:
        page_id = page_id or new_id()
        self.pages[page_id] = {"title": title, "in_trash": False}
        self.children.setdefault(page_id, [])
        self.mention_titles[page_id] = title
        return page_id

    def add_block(self, parent: str, kind: str, rich_text: Optional[list[Any]] = None, *,
                  checked: Optional[bool] = None, by: Optional[str] = None,
                  body: Optional[dict[str, Any]] = None, block_id: Optional[str] = None) -> str:
        block_id = block_id or new_id()
        if body is None:
            body = {"rich_text": rich_text or [], "color": "default"}
            if kind == "to_do":
                body["checked"] = bool(checked)
        parent_ref = ({"type": "page_id", "page_id": parent} if parent in self.pages
                      else {"type": "block_id", "block_id": parent})
        self.blocks[block_id] = {
            "object": "block", "id": block_id, "parent": parent_ref,
            "created_time": self.stamp(), "last_edited_time": self.stamp(),
            "created_by": {"object": "user", "id": by or self.human_id},
            "last_edited_by": {"object": "user", "id": by or self.human_id},
            "has_children": False, "in_trash": False, "type": kind, kind: body,
        }
        self.children.setdefault(block_id, [])
        self.children.setdefault(parent, []).append(block_id)
        if parent in self.blocks:
            self.blocks[parent]["has_children"] = True
        return block_id

    def move(self, block_id: str, new_parent: str) -> None:
        old = self.blocks[block_id]["parent"]
        old_parent = old.get("page_id") or old.get("block_id")
        self.children[old_parent].remove(block_id)
        self.children.setdefault(new_parent, []).append(block_id)
        self.blocks[block_id]["parent"] = ({"type": "page_id", "page_id": new_parent} if new_parent in self.pages
                                           else {"type": "block_id", "block_id": new_parent})

    def set_text(self, block_id: str, rich_text: list[Any]) -> None:
        block = self.blocks[block_id]
        block[block["type"]]["rich_text"] = rich_text
        block["last_edited_time"] = self.stamp()

    def text_of(self, block_id: str) -> str:
        block = self.blocks[block_id]
        return "".join(item["plain_text"] for item in block[block["type"]].get("rich_text", []))

    def page_texts(self, page_id: str) -> list[tuple[str, str]]:
        return [(self.blocks[b]["type"], self.text_of(b)) for b in self.children[page_id]
                if not self.blocks[b]["in_trash"]]

    def fail(self, method: str, pattern: str, mode: str, **options: Any) -> Fault:
        fault = Fault(method, pattern, mode, **options)
        self.faults.append(fault)
        return fault

    def writes(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "PATCH"]

    # ---- serving --------------------------------------------------------
    def mount(self, mock: respx.MockRouter) -> None:
        mock.route(host="api.notion.com").mock(side_effect=self.ahandle)

    def delay(self, method: str, pattern: str, seconds: float, *, apply_first: bool = True) -> None:
        """Hold the next matching answer for ``seconds``; with apply_first the change lands before the wait."""
        self.delays.append((method, pattern, seconds, apply_first))

    async def ahandle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        for index, (method, pattern, seconds, apply_first) in enumerate(self.delays):
            if method == request.method and re.fullmatch(pattern, path):
                del self.delays[index]
                if apply_first:
                    response = self.handle(request)
                    await asyncio.sleep(seconds)
                    return response
                await asyncio.sleep(seconds)
                return self.handle(request)
        return self.handle(request)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request:
            self.on_request(request)
        if request.headers.get("Notion-Version") != "2026-03-11":
            return self.error(400, "missing_version")
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return self.error(401, "unauthorized")
        path = request.url.path.removeprefix("/v1")
        fault = self.take_fault(request.method, path)
        if fault is not None and fault.mode in {"reject", "rate_limit", "redirect", "connect_error",
                                                "drop_before_apply", "server_error_before_apply"}:
            if fault.mode == "reject":
                return self.error(fault.status, fault.code)
            if fault.mode == "rate_limit":
                return httpx.Response(429, json={"object": "error", "status": 429, "code": "rate_limited",
                                                 "message": "slow down"},
                                      headers={"Retry-After": fault.retry_after})
            if fault.mode == "redirect":
                return httpx.Response(307, headers={"Location": "https://evil.example/steal"})
            if fault.mode == "connect_error":
                raise httpx.ConnectError("refused", request=request)
            if fault.mode == "server_error_before_apply":
                return self.error(503, "service_unavailable")
            raise httpx.ReadTimeout("lost", request=request)
        response = self.route(request, path)
        if fault is not None and fault.mode == "drop_after_apply":
            raise httpx.ReadTimeout("lost", request=request)
        if fault is not None and fault.mode == "server_error_after_apply":
            return self.error(502, "bad_gateway")
        return response

    def take_fault(self, method: str, path: str) -> Optional[Fault]:
        for fault in self.faults:
            if fault.method == method and re.fullmatch(fault.pattern, path):
                if fault.skip > 0:
                    fault.skip -= 1
                    return None
                fault.times -= 1
                if fault.times <= 0:
                    self.faults.remove(fault)
                return fault
        return None

    def error(self, status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"object": "error", "status": status, "code": code,
                                            "message": f"{code} with private detail 1234"})

    def route(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "GET" and path == "/users/me":
            return httpx.Response(200, json={"object": "user", "id": self.bot_id, "type": "bot"})
        match = re.fullmatch(rf"/pages/({_ID})", path)
        if request.method == "GET" and match:
            page = self.pages.get(match.group(1))
            if page is None:
                return self.error(404, "object_not_found")
            return httpx.Response(200, json={
                "object": "page", "id": match.group(1), "in_trash": page["in_trash"],
                "properties": {"title": {"id": "title", "type": "title", "title": [rt(page["title"])]}},
                "url": f"https://www.notion.so/{match.group(1).replace('-', '')}",
            })
        match = re.fullmatch(rf"/blocks/({_ID})/children", path)
        if match and request.method == "GET":
            return self.list_children(match.group(1), request)
        if match and request.method == "PATCH":
            return self.append(match.group(1), json.loads(request.content))
        match = re.fullmatch(rf"/blocks/({_ID})", path)
        if match and request.method == "GET":
            block = self.blocks.get(match.group(1))
            if block is None:
                return self.error(404, "object_not_found")
            return httpx.Response(200, json=copy.deepcopy(block))
        if match and request.method == "PATCH":
            return self.update(match.group(1), json.loads(request.content))
        return self.error(400, "invalid_request_url")

    def list_children(self, parent: str, request: httpx.Request) -> httpx.Response:
        if parent not in self.children or (parent not in self.pages and parent not in self.blocks):
            return self.error(404, "object_not_found")
        live = [b for b in self.children[parent] if not self.blocks[b]["in_trash"]]
        size = min(int(request.url.params.get("page_size", "100")), self.page_size_cap)
        cursor = request.url.params.get("start_cursor")
        start = live.index(cursor) if cursor in live else 0
        if cursor is not None and cursor not in live:
            return self.error(400, "validation_error")
        window = live[start:start + size]
        more = start + size < len(live)
        return httpx.Response(200, json={
            "object": "list", "results": [copy.deepcopy(self.blocks[b]) for b in window],
            "has_more": more, "next_cursor": live[start + size] if more else None, "type": "block", "block": {},
        })

    def response_rich_text(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for item in items:
            annotations = {**ANNOTATIONS, **item.get("annotations", {})}
            if item["type"] == "text":
                text = item["text"]
                link = (text.get("link") or {}).get("url")
                out.append({"type": "text", "text": {"content": text["content"], "link": {"url": link} if link else None},
                            "annotations": annotations, "plain_text": text["content"], "href": link})
            elif item["type"] == "equation":
                out.append({**equation(item["equation"]["expression"]), "annotations": annotations})
            else:
                mention = item["mention"]
                kind = mention["type"]
                if kind == "page":
                    out.append(page_mention(mention["page"]["id"], self.mention_titles.get(mention["page"]["id"], "Untitled"),
                                            **item.get("annotations", {})))
                elif kind == "user":
                    out.append({**user_mention(mention["user"]["id"], "Dave"), "annotations": annotations})
                elif kind == "date":
                    out.append({**date_mention(mention["date"]["start"]), "annotations": annotations})
                else:
                    raise ValueError(kind)
        return out

    def check_rich_text(self, items: Any) -> bool:
        if not isinstance(items, list) or len(items) > 100:
            return False
        for item in items:
            if item.get("type") == "text":
                # Conservative: count UTF-16 code units, as a JavaScript string length would.
                if len(item["text"]["content"].encode("utf-16-le")) // 2 > 2000:
                    return False
                link = item["text"].get("link")
                if link and not re.match(r"https?://", link.get("url", "")):
                    return False
            elif item.get("type") == "mention":
                if item["mention"].get("type") not in {"page", "user", "date", "database"}:
                    return False
            elif item.get("type") != "equation":
                return False
        return True

    def append(self, parent: str, body: dict[str, Any]) -> httpx.Response:
        if parent not in self.children or (parent not in self.pages and parent not in self.blocks):
            return self.error(404, "object_not_found")
        children = body.get("children")
        if "after" in body or not isinstance(children, list) or not children or len(children) > 100:
            return self.error(400, "validation_error")
        if body.get("position", {"type": "end"}) != {"type": "end"}:
            return self.error(400, "validation_error")
        for child in children:
            kind = child.get("type")
            if not self.check_rich_text(child.get(kind, {}).get("rich_text")):
                return self.error(400, "validation_error")
        created = []
        for child in children:
            kind = child["type"]
            body_in = child[kind]
            block_id = self.add_block(parent, kind, self.response_rich_text(body_in["rich_text"]),
                                      checked=body_in.get("checked"), by=self.bot_id)
            created.append(copy.deepcopy(self.blocks[block_id]))
        return httpx.Response(200, json={"object": "list", "results": created, "has_more": False, "next_cursor": None})

    def update(self, block_id: str, body: dict[str, Any]) -> httpx.Response:
        block = self.blocks.get(block_id)
        if block is None:
            return self.error(404, "object_not_found")
        kind = block["type"]
        if set(body) != {kind}:
            return self.error(400, "validation_error")
        change = body[kind]
        if "rich_text" in change:
            if not self.check_rich_text(change["rich_text"]):
                return self.error(400, "validation_error")
            block[kind]["rich_text"] = self.response_rich_text(change["rich_text"])
        if "checked" in change:
            if kind != "to_do":
                return self.error(400, "validation_error")
            block[kind]["checked"] = change["checked"]
        block["last_edited_time"] = self.stamp()
        block["last_edited_by"] = {"object": "user", "id": self.bot_id}
        return httpx.Response(200, json=copy.deepcopy(block))

    def tick(self, minutes: int = 1) -> None:
        self.now += timedelta(minutes=minutes)
