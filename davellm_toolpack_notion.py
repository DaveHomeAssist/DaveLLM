"""Notion v2 named data sources/pages, run refs, freshness and targeted readback."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from davellm_notion import NOTION_VERSION, notion_id, rich_text_plain
from davellm_toolpack import ToolpackError, digest, TOOLPACK_MAX_REFS
from davellm_toolpack_http import once

_BASE = "https://api.notion.com/v1"


def _state(record):
    return {key: record.get(key) for key in ("id", "parent", "properties", "last_edited_time", "archived", "in_trash")}


def _public_properties(properties):
    result = {}
    for name, record in list(properties.items())[:50]:
        kind = record.get("type")
        value = record.get(kind)
        if kind in ("title", "rich_text"):
            value = rich_text_plain(value)[:2000]
        elif kind in ("select", "status"):
            value = value.get("name") if isinstance(value, dict) else None
        elif kind in ("number", "checkbox", "date"):
            pass
        else:
            value = "Unsupported property type"
        result[name] = {"type": kind, "value": value}
    return result


def _remember(host, database, row):
    state = host.state()
    identifier = notion_id(row.get("id"))
    if identifier is None:
        raise ToolpackError("Notion returned an invalid row identifier")
    refs = state["refs"]
    ref = next((ref for ref, stored in refs.items() if stored["id"] == identifier and stored["database"] == database), None)
    if ref is None:
        if len(refs) >= TOOLPACK_MAX_REFS:
            raise ToolpackError("This run exhausted its Notion row-ref budget")
        ref = "r" + str(len(refs) + 1)
    snapshot = _state(row)
    refs[ref] = {"id": identifier, "database": database, "snapshot": snapshot}
    return {"row": ref, "digest": digest(snapshot), "properties": _public_properties(row.get("properties", {}))}


def _patch(raw, schema, allowed):
    values = json.loads(raw)
    if not isinstance(values, dict) or not values or len(values) > 20:
        raise ToolpackError("Properties must be a nonempty bounded JSON object")
    result = {}
    for name, value in values.items():
        if name not in allowed or name not in schema:
            raise ToolpackError("Property is not present in the configured writable schema")
        kind = schema[name].get("type")
        if kind in ("title", "rich_text") and isinstance(value, str) and len(value) <= 2000:
            result[name] = {kind: [{"type": "text", "text": {"content": value}}]}
        elif kind == "number" and type(value) in (int, float):
            result[name] = {kind: value}
        elif kind == "checkbox" and type(value) is bool:
            result[name] = {kind: value}
        elif kind in ("select", "status") and isinstance(value, str):
            if value not in {option.get("name") for option in schema[name].get(kind, {}).get("options", [])}:
                raise ToolpackError("Select value is not present in the live schema")
            result[name] = {kind: {"name": value}}
        else:
            raise ToolpackError("Property type or value is unsupported for a targeted patch")
    return result


def _matches(record, patch):
    properties = record.get("properties", {})
    for name, desired in patch.items():
        kind = next(iter(desired))
        actual = properties.get(name, {}).get(kind)
        if kind in ("title", "rich_text"):
            if rich_text_plain(actual) != rich_text_plain(desired[kind]):
                return False
        elif kind in ("select", "status"):
            if not isinstance(actual, dict) or actual.get("name") != desired[kind]["name"]:
                return False
        elif actual != desired[kind]:
            return False
    return True


async def execute_notion(name, args, host):
    settings = host.router["NOTION_SETTINGS"]
    if not settings.token or settings.token_invalid:
        raise ToolpackError("Notion token is not configured or valid on this router")
    auth = {"Authorization": "Bearer " + settings.token, "Notion-Version": NOTION_VERSION}
    state = host.state()

    async def request(method, path, **kwargs):
        return await host.request(method, _BASE + path, headers=auth, **kwargs)

    async def database(alias):
        configured = host.configured("notion_databases", alias)
        identifier = notion_id(configured.get("id"))
        if identifier is None:
            raise ToolpackError("Configured Notion data source is invalid")
        metadata = await request("GET", "/data_sources/" + identifier)
        if metadata.get("object") != "data_source" or not isinstance(metadata.get("properties"), dict):
            raise ToolpackError("Notion returned a malformed data source")
        return identifier, configured, metadata

    if name == "notion.search":
        matches = []
        if len(settings.pages) + len(host.config.get("notion_databases", {})) > 20:
            raise ToolpackError("Search target set exceeds the twenty-target budget")
        for alias, identifier in settings.pages.items():
            page = await request("GET", "/pages/" + identifier)
            title = " ".join(str(item.get("value", "")) for item in _public_properties(page.get("properties", {})).values())
            state["snapshots"]["page:" + alias] = _state(page)
            if args["query"].casefold() in title.casefold():
                matches.append({"page": alias, "title": title[:200], "digest": digest(_state(page))})
        for alias in host.config.get("notion_databases", {}):
            identifier, _, metadata = await database(alias)
            title = rich_text_plain(metadata.get("title"))
            if args["query"].casefold() in title.casefold():
                matches.append({"database": alias, "title": title[:200], "digest": digest(metadata.get("properties", {}))})
        return {"matches": matches[:args["limit"]], "scope": "Configured target titles only", "truncated": len(matches) > args["limit"]}
    if name in ("notion.db.query", "notion.routines.read", "notion.inbox.triage"):
        alias = args["database"]
        identifier, configured, metadata = await database(alias)
        if name == "notion.routines.read" and configured.get("kind") != "routines":
            raise ToolpackError("Data source is not configured as routines")
        if name == "notion.inbox.triage" and configured.get("kind") != "inbox":
            raise ToolpackError("Data source is not configured as inbox")
        state["snapshots"]["database:" + alias] = metadata.get("properties", {})
        listing = await request("POST", "/data_sources/" + identifier + "/query", body={"page_size": args["limit"]})
        records = listing.get("results")
        if not isinstance(records, list):
            raise ToolpackError("Notion returned malformed rows")
        rows = []
        for row in records[:args["limit"]]:
            if notion_id(row.get("parent", {}).get("data_source_id")) != identifier:
                raise ToolpackError("Notion row is outside the configured data source")
            rows.append(_remember(host, alias, row))
        proposals = []
        if name == "notion.inbox.triage":
            rules = configured.get("routing_rules", [])
            if not isinstance(rules, list) or len(rules) > 20:
                raise ToolpackError("Inbox routing rules exceed the configured budget")
            for row in rows:
                status = row["properties"].get(configured.get("status_property"), {}).get("value")
                if status in configured.get("resolved_values", []):
                    continue
                text = " ".join(str(value.get("value", "")) for value in row["properties"].values()).casefold()
                matches = [rule.get("project") for rule in rules if isinstance(rule, dict) and isinstance(rule.get("contains"), str)
                           and rule["contains"] and rule["contains"].casefold() in text and isinstance(rule.get("project"), str)]
                proposals.append({"row": row["row"], "projects": list(dict.fromkeys(matches)),
                                  "reason": "Configured keyword match" if matches else "Unresolved; no configured route matches"})
        return {"rows": rows, "schema": {key: value.get("type") for key, value in metadata["properties"].items()},
                "schema_digest": digest(metadata["properties"]), "truncated": bool(listing.get("has_more")),
                "routing": "Proposal only; no rows changed" if name == "notion.inbox.triage" else None,
                "proposals": proposals if name == "notion.inbox.triage" else None}
    if name in ("notion.page.diff", "notion.page.create", "notion.comment.add"):
        alias = args["page"]
        if alias not in settings.pages:
            raise ToolpackError("Notion page is not configured")
        identifier = settings.pages[alias]
        current = await request("GET", "/pages/" + identifier)
        snapshot = _state(current)
        key = "page:" + alias
        before = state["snapshots"].get(key)
        if name == "notion.page.diff":
            state["snapshots"][key] = snapshot
            return {"page": alias, "digest": digest(snapshot), "baseline_recorded": before is None,
                    "changed_fields": [field for field in snapshot if before is not None and snapshot[field] != before.get(field)],
                    "scope": "Page metadata/property snapshot; block contents remain the v1 adapter's boundary"}
        if before is None or digest(before) != args["expected_digest"] or digest(snapshot) != args["expected_digest"] or current.get("archived") or current.get("in_trash"):
            raise ToolpackError("Notion page changed or has no matching run snapshot; nothing was sent")
        if name == "notion.page.create":
            body = {"parent": {"page_id": identifier}, "properties": {"title": {"type": "title", "title": [{"type": "text", "text": {"content": args["title"]}}]}}}
            async def send():
                fresh = await request("GET", "/pages/" + identifier)
                if digest(_state(fresh)) != args["expected_digest"]:
                    raise ToolpackError("Notion page changed; nothing was sent")
                return await request("POST", "/pages", body=body)
            async def verify(record):
                child = notion_id(record.get("id"))
                if child is None:
                    return False
                fresh = await request("GET", "/pages/" + child)
                return notion_id(fresh.get("parent", {}).get("page_id")) == identifier and rich_text_plain(fresh.get("properties", {}).get("title", {}).get("title")) == args["title"]
        else:
            body = {"parent": {"page_id": identifier}, "rich_text": [{"type": "text", "text": {"content": args["text"]}}]}
            async def send():
                fresh = await request("GET", "/pages/" + identifier)
                if digest(_state(fresh)) != args["expected_digest"]:
                    raise ToolpackError("Notion page changed; nothing was sent")
                return await request("POST", "/comments", body=body)
            async def verify(record):
                rows = await request("GET", "/comments", params={"block_id": identifier, "page_size": 100})
                return any(row.get("id") == record.get("id") and rich_text_plain(row.get("rich_text")) == args["text"] for row in rows.get("results", []))
        result = await once(host, name, args, send, verify, lock=host.effect_lock("notion", identifier))
        result.pop("record", None)  # Provider IDs never leave the run ledger.
        return result
    alias = args["database"]
    identifier, configured, metadata = await database(alias)
    patch = _patch(args["properties_json"], metadata["properties"], configured.get("writable_properties", []))
    if name == "notion.db.update_props":
        record = state["refs"].get(args["row"])
        if not record or record["database"] != alias:
            raise ToolpackError("Row ref is not from this run and configured data source")
        current = await request("GET", "/pages/" + record["id"])
        if digest(record["snapshot"]) != args["expected_digest"] or digest(_state(current)) != args["expected_digest"] or notion_id(current.get("parent", {}).get("data_source_id")) != identifier or current.get("archived") or current.get("in_trash"):
            raise ToolpackError("Notion row changed; nothing was sent")
        # Recheck immediately before dispatch: all the checks above remain read-only.
        async def send():
            fresh = await request("GET", "/pages/" + record["id"])
            if digest(_state(fresh)) != args["expected_digest"]:
                raise ToolpackError("Notion row changed; nothing was sent")
            return await request("PATCH", "/pages/" + record["id"], body={"properties": patch})
        async def verify(response):
            fresh = await request("GET", "/pages/" + record["id"])
            return _matches(fresh, patch) and all(fresh.get("properties", {}).get(key) == value for key, value in current.get("properties", {}).items() if key not in patch)
    elif name in ("notion.db.create_row", "notion.log_session"):
        if name == "notion.log_session" and configured.get("kind") != "session_log":
            raise ToolpackError("Data source is not configured as a session log")
        snapshot = state["snapshots"].get("database:" + alias)
        if snapshot is None or digest(snapshot) != args["expected_digest"] or digest(metadata["properties"]) != args["expected_digest"]:
            raise ToolpackError("Notion schema changed or has no matching run snapshot; nothing was sent")
        async def send():
            fresh = await request("GET", "/data_sources/" + identifier)
            if digest(fresh.get("properties")) != args["expected_digest"]:
                raise ToolpackError("Notion schema changed; nothing was sent")
            return await request("POST", "/pages", body={"parent": {"type": "data_source_id", "data_source_id": identifier}, "properties": patch})
        async def verify(response):
            child = notion_id(response.get("id"))
            if child is None:
                return False
            fresh = await request("GET", "/pages/" + child)
            return notion_id(fresh.get("parent", {}).get("data_source_id")) == identifier and _matches(fresh, patch)
    else:
        raise ToolpackError("Tool implementation is unavailable")
    result = await once(host, name, args, send, verify, lock=host.effect_lock("notion", identifier))
    record = result.pop("record", None)
    if result["outcome"] == "verified" and isinstance(record, dict):
        result["row"] = _remember(host, alias, record)
    return result
