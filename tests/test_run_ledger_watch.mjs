import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

test("a late frame from a prior run cannot advance the new run cursor", async () => {
    const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
    const start = source.indexOf("async function watchToolRun(runId) {");
    const end = source.indexOf("\nasync function startToolRun()", start);
    assert.ok(start >= 0 && end > start);
    let releaseOld;
    let oldReaderStarted;
    const oldReading = new Promise((resolve) => { oldReaderStarted = resolve; });
    const oldFrame = new Promise((resolve) => { releaseOld = resolve; });
    const requested = [];
    const encode = (event) => new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`);
    const context = {
        activeToolRunId: "run_old",
        toolRunState: { runId: "run_old", cursor: 0, events: [] },
        toolRunStreamAbort: null,
        runLedgerRetry: { classList: { add() {}, remove() {} } },
        runLedgerReason: { textContent: "" },
        routerEndpoint: (path) => path,
        authHeaders: (headers) => headers,
        refreshToolRun: async (runId) => ({ status: runId === "run_new" ? "completed" : "running" }),
        TERMINAL_TOOL_RUNS: new Set(["completed"]),
        AbortController,
        TextDecoder,
        setTimeout,
        fetch: async (url) => {
            requested.push(url);
            if (url.includes("run_old")) {
                return { ok: true, body: { getReader: () => ({
                    read: async () => {
                        oldReaderStarted();
                        await oldFrame;
                        return { done: false, value: encode({ sequence: 99, kind: "terminal" }) };
                    }
                }) } };
            }
            let delivered = false;
            return { ok: true, body: { getReader: () => ({ read: async () => {
                if (delivered) return { done: true };
                delivered = true;
                return { done: false, value: encode({ sequence: 1, kind: "terminal" }) };
            } }) } };
        }
    };
    vm.createContext(context);
    vm.runInContext(`${source.slice(start, end)}\nglobalThis.watchToolRun = watchToolRun;`, context);
    const oldWatch = context.watchToolRun("run_old");
    await oldReading;
    context.toolRunStreamAbort.abort();
    context.activeToolRunId = "run_new";
    context.toolRunState = { runId: "run_new", cursor: 0, events: [] };
    const newWatch = context.watchToolRun("run_new");
    releaseOld();
    await Promise.all([oldWatch, newWatch]);
    assert.equal(context.toolRunState.cursor, 1);
    assert.deepEqual(context.toolRunState.events.map((event) => event.sequence), [1]);
    assert.ok(requested.some((url) => url.includes("run_new/events?stream=true&after=0")));
});
