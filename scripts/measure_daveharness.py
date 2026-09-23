"""Measure request admission at DaveLLM's expected size and twice that load."""

import json
import asyncio
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daveharness import EventJournal, RunEvent, RunRequest


def measure():
    rows = []
    for count in (1_000_000, 2_000_000):
        messages = [{"role": "user", "content": "x" * count}]
        tracemalloc.start()
        started = time.perf_counter()
        request = RunRequest.create(messages)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rows.append({"input_characters": count, "transcript_bytes": len(request.messages_json),
                     "elapsed_seconds": round(elapsed, 4), "peak_allocated_bytes": peak,
                     "passed": elapsed < 30 and peak < 32 * 1024 * 1024})
    return rows


async def measure_events():
    rows = []
    for count in (1000, 2000):
        journal = EventJournal()
        tracemalloc.start()
        started = time.perf_counter()
        for sequence in range(1, count + 1):
            journal.record(RunEvent("run", sequence, "terminal" if sequence == count else "model_result",
                                    1, None, None, None, None, "2026-09-23T12:00:00+00:00"))
        await journal.flush()
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        retained = journal.events("run")
        retained_bytes = sum(event.byte_size for event in retained)
        rows.append({"input_events": count, "retained_events": len(retained), "retained_bytes": retained_bytes,
                     "elapsed_seconds": round(elapsed, 4), "peak_allocated_bytes": peak,
                     "passed": elapsed < 30 and peak < 8 * 1024 * 1024 and len(retained) <= 1000
                     and retained_bytes <= 1_048_576 and retained[-1].kind == "terminal"})
    return rows


if __name__ == "__main__":
    requests = measure()
    events = asyncio.run(measure_events())
    report = {"report_version": 1, "requests": requests, "events": events,
              "passed": all(row["passed"] for row in requests + events)}
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
