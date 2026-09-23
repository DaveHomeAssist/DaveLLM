"""Offline qualification. No network adapters, files-as-tools, or live models."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from daveharness import ApprovalDecision, Harness, RunBudget, RunRequest, ToolDefinition, ToolRegistry


async def evaluate(case, adapter):
    effects = []
    unauthorized = []
    approved = False
    entered = asyncio.Event()
    cursor = 0
    permissions = tuple(case.get("permissions", ["read", "write"]))

    def handler(name):
        def invoke(_args):
            effects.append(name)
            if name not in permissions or (name == "write" and not approved):
                unauthorized.append(name)
            return "ok"
        return invoke

    registry = ToolRegistry()
    for name in ("read", "write"):
        registry.register(ToolDefinition(
            name, "Disposable in-memory counter", {"type": "object"}, handler(name),
            permission=name, approval_required=name == "write", cancellation="bounded",
            handler_version="qualification-v1",
        ))

    async def model(_messages, _schemas):
        nonlocal cursor
        entered.set()
        if case.get("mode") in {"timeout", "cancel"}:
            await asyncio.Event().wait()
        response = case["responses"][min(cursor, len(case["responses"]) - 1)]
        cursor += 1
        # Exercise both injected mappings and a replayed JSON wire recording.
        return response if adapter == "fake" else json.loads(json.dumps(response))

    harness = Harness(registry=registry, invoke_model=model, model_timeout_seconds=0.05)
    task = asyncio.create_task(harness.start(RunRequest.create(
        [{"role": "user", "content": "qualification"}], run_id="qualification_run",
        allowed_permissions=permissions, budget=RunBudget(**case.get("budget", {})),
    )))
    if case.get("mode") == "cancel":
        await entered.wait()
        await harness.cancel("qualification_run")
    result = await task
    if result.status == "approval_required" and case.get("decision"):
        pending = result.snapshot.pending_call
        approved = case["decision"] == "approve"
        result = await harness.decide(ApprovalDecision(
            run_id="qualification_run", call_id=pending.call_id, digest=pending.digest,
            definition_fingerprint=pending.definition_fingerprint, permission=pending.permission,
            nonce=pending.nonce, decision_id="qualification_decision", decision=case["decision"],
            issued_at=pending.created_at, expires_at=pending.expires_at,
        ))
    final = harness.snapshot("qualification_run")
    terminals = sum(event.kind == "terminal" for event in harness.events("qualification_run"))
    passed = final.status == case["status"] and len(effects) == case["effects"] and not unauthorized and terminals == 1
    return {"case": case["id"], "adapter": adapter, "status": final.status,
            "effects": len(effects), "unauthorized_effects": len(unauthorized),
            "terminal_events": terminals, "passed": passed}


async def qualify():
    corpus = json.loads((ROOT / "tests/fixtures/daveharness/corpus.json").read_text())
    results = [await evaluate(case, adapter) for adapter in ("fake", "recorded") for case in corpus["cases"]]
    return {"report_version": 1, "live_models": False, "provenance": corpus["provenance"],
            "evaluations": len(results), "passed": all(row["passed"] for row in results),
            "unauthorized_effects": sum(row["unauthorized_effects"] for row in results), "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(qualify())
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end="")
    raise SystemExit(0 if report["passed"] else 1)
