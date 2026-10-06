"""Fixed-provider REST adapters and operator-allowlisted network targets."""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import socket
import time
from datetime import timedelta
from email.message import EmailMessage
from urllib.parse import quote

import asyncio
import httpx
from davellm_public_http import public_http_client, public_stream

from davellm_toolpack import ToolpackError, digest, fixed_url, redact
from davellm_toolpack_local import aware


def headers(host, variable):
    token = host.secrets.get(variable)
    if not token:
        raise ToolpackError("Provider credential is not configured on this router")
    return {"Authorization": "Bearer " + token}


async def once(host, name, args, send, verify, *, lock=None):
    state = host.state()
    key = digest({"tool": name, "arguments": args})
    if key in state["effects"]:
        raise ToolpackError("This exact effect was already dispatched; inspect its outcome before another call")
    if len(state["effects"]) >= 50:
        raise ToolpackError("This run exhausted its effect ledger budget")
    if len(host.pending_effects) >= 64:
        raise ToolpackError("Bounded effect verification capacity is full")
    state["effects"][key] = "unknown"
    async def operation():
        async def work():
            response = await send()
            confirmed = await verify(response)
            state["effects"][key] = "verified" if confirmed else "unknown"
            return {"outcome": state["effects"][key], "acknowledged": True, "record": response}
        async def guarded():
            if lock is None:
                return await work()
            async with lock:
                return await work()
        try:
            return await asyncio.wait_for(guarded(), 30)
        except (ToolpackError, TimeoutError):
            # Lost responses are ambiguous. Never retry automatically.
            return {"outcome": "unknown", "acknowledged": False, "note": "Inspect the destination before proposing a new call"}
    # Cancellation must not abandon a dispatched write's bounded readback.
    task = asyncio.create_task(operation())
    host.pending_effects.add(task)
    def finished(completed):
        host.pending_effects.discard(completed)
        if not completed.cancelled():
            completed.exception()
    task.add_done_callback(finished)
    return await asyncio.shield(task)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,200}", value):
        raise ToolpackError("Configured provider identifier is invalid")
    return quote(value, safe="")


async def github_job_logs(url, auth, host, lines):
    """Credentials go to api.github.com only; signed redirects use pinned public DNS."""
    async def capture(response):
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > 512 * 1024:
                raise ToolpackError("Job log exceeded the capture budget")
        decoded = data.decode("utf-8", "replace").splitlines()
        return {"lines": decoded[-lines:], "truncated": len(decoded) > lines}

    async def work():
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url, headers=auth) as response:
                if response.status_code == 200:
                    return await capture(response)
                if response.status_code not in (302, 307) or not response.headers.get("location", "").startswith("https://"):
                    raise ToolpackError("Provider refused the log request")
                location = response.headers["location"]
        addresses = await asyncio.to_thread(host.router["validate_public_url"], location)
        async with public_http_client(timeout=10) as client:
            async with public_stream(client, location, addresses) as response:
                if response.status_code != 200:
                    raise ToolpackError("Log download was refused; further redirects are not followed")
                return await capture(response)
    return await asyncio.wait_for(work(), 20)


async def execute_http(name, args, host):
    if name.startswith("gh.") or name == "pages.deploy_status":
        repository = host.configured("repositories", args["repository"])
        if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ToolpackError("Configured repository is invalid")
        base = "https://api.github.com/repos/" + repository
        auth = headers(host, "DAVE_GITHUB_TOKEN")
        auth.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        if name == "gh.pr.status":
            record = await host.request("GET", base + "/pulls/" + str(args["number"]), headers=auth)
            checks = await host.request("GET", base + "/commits/" + _identifier(record["head"]["sha"]) + "/check-runs", headers=auth, params={"per_page": 50})
            return {"pr": {k: record.get(k) for k in ("number", "title", "state", "merged", "mergeable", "draft")},
                    "head": record["head"]["sha"], "checks": checks.get("check_runs", [])}
        if name == "gh.pr.comments":
            rows = await host.request("GET", base + "/pulls/" + str(args["number"]) + "/comments", headers=auth, params={"per_page": args["limit"]})
            return {"comments": rows, "may_have_more": len(rows) == args["limit"]}
        if name == "gh.ci.logs":
            job = await host.request("GET", base + "/actions/jobs/" + str(args["job"]), headers=auth)
            check_id = str(job.get("check_run_url", "")).rsplit("/", 1)[-1]
            if not check_id.isdigit():
                raise ToolpackError("Job has no valid check-run evidence")
            annotations = await host.request("GET", base + "/check-runs/" + check_id + "/annotations", headers=auth, params={"per_page": args["limit"]})
            logs = await github_job_logs(base + "/actions/jobs/" + str(args["job"]) + "/logs", auth, host, args["limit"])
            return {"job": {k: job.get(k) for k in ("id", "name", "status", "conclusion", "steps")},
                    "failure_annotations": annotations, "logs": logs}
        if name == "pages.deploy_status":
            return {"build": await host.request("GET", base + "/pages/builds/latest", headers=auth)}
        async def send():
            return await host.request("POST", base + "/issues", headers=auth, body={"title": args["title"], "body": args["body"]})
        async def verify(record):
            if not isinstance(record.get("number"), int):
                return False
            fresh = await host.request("GET", base + "/issues/" + str(record["number"]), headers=auth)
            return fresh.get("title") == args["title"] and fresh.get("body") == args["body"]
        return await once(host, name, args, send, verify)
    if name == "vercel.deploy_status":
        project = host.configured("vercel_projects", args["project"])
        return {"deployments": await host.request("GET", "https://api.vercel.com/v6/deployments", headers=headers(host, "DAVE_VERCEL_TOKEN"),
                                                    params={"projectId": project, "limit": args["limit"]})}
    if name.startswith("ha."):
        config = host.configured("services", "homeassistant")
        base = fixed_url(config["url"])
        auth = headers(host, "DAVE_HA_TOKEN")
        entity = args.get("entity")
        if entity is not None and entity not in config.get("entities", []):
            raise ToolpackError("Home Assistant entity is not allowlisted")
        if name == "ha.state.get":
            return {"state": await host.request("GET", base + "/api/states/" + _identifier(entity), headers=auth)}
        if name == "ha.history":
            since = aware(args["since"])
            if abs((__import__("datetime").datetime.now(since.tzinfo) - since).total_seconds()) > 7 * 86400:
                raise ToolpackError("History is limited to the last seven days")
            return {"history": await host.request("GET", base + "/api/history/period/" + quote(since.isoformat(), safe=""), headers=auth,
                                                  params={"filter_entity_id": entity, "minimal_response": "true", "no_attributes": "true"})}
        if name == "ha.config.check":
            result = await host.request("POST", base + "/api/config/core/check_config", headers=auth)
            return {"validation": result, "configuration_reloaded": False}
        service = args["service"]
        if service not in config.get("allowed_services", []) or not re.fullmatch(r"[a-z_]+\.[a-z_]+", service):
            raise ToolpackError("Home Assistant service is not allowlisted")
        domain, action = service.split(".")
        async def send():
            return await host.request("POST", base + "/api/services/" + domain + "/" + action, headers=auth, body={"entity_id": entity})
        async def verify(record):
            # A state read is evidence, not proof of physical actuation.
            await host.request("GET", base + "/api/states/" + _identifier(entity), headers=auth)
            return False
        return await once(host, name, args, send, verify)
    if name in ("service.health", "resolume.status"):
        service = host.configured("services", args["service"])
        if name == "resolume.status" and service.get("kind") != "resolume":
            raise ToolpackError("Service is not a configured Resolume target")
        started = time.monotonic()
        record = await host.request("GET", fixed_url(service["url"]))
        return {"service": args["service"], "state": "Confirmed", "latency_ms": round((time.monotonic() - started) * 1000, 1), "response": record}
    if name == "notify.push":
        target = host.configured("notifications", args["destination"])
        endpoint = fixed_url(target["url"])
        if not endpoint.startswith("https://"):
            raise ToolpackError("Notifications require HTTPS")
        safe = redact(args["text"], tuple(host.secrets.values()))
        if safe != args["text"]:
            raise ToolpackError("Notification contains a sensitive value; nothing was sent")
        key = digest({"destination": args["destination"], "text": safe})
        now = time.monotonic()
        if any(k == key and now - at < 600 or k.startswith(args["destination"] + ":") and now - at < 60 for k, at in host.notifications.items()):
            raise ToolpackError("Notification is duplicate or rate limited")
        host.notifications[key] = now
        host.notifications[args["destination"] + ":rate"] = now
        while len(host.notifications) > 128:
            host.notifications.popitem(last=False)
        auth = headers(host, "DAVE_NTFY_TOKEN") if host.secrets.get("DAVE_NTFY_TOKEN") else {}
        async def send():
            # ntfy JSON publishing uses the server origin and explicitly configured topic.
            return await host.request("POST", endpoint, headers=auth, body={"topic": target["topic"], "message": safe})
        async def verify(record):
            return False  # Transport acknowledgement is not phone acceptance.
        result = await once(host, name, args, send, verify)
        result["phone_acceptance"] = "Unknown"
        return result
    if name == "osc.send":
        target = host.configured("osc_targets", args["target"])
        address = args["address"]
        if address not in target.get("addresses", []) or not address.startswith("/") or "\0" in address or "\0" in args["value"]:
            raise ToolpackError("OSC address is not allowlisted or contains a null byte")
        destination = ipaddress.ip_address(target["host"])
        if not destination.is_private or destination.is_multicast or destination.is_unspecified:
            raise ToolpackError("OSC target must be a configured private address")
        port = target["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ToolpackError("OSC target port is invalid")
        def string(value):
            data = value.encode("utf-8") + b"\0"
            return data + b"\0" * (-len(data) % 4)
        packet = string(address) + string(",s") + string(args["value"])
        with socket.socket(socket.AF_INET6 if destination.version == 6 else socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.settimeout(1)
            sender.sendto(packet, (str(destination), port))
        return {"outcome": "unknown", "packet_sent": True, "receipt": "OSC/UDP supplies no acknowledgement"}
    # OAuth state belongs to the router/operator, never to model arguments or a repository.
    binding = host.binding()
    if binding.user_id not in host.config.get("google_users", []):
        raise ToolpackError("Google account is not available to this run owner")
    auth = headers(host, "DAVE_GOOGLE_ACCESS_TOKEN")
    if name == "gmail.search":
        return {"messages": await host.request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages", headers=auth,
                                               params={"q": args["query"], "maxResults": args["limit"]})}
    if name == "gmail.draft":
        if "\n" in args["to"] or "\r" in args["to"] or "\n" in args["subject"] or "\r" in args["subject"]:
            raise ToolpackError("Mail headers cannot contain line breaks")
        message = EmailMessage()
        message["To"], message["Subject"] = args["to"], args["subject"]
        message.set_content(args["body"])
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        base = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
        async def send():
            return await host.request("POST", base, headers=auth, body={"message": {"raw": raw}})
        async def verify(record):
            identifier = _identifier(record.get("id", ""))
            fresh = await host.request("GET", base + "/" + identifier, headers=auth, params={"format": "raw"})
            received = fresh.get("message", {}).get("raw", "")
            return base64.urlsafe_b64decode(received + "=" * (-len(received) % 4)) == message.as_bytes()
        return await once(host, name, args, send, verify)
    if name.startswith("calendar."):
        calendar = host.configured("calendars", args["calendar"])
        start, end = aware(args["start"]), aware(args["end"])
        if not start < end or (end - start).total_seconds() > 31 * 86400:
            raise ToolpackError("Calendar window must be positive and at most 31 days")
        record = await host.request("GET", "https://www.googleapis.com/calendar/v3/calendars/" + _identifier(calendar) + "/events", headers=auth,
                                    params={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime", "maxResults": args.get("limit", 50)})
        if name == "calendar.list":
            return {"events": record.get("items", []), "truncated": bool(record.get("nextPageToken"))}
        if record.get("nextPageToken"):
            return {"suggestion": None, "state": "Unknown", "reason": "Incomplete event inventory"}
        cursor, duration = start, timedelta(minutes=args["minutes"])
        for event in record.get("items", []):
            if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
                continue
            if not event.get("start", {}).get("dateTime") or not event.get("end", {}).get("dateTime"):
                return {"suggestion": None, "state": "Unknown", "reason": "All-day event requires timezone-aware calendar policy"}
            left, right = aware(event["start"]["dateTime"]), aware(event["end"]["dateTime"])
            if cursor + duration <= left:
                break
            cursor = max(cursor, right)
        return {"suggestion": {"start": cursor.isoformat(), "end": (cursor + duration).isoformat()} if cursor + duration <= end else None,
                "advisory_only": True}
    if name == "drive.search":
        folder = host.configured("drive_folders", args["folder"])
        def escape(value):
            return str(value).replace("\\", "\\\\").replace("'", "\\'")
        query = "'" + escape(folder) + "' in parents and trashed = false and name contains '" + escape(args["query"]) + "'"
        return {"files": await host.request("GET", "https://www.googleapis.com/drive/v3/files", headers=auth,
                                             params={"q": query, "pageSize": args["limit"], "fields": "files(id,name,mimeType),nextPageToken"})}
    if name == "drive.read":
        identifier = _identifier(host.configured("drive_files", args["file"]))
        base = "https://www.googleapis.com/drive/v3/files/" + identifier
        record = await host.request("GET", base, headers=auth, params={"fields": "name,mimeType,size"})
        if record.get("mimeType") == "application/vnd.google-apps.document":
            text = await host.request("GET", base + "/export", headers=auth, params={"mimeType": "text/plain"}, text_response=True)
        elif record.get("mimeType", "").startswith("text/"):
            text = await host.request("GET", base, headers=auth, params={"alt": "media"}, text_response=True)
        else:
            text = None
        return {"metadata": record, "text": text, "content_state": "Confirmed" if text is not None else "Unsupported MIME type"}
    if name == "contacts.lookup":
        return {"contacts": await host.request("GET", "https://people.googleapis.com/v1/people:searchContacts", headers=auth,
                                                params={"query": args["query"], "readMask": "names,emailAddresses", "pageSize": min(args["limit"], 30)})}
    raise ToolpackError("Tool implementation is unavailable")
