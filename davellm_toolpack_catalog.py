"""The exact 100 October 5 expansion candidates; no broad action-switch schema."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def text(limit: int = 200, minimum: int = 1) -> dict[str, Any]:
    return {"type": "string", "minLength": minimum, "maxLength": limit}


def integer(low: int, high: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": low, "maximum": high}


def choice(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def array(item: dict[str, Any]) -> dict[str, Any]:
    # The harness subset has no maxItems. Runtime admission caps every array at 50.
    return {"type": "array", "items": item}


def obj(**properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


NODE = text(40)
MODEL = text(160)
PATH = text(4096)
ALIAS = text(80)
BODY = text(8000)
LIMIT = integer(1, 50)
TARGET = obj(node=NODE, model=MODEL)


@dataclass(frozen=True)
class Spec:
    name: str
    family: str
    description: str
    schema: dict[str, Any]
    permission: str = "read"
    approval: bool = False
    backend: str = "local"


def spec(name: str, family: str, description: str, properties: dict[str, Any],
         required: tuple[str, ...] | None = None, *, permission: str = "read",
         approval: bool = False, backend: str = "local") -> Spec:
    return Spec(name, family, description,
                {"type": "object", "properties": properties,
                 "required": list(properties) if required is None else list(required),
                 "additionalProperties": False}, permission, approval, backend)


SPECS = (
    spec("ollama.ps", "cluster", "Read loaded models, observed residency and expiry on a configured node.", {"node": NODE}, permission="public_network", backend="cluster"),
    spec("model.pull", "cluster", "Pull one operator-allowlisted model; approval required, no automatic retries.", {"node": NODE, "model": MODEL}, permission="write", approval=True, backend="cluster"),
    spec("model.delete", "cluster", "Delete one installed model after exact approval and inventory validation.", {"node": NODE, "model": MODEL}, permission="write", approval=True, backend="cluster"),
    spec("model.unload", "cluster", "Unload one installed model with keep_alive zero after approval.", {"node": NODE, "model": MODEL}, permission="write", approval=True, backend="cluster"),
    spec("model.warm", "cluster", "Preload one installed model after approval; bounded, no question inference.", {"node": NODE, "model": MODEL}, permission="write", approval=True, backend="cluster"),
    spec("node.wake", "cluster", "Send one Wake-on-LAN packet to an operator-configured target after approval.", {"node": NODE}, permission="write", approval=True, backend="cluster"),
    spec("node.diagnose", "cluster", "Read bounded node service, inventory, latency and version evidence; timeout means Unknown.", {"node": NODE}, permission="public_network", backend="cluster"),
    spec("node.disk", "cluster", "Read an approved runner's disk and configured mount evidence.", {"runner": ALIAS}, permission="execute_process", approval=True, backend="job"),
    spec("node.ctx_check", "cluster", "Read observed loaded context separately from router-requested context; missing observations are Unknown.", {"node": NODE, "model": MODEL}, permission="public_network", backend="cluster"),
    spec("cluster.benchmark", "cluster", "Approved bounded throughput measurement; never H9 qualification.", {"node": NODE, "model": MODEL, "text": text(1000), "max_tokens": integer(1, 256)}, permission="execute_process", approval=True, backend="cluster"),
    spec("model.inventory_drift", "cluster", "Compare installed models with operator-configured expected inventory.", {"node": NODE}, permission="public_network", backend="cluster"),
    spec("ollama.version_check", "cluster", "Read local version; public latest comparison requires explicit compare_latest true.", {"node": NODE, "compare_latest": {"type": "boolean"}}, permission="public_network", backend="cluster"),
    spec("route.suggest", "routing", "Suggest only currently installed node/model pairs; advisory, never executes.", {"text": BODY}, permission="public_network", backend="cluster"),
    spec("model.ask", "routing", "Approved isolated second opinion with a bounded output; cannot run tools or recurse.", {"node": NODE, "model": MODEL, "text": BODY, "max_tokens": integer(1, 512)}, permission="execute_process", approval=True, backend="cluster"),
    spec("model.consensus", "routing", "Approved two or three distinct-model answers, not a claim of correctness.", {"targets": array(TARGET), "text": BODY, "max_tokens": integer(1, 256)}, permission="execute_process", approval=True, backend="cluster"),
    spec("token.count", "routing", "Estimate text tokens and compare with a configured node's advisory limit; not tokenizer-exact.", {"text": BODY, "node": NODE}, backend="native"),
    spec("chat.summarize", "routing", "Approved summary of the run owner's conversation on an installed model; no persistence.", {"conversation": text(100), "node": NODE, "model": MODEL, "max_tokens": integer(1, 512)}, permission="execute_process", approval=True, backend="cluster"),
    spec("prompt.lint", "quality", "Report deterministic PRMT placeholder/scaffolding findings; no model call.", {"text": BODY}),
    spec("eval.run", "quality", "Approved single reference-prompt comparison with explicit expected text; not H9.", {"node": NODE, "model": MODEL, "text": text(4000), "expected": text(1000), "max_tokens": integer(1, 256)}, permission="execute_process", approval=True, backend="cluster"),
    spec("perf.read", "quality", "Read bounded owner-scoped performance/cost records, never unrestricted database SQL.", {"limit": LIMIT}, backend="native"),
    spec("memory.recall", "memory", "Search only memory sources allowlisted for the current run owner.", {"source": ALIAS, "query": text(200), "limit": LIMIT}, permission="read_files"),
    spec("memory.propose", "memory", "Append a proposal to the run's project notepad after approval; does not silently install memory.", {"text": text(4000), "expected_digest": text(64)}, permission="write", approval=True, backend="native"),
    spec("vector.search", "memory", "Search owner-scoped conversation vectors and the configured dedicated document index; no inference.", {"query": text(200), "limit": LIMIT}, backend="native"),
    spec("vector.index", "memory", "Approved indexing recipe on an allowlisted capable runner and source.", {"runner": ALIAS, "source": ALIAS}, permission="execute_process", approval=True, backend="job"),
    spec("project.notepad.write", "memory", "Replace run-owned plain notepad only when expected digest still matches, then read back.", {"text": text(20000, 0), "expected_digest": text(64)}, permission="write", approval=True, backend="native"),
    spec("project.brain.pin", "memory", "Pin a run-owned BRAIN fact after exact approval and captured-revision freshness check.", {"text": text(4000), "expected_revision": integer(1, 1000000)}, permission="write", approval=True, backend="native"),
    spec("chat.read", "history", "Read bounded user/assistant turns from a conversation owned by this run's user.", {"conversation": text(100), "offset": integer(0, 100000), "limit": LIMIT}, backend="native"),
    spec("chat.export", "history", "Return bounded owner-scoped Markdown in the result; no file is written.", {"conversation": text(100)}, backend="native"),
    spec("docs.ask", "memory", "Approved folder-grounded answer from allowlisted source excerpts with citations; no recursive tools.", {"source": ALIAS, "query": text(1000), "node": NODE, "model": MODEL, "max_tokens": integer(1, 512)}, permission="execute_process", approval=True, backend="cluster"),
    spec("pdf.read", "memory", "Read PDF text through a bounded isolated parsing recipe on an approved runner.", {"runner": ALIAS, "path": PATH, "start_page": integer(1, 10000), "pages": integer(1, 10)}, permission="execute_process", approval=True, backend="job"),
    spec("claude.memory.read", "memory", "Read a named owner-allowlisted memory file; never implicit home-directory access.", {"source": ALIAS, "path": PATH}, permission="read_files"),
    spec("glossary.lookup", "memory", "Look up a term in the operator's nonsecret configured glossary.", {"term": text(200)}),
    spec("notion.search", "notion", "Search only configured pages/databases with run-local refs; no workspace-wide enumeration.", {"query": text(200), "limit": LIMIT}, permission="public_network", backend="notion"),
    spec("notion.db.query", "notion", "Query one configured data source; bounded refs and property snapshots.", {"database": ALIAS, "limit": LIMIT}, permission="public_network", backend="notion"),
    spec("notion.db.create_row", "notion", "Create one row in a configured data source after schema snapshot freshness and approval.", {"database": ALIAS, "properties_json": text(8000), "expected_digest": text(64)}, permission="write", approval=True, backend="notion"),
    spec("notion.db.update_props", "notion", "Patch only named row properties; run ref, fresh snapshot, approval and readback required.", {"database": ALIAS, "row": text(20), "properties_json": text(8000), "expected_digest": text(64)}, permission="write", approval=True, backend="notion"),
    spec("notion.page.create", "notion", "Create one child under a configured parent; no arbitrary parent ID.", {"page": ALIAS, "title": text(200), "expected_digest": text(64)}, permission="write", approval=True, backend="notion"),
    spec("notion.comment.add", "notion", "Add one comment on a configured page after snapshot freshness and approval.", {"page": ALIAS, "text": text(2000), "expected_digest": text(64)}, permission="write", approval=True, backend="notion"),
    spec("notion.page.diff", "notion", "Compare a configured page's fresh metadata with this run's earlier snapshot.", {"page": ALIAS}, permission="public_network", backend="notion"),
    spec("notion.log_session", "notion", "Create a session row in the configured logging data source after approval and readback.", {"database": ALIAS, "properties_json": text(8000), "expected_digest": text(64)}, permission="write", approval=True, backend="notion"),
    spec("notion.routines.read", "notion", "Read only a configured routines data source; never infer a database ID.", {"database": ALIAS, "limit": LIMIT}, permission="public_network", backend="notion"),
    spec("notion.inbox.triage", "notion", "Propose inbox routing from configured data source rows; no mutation.", {"database": ALIAS, "limit": LIMIT}, permission="public_network", backend="notion"),
    spec("gh.pr.status", "github", "Read status/checks of a PR in an operator-allowlisted repository.", {"repository": ALIAS, "number": integer(1, 1000000)}, permission="public_network", backend="http"),
    spec("gh.pr.comments", "github", "Read bounded review comments on an allowlisted repository PR.", {"repository": ALIAS, "number": integer(1, 1000000), "limit": LIMIT}, permission="public_network", backend="http"),
    spec("gh.ci.logs", "github", "Read bounded job log tail; signed public downloads are DNS-pinned and receive no GitHub credentials.", {"repository": ALIAS, "job": integer(1, 999999999999), "limit": LIMIT}, permission="public_network", backend="http"),
    spec("gh.issue.create", "github", "Create one issue after approval in a configured repository; verify by readback.", {"repository": ALIAS, "title": text(200), "body": BODY}, permission="write", approval=True, backend="http"),
    spec("git.worktrees", "git", "List bounded admitted Git worktrees and claim presence without external paths.", {"repository": PATH}, permission="read_files"),
    spec("git.commit", "git", "Approved named-file-only commit recipe; hooks disabled, secret scan and exact diff required.", {"runner": ALIAS, "repository": ALIAS, "files": array(PATH), "message": text(1000), "expected_digest": text(64)}, permission="execute_process", approval=True, backend="job"),
    spec("git.push", "git", "Approved configured remote/branch push recipe; no force, history rewrite or caller URL.", {"runner": ALIAS, "repository": ALIAS, "branch": text(120), "expected_head": text(40)}, permission="execute_process", approval=True, backend="job"),
    spec("test.run", "code", "Run one preconfigured remote test recipe; caller cannot choose a command.", {"runner": ALIAS, "recipe": ALIAS}, permission="execute_process", approval=True, backend="job"),
    spec("code.symbols", "code", "Read Python functions/classes from an admitted bounded UTF-8 source file.", {"path": PATH}, permission="read_files"),
    spec("code.grep_regex", "code", "Run bounded regex matching in an isolated runner process, never in the router event loop.", {"runner": ALIAS, "source": ALIAS, "pattern": text(200)}, permission="execute_process", approval=True, backend="job"),
    spec("lint.run", "code", "Run an operator-defined remote lint recipe with a bounded capture.", {"runner": ALIAS, "recipe": ALIAS}, permission="execute_process", approval=True, backend="job"),
    spec("dep.audit", "code", "Run an operator-defined remote dependency audit recipe; network access is part of approval.", {"runner": ALIAS, "recipe": ALIAS}, permission="execute_process", approval=True, backend="job"),
    spec("pages.deploy_status", "github", "Read GitHub Pages build status; never starts a deployment.", {"repository": ALIAS}, permission="public_network", backend="http"),
    spec("vercel.deploy_status", "github", "Read deployments of one configured Vercel project.", {"project": ALIAS, "limit": LIMIT}, permission="public_network", backend="http"),
    spec("rules.lookup", "governance", "Read one rule ID from a named owner-allowlisted governance source.", {"source": ALIAS, "rule": text(80)}, permission="read_files"),
    spec("machine.access.lookup", "governance", "Read a named nonsecret access-guide section from an owner-allowlisted source.", {"source": ALIAS, "machine": text(80)}, permission="read_files"),
    spec("comms.log.append", "governance", "Append through an approved runner recipe with concurrent preservation and readback.", {"runner": ALIAS, "source": ALIAS, "text": text(8000), "expected_digest": text(64)}, permission="execute_process", approval=True, backend="job"),
    spec("comms.log.read", "governance", "Read bounded current-day entries from an owner-allowlisted log source.", {"source": ALIAS, "limit": LIMIT}, permission="read_files"),
    spec("nextsteps.read", "governance", "Read one named owner-allowlisted project record, never the cross-project board implicitly.", {"source": ALIAS}, permission="read_files"),
    spec("nextsteps.update", "governance", "Approved governed updater recipe; --check and peer-project preservation required.", {"runner": ALIAS, "source": ALIAS, "record_json": text(8000), "expected_digest": text(64)}, permission="execute_process", approval=True, backend="job"),
    spec("agent.claim.check", "governance", "Read only claim presence/age in an admitted repository; no takeover.", {"repository": PATH}, permission="read_files"),
    spec("runner.select", "governance", "Suggest only configured runner capability records; no dispatch or agents.", {"capability": text(80)}),
    spec("ha.state.get", "ha", "Read one operator-allowlisted Home Assistant entity.", {"entity": ALIAS}, permission="public_network", backend="http"),
    spec("ha.service.call", "ha", "Call one allowlisted service on one allowlisted entity after approval; no templates or arbitrary service data.", {"entity": ALIAS, "service": ALIAS}, permission="write", approval=True, backend="http"),
    spec("ha.history", "ha", "Read bounded recent history of an allowlisted entity.", {"entity": ALIAS, "since": text(50)}, permission="public_network", backend="http"),
    spec("ha.config.check", "ha", "Request Home Assistant config validation after approval, never reloads configuration.", {}, permission="write", approval=True, backend="http"),
    spec("docker.ps", "ops", "Read only operator-allowlisted containers through an approved read recipe.", {"runner": ALIAS}, permission="execute_process", approval=True, backend="job"),
    spec("docker.logs", "ops", "Read bounded redacted logs for one configured container.", {"runner": ALIAS, "container": ALIAS, "lines": integer(1, 200)}, permission="execute_process", approval=True, backend="job"),
    spec("mac.disk.report", "ops", "Read bounded disk/mount observations from an explicitly approved Mac-only recipe.", {"runner": ALIAS}, permission="execute_process", approval=True, backend="job"),
    spec("mac.pressure", "ops", "Read bounded memory/swap/process observations through an explicitly approved Mac-only recipe.", {"runner": ALIAS}, permission="execute_process", approval=True, backend="job"),
    spec("tailscale.status", "ops", "Read bounded, address-redacted peer presence through a configured recipe.", {"runner": ALIAS}, permission="execute_process", approval=True, backend="job"),
    spec("service.health", "ops", "Probe one named operator-configured health endpoint; caller supplies no URL.", {"service": ALIAS}, permission="public_network", backend="http"),
    spec("gmail.search", "google", "Read bounded mailbox metadata with router-held OAuth; no send operation exists.", {"query": text(200), "limit": LIMIT}, permission="public_network", backend="http"),
    spec("gmail.draft", "google", "Create draft only after approval; never sends email; verify draft ID by readback.", {"to": text(200), "subject": text(200), "body": BODY}, permission="write", approval=True, backend="http"),
    spec("calendar.list", "google", "Read events from one configured calendar in a bounded time window.", {"calendar": ALIAS, "start": text(50), "end": text(50), "limit": LIMIT}, permission="public_network", backend="http"),
    spec("calendar.suggest_time", "google", "Suggest a free interval from bounded configured calendar events; never books.", {"calendar": ALIAS, "start": text(50), "end": text(50), "minutes": integer(1, 480)}, permission="public_network", backend="http"),
    spec("drive.search", "google", "Search only an operator-configured Drive folder; query escaped, no unrestricted Drive query syntax.", {"folder": ALIAS, "query": text(200), "limit": LIMIT}, permission="public_network", backend="http"),
    spec("drive.read", "google", "Read metadata or bounded plain-text export of one configured Drive document.", {"file": ALIAS}, permission="public_network", backend="http"),
    spec("contacts.lookup", "google", "Read bounded contact names/emails with router-held Google OAuth.", {"query": text(200), "limit": LIMIT}, permission="public_network", backend="http"),
    spec("notify.push", "notify", "Send one approved redacted message to a configured ntfy destination; deduplicated and rate limited.", {"destination": ALIAS, "text": text(1000)}, permission="write", approval=True, backend="http"),
    spec("dmx.patch_check", "av", "Validate DMX footprints and overlaps without sending network data.", {"fixtures": array(obj(name=text(80), universe=integer(1, 32768), address=integer(1, 512), footprint=integer(1, 512)))}),
    spec("artnet.discover", "av", "Run bounded approved Art-Net discovery on an operator-allowlisted interface recipe.", {"runner": ALIAS, "interface": ALIAS}, permission="execute_process", approval=True, backend="job"),
    spec("osc.send", "av", "Send one approved OSC string to a configured target/address; no caller host or port.", {"target": ALIAS, "address": text(200), "value": text(1000)}, permission="write", approval=True, backend="http"),
    spec("resolume.status", "av", "Read a configured Resolume composition endpoint; no transport changes.", {"service": ALIAS}, permission="public_network", backend="http"),
    spec("midi.map.lookup", "av", "Look up a control in an operator-configured MIDI map.", {"control": text(80)}),
    spec("av.inventory.lookup", "av", "Look up equipment in an operator-configured nonsecret AV inventory.", {"query": text(200), "limit": LIMIT}),
    spec("video.probe", "media", "Run bounded ffprobe metadata extraction on an approved media runner.", {"runner": ALIAS, "path": PATH}, permission="execute_process", approval=True, backend="job"),
    spec("video.gap_find", "media", "Find gaps/overlaps between supplied take intervals; no media decoding.", {"takes": array(obj(name=text(100), start=text(50), end=text(50)))}),
    spec("audio.transcribe", "media", "Run bounded transcription on an approved Whisper-capable runner.", {"runner": ALIAS, "path": PATH, "language": text(20)}, permission="execute_process", approval=True, backend="job"),
    spec("setlist.parse", "media", "Parse bounded setlist lines and optional minute:second durations.", {"text": BODY}),
    spec("calc.eval", "utility", "Evaluate bounded arithmetic AST and explicit unit conversion; never eval or function calls.", {"expression": text(200), "from_unit": text(20, 0), "to_unit": text(20, 0)}),
    spec("time.convert", "utility", "Convert an offset-aware ISO timestamp to an IANA timezone; ambiguous naive times refused.", {"timestamp": text(50), "zone": text(100)}),
    spec("image.describe", "media", "Approved bounded vision request to an installed model with an admitted image; no recursive tools.", {"node": NODE, "model": MODEL, "path": PATH, "text": text(1000), "max_tokens": integer(1, 256)}, permission="execute_process", approval=True, backend="cluster"),
    spec("ocr.image", "media", "Run bounded OCR on an approved capable runner and admitted image.", {"runner": ALIAS, "path": PATH}, permission="execute_process", approval=True, backend="job"),
    spec("data.query", "utility", "Query bounded CSV/JSON records with isolated read-only SQL; extension/network/file access disabled.", {"runner": ALIAS, "path": PATH, "sql": text(2000)}, permission="execute_process", approval=True, backend="job"),
    spec("json.validate", "utility", "Validate bounded JSON against the existing harness schema subset; unsupported keywords refused.", {"document": text(8000), "schema": text(8000)}),
    spec("diagram.render", "utility", "Render Mermaid to an SVG artifact on an approved isolated runner; file-producing effect.", {"runner": ALIAS, "source": text(4000), "output": PATH}, permission="execute_process", approval=True, backend="job"),
    spec("secret.scan", "quality", "Detect common credential families in supplied text; returns locations/kinds, never matched secrets.", {"text": BODY}),
)

BY_NAME = {item.name: item for item in SPECS}
assert len(SPECS) == len(BY_NAME) == 100
COMPANIONS = (
    spec("project.notepad.snapshot", "memory", "Read run-owned notepad and its freshness digest for an approved write proposal.", {}, backend="native"),
    spec("git.write_preview", "git", "Read a named-file Git diff and digest for exact commit approval; no staging.", {"repository": PATH, "files": array(PATH)}, permission="read_files"),
)
ALL_SPECS = SPECS + COMPANIONS
BY_NAME.update({item.name: item for item in COMPANIONS})
