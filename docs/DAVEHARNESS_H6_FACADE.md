# DaveHarness 0.8.0 facade and store

`Harness` owns one registry, model invoker, policy, optional cancellable runner, clock, event sink, active controllers, and run journals. `start`, `snapshot`, `decide`, `resume`, `cancel`, and cursor-based `events` operate on that instance. `RunRequest` defensively copies finite JSON messages; the host supplies model metadata, permissions, and any assembled context. DaveHarness never reads DaveLLM BRAIN, project data, credentials, environment, database, or network configuration.

`RunStore` requires `create`, `load`, and `compare_and_swap`. The default `InMemoryRunStore` keeps at most 128 runs, 16 MiB of serialized snapshots, and one hour of retention from insertion. It uses defensive JSON round trips, a lock, optimistic versions, oldest-first eviction, and explicit expiry lookup. Reads and saves do not extend TTL. An overlarge new run is rejected; a save that would exceed the byte ceiling loses CAS without invoking another effect. The facade removes its journal and controller when the in-memory store evicts or expires a run. Hosts that inject another store own its eviction and journal cleanup policy. This store does not provide process-restart durability.

| API | Support | Contract |
|---|---|---|
| `RunRequest`, `RunSnapshot`, `ApprovalDecision`, `RunEvent` | New lifecycle API from 0.8.0 | Versioned or validated values; generic host inputs only |
| `RunStore`, `InMemoryRunStore`, `Harness` | New lifecycle API from 0.8.0 | Instance-owned dependencies and bounded state |
| `Harness.start`, `snapshot`, `decide`, `resume`, `cancel`, `events` | New lifecycle API from 0.8.0 | Structured outcomes, exact decisions, cursor replay |
| `run_executor_loop`, `resume_executor_loop`, `run_tool` | Compatibility API | Shipped status and outcome fields remain stable; shared default registry and pending store live only in the outer adapter |
| `DEFAULT_TOOL_REGISTRY`, `DEFAULT_PENDING_CALL_STORE` | Compatibility defaults | Legacy callers may continue to use them; new lifecycle code injects explicit instances |
| Root `tool_executor.py` imports | DaveLLM 2.x compatibility | Re-export only; no root-shim removal before a separately approved DaveLLM major version |

No public name in DaveHarness `1.x` is removed. Deprecation requires a documented replacement and a separate release decision. The legacy loop retains its existing in-memory approval and response-deadline limitations; the facade uses serializable snapshots, exact decisions, events, and bounded host memory. Neither API promises hard cancellation for a noncooperative synchronous handler.
