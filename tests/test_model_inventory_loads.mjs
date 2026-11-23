// Backlog: "Discard stale model inventories after node changes".
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");

function extract(name) {
    const start = source.search(new RegExp(`^(async )?function ${name}\\(`, "m"));
    assert.ok(start >= 0, `${name} is defined`);
    const end = source.indexOf("\n}\n", start);
    return source.slice(start, end + 2);
}

function select() {
    return {
        options: [], value: "", disabled: false,
        replaceChildren() { this.options = []; },
        appendChild(option) { this.options.push(option); if (this.options.length === 1) this.value = option.value; },
    };
}

// loadModelsFromNode against stubs; each fetch waits until the test answers it.
function harness() {
    const pending = new Map();
    const modelSelect = select();
    const context = {
        modelLoadSequence: 0, modelSelect,
        state: { selectedNode: "A", nodes: [{ id: "A" }, { id: "B" }], modelMeta: {}, selectionFallback: false },
        document: { createElement: () => ({ value: "", textContent: "", title: "", dataset: {} }) },
        replaceSelectOptions: (target, label) => { target.options = [{ value: "", textContent: label }]; target.value = ""; },
        normalizeModelMeta: (model) => ({ id: model.name, vision: Boolean(model.vision) }),
        routerEndpoint: (path) => path, authHeaders: () => ({}),
        notifyConsole() {}, updateImageSupportNotice() {}, updateContextStrip() {}, renderPredictions() {},
        console: { log() {}, error() {} },
        fetch: (url) => new Promise((resolve) => pending.set(url.split("/")[2], resolve)),
    };
    vm.createContext(context);
    vm.runInContext(`${extract("loadModelsFromNode")}\nglobalThis.loadModelsFromNode = loadModelsFromNode;`, context);
    const answer = (nodeId, names) => pending.get(nodeId)({
        ok: true, json: async () => ({ models: names.map((name) => ({ name })) }),
    });
    const fail = (nodeId) => pending.get(nodeId)({ ok: false, status: 502 });
    return { context, modelSelect, answer, fail };
}

const optionValues = (target) => target.options.map((option) => option.value);

test("node A's late inventory never replaces node B's models", async () => {
    const { context, modelSelect, answer } = harness();
    const loadA = context.loadModelsFromNode();
    context.state.selectedNode = "B";
    const loadB = context.loadModelsFromNode();
    answer("B", ["b-model"]);
    assert.equal(await loadB, "b-model");
    answer("A", ["a-model"]);
    assert.equal(await loadA, null);
    assert.deepEqual(optionValues(modelSelect), ["b-model"]);
    assert.equal(modelSelect.value, "b-model");
    assert.deepEqual(Object.keys(context.state.modelMeta), ["b-model"]);
});

test("node A's late failure leaves node B's models in place", async () => {
    const { context, modelSelect, answer, fail } = harness();
    const loadA = context.loadModelsFromNode();
    context.state.selectedNode = "B";
    const loadB = context.loadModelsFromNode();
    answer("B", ["b-model"]);
    await loadB;
    fail("A");
    await loadA;
    assert.deepEqual(optionValues(modelSelect), ["b-model"]);
    assert.deepEqual(Object.keys(context.state.modelMeta), ["b-model"]);
});

test("a single request still fills the list for the selected node", async () => {
    const { context, modelSelect, answer } = harness();
    const load = context.loadModelsFromNode();
    answer("A", ["a-model", "a-other"]);
    assert.equal(await load, "a-model");
    assert.deepEqual(optionValues(modelSelect), ["a-model", "a-other"]);
    assert.equal(modelSelect.disabled, false);
});
