import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");

// One top-level function from app.js, so it runs against stubs without the DOM.
function extract(name) {
    const start = source.search(new RegExp(`^(async )?function ${name}\\(`, "m"));
    assert.ok(start >= 0, `${name} is defined`);
    const end = source.indexOf("\n}\n", start);
    assert.ok(end > start, `${name} ends`);
    return source.slice(start, end + 2);
}

function load(names, context) {
    vm.createContext(context);
    vm.runInContext(`${names.map(extract).join("\n")}\n${names.map((name) => `globalThis.${name} = ${name};`).join("\n")}`, context);
    return context;
}

function classes(initial = []) {
    const set = new Set(initial);
    return {
        add: (name) => set.add(name),
        remove: (name) => set.delete(name),
        toggle: (name, on) => (on ? set.add(name) : set.delete(name)),
        contains: (name) => set.has(name),
    };
}

function element(initialClasses = []) {
    return {
        textContent: "", title: "", disabled: false, attributes: {}, dataset: {}, focused: false,
        classList: classes(initialClasses),
        setAttribute(name, value) { this.attributes[name] = value; },
        replaceChildren() { this.children = []; },
        focus() { this.focused = true; },
    };
}

test("the Send button says Stop while a reply streams and Send after", () => {
    const sendBtn = element();
    const responseBox = element();
    const context = load(["setStreamingUi"], { sendBtn, responseBox });
    context.setStreamingUi(true);
    assert.equal(sendBtn.textContent, "Stop");
    assert.equal(sendBtn.attributes["aria-label"], "Stop the reply");
    assert.ok(sendBtn.classList.contains("is-stop"));
    assert.equal(responseBox.attributes["aria-busy"], "true");
    context.setStreamingUi(false);
    assert.equal(sendBtn.textContent, "Send");
    assert.equal(sendBtn.attributes["aria-label"], "Send message");
    assert.equal(responseBox.attributes["aria-busy"], "false");
    assert.ok(!sendBtn.classList.contains("is-stop"));
});

test("Enter during a streaming reply keeps the draft instead of starting a second reply", async () => {
    const notices = [];
    const promptInput = { value: "next question" };
    const context = load(["sendMessage"], {
        approvalBusy: false,
        state: { streaming: true },
        promptInput,
        notifyConsole: (message) => notices.push(message),
    });
    await context.sendMessage();
    assert.equal(promptInput.value, "next question");
    assert.deepEqual(notices, ["A reply is still coming. Press Stop to end it; your draft is kept."]);
});

function ledgerContext(overrides = {}) {
    const runLedger = element(["hidden"]);
    const aborted = [];
    const context = {
        toolRunClosable: true,
        activeToolRunId: null,
        toolRunState: { runId: null, cursor: 0, events: [] },
        toolRunStreamAbort: { abort: () => aborted.push(true) },
        runLedger,
        runLedgerClose: element(["hidden"]),
        runLedgerRetry: element(),
        runLedgerReason: element(),
        runLedgerEvents: element(),
        runLedgerTranscript: element(),
        runLedgerApproval: element(),
        runLedgerStatus: element(),
        runLedgerStop: element(),
        runToolsBtn: element(),
        promptInput: element(),
        aborted,
        ...overrides,
    };
    return load(["setToolRunClosable", "closeToolRun", "showToolRunNotice"], context);
}

test("a finished run's card closes and clears; a live run's card stays", () => {
    const context = ledgerContext({ activeToolRunId: "run_1", toolRunState: { runId: "run_1", cursor: 5, events: [{}] } });
    context.runLedger.classList.remove("hidden");
    context.setToolRunClosable(false);  // running
    assert.ok(context.runLedgerClose.classList.contains("hidden"));
    context.closeToolRun();
    assert.ok(!context.runLedger.classList.contains("hidden"));
    assert.equal(context.activeToolRunId, "run_1");

    context.setToolRunClosable(true);  // cancelled, completed or another terminal status
    assert.ok(!context.runLedgerClose.classList.contains("hidden"));
    context.closeToolRun();
    assert.ok(context.runLedger.classList.contains("hidden"));
    assert.equal(context.activeToolRunId, null);
    assert.equal(JSON.stringify(context.toolRunState), JSON.stringify({ runId: null, cursor: 0, events: [] }));
    assert.equal(context.aborted.length, 1);
    assert.equal(context.runLedgerStatus.textContent, "Ready");
    assert.ok(context.runToolsBtn.focused);
});

test("a start that fails shows a closable notice but never hides a live run", () => {
    const idle = ledgerContext();
    idle.showToolRunNotice("Enter a message and select a node and model first.");
    assert.ok(!idle.runLedger.classList.contains("hidden"));
    assert.equal(idle.toolRunClosable, true);
    assert.ok(!idle.runLedgerClose.classList.contains("hidden"));

    const live = ledgerContext({ activeToolRunId: "run_2", toolRunClosable: false });
    live.runLedger.classList.remove("hidden");
    live.showToolRunNotice("Run request failed (500)");
    assert.equal(live.toolRunClosable, false);
    assert.equal(live.runLedgerReason.textContent, "Run request failed (500)");
});

test("the size check renders only the latest draft's answer", async () => {
    const rendered = [];
    const pending = [];
    const context = load(["schedulePromptSizeCheck", "runPromptSizeCheck"], {
        PROMPT_SIZE_DEBOUNCE_MS: 600,
        promptSizeTimer: null, promptSizeAbort: null, promptSizeSeq: 0,
        promptInput: { value: "draft" },
        supportFlag: { checked: false },
        state: { selectedNode: "duncan", modelMeta: { "gpt-oss:120b": {} }, sessionId: "c1" },
        modelSelect: { value: "gpt-oss:120b" },
        currentConversation: () => ({ project_id: "p1" }),
        attachedFilePromptText: async () => ({ text: "" }),
        window: { DavePrompt: { buildDisplayedPrompt: (prompt, file) => `${prompt}${file}` } },
        routerEndpoint: (path) => path,
        authHeaders: (headers) => headers,
        AbortController,
        clearTimeout,
        setTimeout: () => 0,
        renderPromptSize: (report) => rendered.push(report),
        fetch: (url, options) => new Promise((resolve) => pending.push({ url, body: JSON.parse(options.body), resolve })),
    });
    const first = context.runPromptSizeCheck();
    const second = context.runPromptSizeCheck();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(pending.length, 2);
    assert.equal(pending[0].url, "/chat/size-check");
    assert.deepEqual(pending[1].body, {
        conversation_id: "c1", prompt: "draft", node_id: "duncan", model: "gpt-oss:120b", project_id: "p1", max_tokens: 2048,
    });
    pending[1].resolve({ ok: true, json: async () => ({ prompt_tokens: 13520 }) });
    pending[0].resolve({ ok: true, json: async () => ({ prompt_tokens: 1 }) });
    await Promise.all([first, second]);
    assert.deepEqual(rendered, [{ prompt_tokens: 13520 }]);

    context.promptInput.value = "   ";
    context.schedulePromptSizeCheck();
    assert.deepEqual(rendered.at(-1), null);  // an empty draft clears the line without a request
    assert.equal(pending.length, 2);
});
