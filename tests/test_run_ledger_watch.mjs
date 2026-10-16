import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

function ledgerHarness() {
    class Element {
        constructor() {
            this.textContent = "";
            this.children = [];
            this.dataset = {};
            this.classList = { add() {}, remove() {}, toggle() {} };
        }
        replaceChildren(...children) { this.children = children; }
        prepend(...children) { this.children.unshift(...children); }
        append(...children) { this.children.push(...children); }
        appendChild(child) { this.children.push(child); }
    }
    const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
    const start = source.indexOf("function renderToolRun(run) {");
    const end = source.indexOf("\nasync function attachNotionContext", start);
    const context = {
        activeToolRunId: "run_t20", toolRunState: { events: [] },
        TERMINAL_TOOL_RUNS: new Set(["completed", "error_budget"]),
        document: { createElement: () => new Element() },
        ...Object.fromEntries(["runLedger", "runLedgerStatus", "runLedgerReason", "runLedgerEvents",
            "runLedgerTranscript", "runLedgerApproval", "runLedgerStop"].map((key) => [key, new Element()])),
    };
    context.setToolRunClosable = (closable) => { context.toolRunClosable = closable; };
    vm.createContext(context);
    vm.runInContext(source.slice(start, end), context);
    return context;
}

test("error-budget rendering shows the harness explanation and both literal errors without duplication", () => {
    const context = ledgerHarness();
    const errors = ["File not found", "Access denied: path is not allowed <script>not markup</script>"];
    const run = {
        run_id: "run_t20", status: "error_budget", reason_code: "error_budget",
        snapshot: { transcript: [], steps: 1, pending_call: null },
        terminal_explanation: {
            source: "harness", message: "Stopped at the error limit (2/2). No further model or tool calls were made.",
            tool_errors: errors.map((error, index) => ({ call_id: `call_${index}`, tool_name: "file.read_lines", error })),
        },
    };
    context.renderToolRun(run);
    const text = (element) => [element.textContent, ...element.children.map(text)].join("\n");
    const first = text(context.runLedgerTranscript);
    assert.match(first, /Harness report/);
    assert.ok(first.includes(run.terminal_explanation.message));
    for (const error of errors) assert.ok(first.includes(error));
    assert.equal(context.runLedgerStop.disabled, true);
    assert.equal(context.toolRunClosable, true);  // a terminal run's card can close
    context.renderToolRun(run);
    assert.equal(text(context.runLedgerTranscript), first);
    context.renderToolRun({ ...run, run_id: "run_old" });
    assert.equal(text(context.runLedgerTranscript), first);
    context.renderToolRun({ ...run, status: "completed", reason_code: "completed", terminal_explanation: null });
    assert.doesNotMatch(text(context.runLedgerTranscript), /Harness report|File not found/);
});

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

test("tool steps read as plain words, without doubled numbers, with raw output folded away", () => {
    const context = ledgerHarness();
    const call = (id, name, args) => ({ id, type: "function", function: { name, arguments: JSON.stringify(args) } });
    const result = (id, payload) => ({ role: "tool", tool_call_id: id, content: JSON.stringify(payload) });
    const run = {
        run_id: "run_t20", status: "completed", reason_code: "completed", terminal_explanation: null,
        snapshot: { steps: 2, pending_call: null, transcript: [
            { role: "user", content: "find it" },
            { role: "assistant", content: "", tool_calls: [
                call("a", "file.search", { query: "approvalPreview" }),
                call("b", "calc.eval", { expression: "2+2" }),
                call("c", "md.outline", { path: "x.md" }),
            ] },
            result("a", { status: "success", result: JSON.stringify({ matches: [{}, {}], path_matches: ["static/app.js"] }) }),
            result("b", { status: "success", result: JSON.stringify({ value: 4 }) }),
            result("c", { status: "error", result: "", error: "File not found; list or search the folder to check the path" }),
            { role: "assistant", content: "Found it." },
        ] },
    };
    context.renderToolRun(run);
    const steps = context.runLedgerEvents.children.map((item) => item.textContent);
    assert.deepEqual(steps, [
        "✓ Searched files for “approvalPreview” · 2 matches, 1 file name",
        "✓ Calculated 2+2 · = 4",
        "✗ Outlined x.md · File not found; list or search the folder to check the path",
    ]);
    assert.ok(steps.every((line) => !/^\d+\./.test(line)));
    const [raw] = context.runLedgerTranscript.children;
    assert.equal(raw.children[0].textContent, "Raw tool output");
    assert.ok(raw.children.some((block) => block.textContent === "assistant: Found it."));
});

test("a call awaiting approval and a thinking model show as active steps", () => {
    const context = ledgerHarness();
    const steps = context.toolRunSteps({ status: "approval_required", snapshot: {
        pending_call: { call_id: "e" }, model_inflight: false,
        transcript: [{ role: "assistant", content: "", tool_calls: [
            { id: "e", function: { name: "file.edit", arguments: { path: "a.md" } } },
        ] }],
    } });
    assert.deepEqual(JSON.parse(JSON.stringify(steps)), [{ state: "active", text: "… Edit a.md · waiting for your approval" }]);
    const thinking = context.toolRunSteps({ status: "running", snapshot: { transcript: [], model_inflight: true } });
    assert.equal(thinking[0].text, "… Thinking");
    const direct = context.toolRunSteps({ status: "completed", snapshot: { transcript: [], model_inflight: false } });
    assert.equal(direct[0].text, "✓ Answered without using a tool");
    const textCall = context.toolRunSteps({ status: "completed", snapshot: {
        transcript: [], model_inflight: false, last_content: "Let me look. <function=file.search> <parameter=query>x</parameter>",
    } });
    assert.deepEqual(JSON.parse(JSON.stringify(textCall)), [{ state: "failed", text: "✗ The model wrote a tool call as text, so it did not run" }]);
});
