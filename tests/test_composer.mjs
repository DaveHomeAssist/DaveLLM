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
    assert.deepEqual(notices, ["A reply is still being written. Press Stop to end it, or wait for it to finish. Your draft is kept."]);
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
    assert.ok(context.promptInput.focused);  // main replaced Run tools with the composer tool picker
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


test("a second Send cannot enter while the first reads its attachment", async () => {
    let release;
    let reads = 0;
    const reading = new Promise(resolve => { release = resolve; });
    const promptInput = { value: "Keep this draft" };
    const context = load(["sendMessage", "sendPreparedMessage"], {
        approvalBusy: false, state: { streaming: false }, promptInput,
        attachedFilePromptText: () => { reads += 1; return reading; },
        notifyConsole: () => {},
    });
    const first = context.sendMessage();
    const second = context.sendMessage();
    const concurrentReads = reads;
    release({ tooLarge: true });
    await Promise.all([first, second]);
    assert.equal(concurrentReads, 1, "only one send reaches asynchronous preparation");
    assert.equal(promptInput.value, "Keep this draft");
    await context.sendMessage();
    assert.equal(reads, 2, "a rejected attachment releases the send lock for retry");
});


test("an attachment read error releases preparation without losing the draft", async () => {
    let reads = 0;
    const promptInput = { value: "Retry this draft" };
    const context = load(["sendMessage", "sendPreparedMessage"], {
        approvalBusy: false, state: { streaming: false }, promptInput,
        attachedFilePromptText: async () => {
            reads += 1;
            if (reads === 1) throw new Error("Attachment read failed");
            return { tooLarge: true };
        },
        notifyConsole: () => {},
    });
    await assert.rejects(context.sendMessage(), /Attachment read failed/);
    assert.equal(context.state.preparingMessage, false);
    assert.equal(promptInput.value, "Retry this draft");
    await context.sendMessage();
    assert.equal(reads, 2);
    assert.equal(context.state.preparingMessage, false);
});


// TA-02: the happy path. The lock must hold through conversation creation, the
// budget request and the start of the stream, so a second Send (Enter plus the
// button) never posts a second chat request, and it must be released after.
function sendContext({ createConversation, streamFetch }) {
    const notices = [];
    const chatRequests = [];
    const state = {
        streaming: false, sessionId: null, conversations: {}, pendingImages: [],
        nodes: [{ id: "node-a" }], selectedNode: "node-a", modelMeta: { "model-a": {} },
        stats: { totalMessages: 0, totalTokens: 0 }, autoScroll: false,
    };
    const promptInput = { value: "Hello there", style: {} };
    const context = {
        approvalBusy: false, state, promptInput,
        notifyConsole: (message) => notices.push(message),
        attachedFilePromptText: async () => ({ text: "" }),
        window: {
            DavePrompt: { buildDisplayedPrompt: (prompt) => prompt },
            DaveAnticipation: { incrementUsage: (record) => record },
        },
        supportFlag: null, templateSelect: { value: "general" }, selectedProjectId: null,
        currentConversation: () => state.conversations[state.sessionId],
        createNewConversation: () => createConversation(state),
        createConversationFromTemplate: async () => null,
        modelSelect: { value: "model-a" }, routeStatus: null,
        getSelectedModelMeta: () => ({ vision: false }),
        imageStatus: { textContent: "" }, imageInput: { value: "" }, fileInput: null, fileStatus: null, audioStatus: null,
        routerEndpoint: (path) => `http://router.test${path}`,
        authHeaders: (extra = {}) => extra,
        fetch: async (url, init = {}) => {
            if (url.endsWith("/analytics/costs")) return { ok: true, json: async () => ({ total_cost: 0 }) };
            if (url.endsWith("/chat/stream")) {
                chatRequests.push(JSON.parse(init.body));
                return streamFetch();
            }
            throw new Error(`unexpected request ${url}`);
        },
        updateStatsDisplay: () => {}, anticipationRecord: {}, writeAnticipationRecord: () => {}, updateAnticipationPreferences: () => {},
        schedulePromptSizeCheck: () => {}, sessionStorage: { removeItem: () => {} }, DRAFT_SESSION_KEY: "draft",
        undoPredictionBtn: null, renderMessages: () => {}, showRelevantMemories: () => {}, setStreamingUi: () => {},
        renderConversationList: () => {}, renderLiveMessage: () => {}, renderPredictions: () => {},
        readerCanSee: () => true, markConversationUnread: () => {}, loadConversationHistory: async () => {},
        streamStatusText: () => "", confirm: () => true,
        setInterval: () => 0, clearInterval: () => {}, AbortController, TextDecoder, TextEncoder,
        console: { log: () => {}, warn: () => {}, error: () => {} },
    };
    load(["sendMessage", "sendPreparedMessage"], context);
    return { context, notices, chatRequests, state, promptInput };
}

function doneStream() {
    const chunks = [new TextEncoder().encode('data: {"token":"Hi"}\ndata: {"done":true}\n')];
    return {
        ok: true,
        body: { getReader: () => ({ read: async () => (chunks.length ? { done: false, value: chunks.shift() } : { done: true }) }) },
    };
}

test("a second Send during a successful send's preparation posts exactly one chat request", async () => {
    let releaseCreate;
    const creating = new Promise((resolve) => { releaseCreate = resolve; });
    const { context, notices, chatRequests, state } = sendContext({
        createConversation: async (s) => {
            await creating;
            s.sessionId = "c1";
            s.conversations.c1 = { title: "Chat", messages: [] };
        },
        streamFetch: doneStream,
    });

    const first = context.sendMessage();      // Enter key
    const second = context.sendMessage();     // the Send button, same moment
    assert.equal(state.preparingMessage, true, "the first send holds the lock while it prepares");
    releaseCreate();
    await Promise.all([first, second]);

    assert.equal(chatRequests.length, 1, "exactly one chat request");
    assert.equal(chatRequests[0].prompt, "Hello there");
    assert.equal(state.preparingMessage, false, "the lock is released after the send");
    assert.equal(state.streaming, false);
    assert.deepEqual(notices, ["A message is being prepared. Wait for it to send. Your draft is kept."]);
    const reply = state.conversations.c1.messages.at(-1);
    assert.equal(reply.role, "assistant");
    assert.equal(reply.content, "Hi");

    // The next message sends normally once the first has finished.
    context.promptInput.value = "Second message";
    await context.sendMessage();
    assert.equal(chatRequests.length, 2);
    assert.equal(state.preparingMessage, false);
});

test("a stream request that fails releases the send lock and reports the error", async () => {
    let attempts = 0;
    const { context, chatRequests, state } = sendContext({
        createConversation: async (s) => {
            s.sessionId = "c1";
            s.conversations.c1 = { title: "Chat", messages: [] };
        },
        streamFetch: async () => {
            attempts += 1;
            if (attempts === 1) throw new Error("router unreachable");
            return doneStream();
        },
    });

    await context.sendMessage();
    assert.equal(chatRequests.length, 1);
    assert.equal(state.preparingMessage, false, "a failed stream does not leave the lock held");
    assert.equal(state.streaming, false);
    const messages = state.conversations.c1.messages;
    assert.equal(messages.at(-2).content, "Hello there", "the sent prompt stays in the conversation");
    assert.equal(messages.at(-1).role, "error");
    assert.match(messages.at(-1).content, /router unreachable/);

    context.promptInput.value = "Retry";
    await context.sendMessage();
    assert.equal(chatRequests.length, 2, "the user can send again after the failure");
});
