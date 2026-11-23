// Backlog: "Surface model stream failures instead of normal completion" (acceptance criteria).
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

function harness() {
    const context = {
        document: {
            createElement: (tag) => ({
                tag, className: "", textContent: "", attributes: {},
                setAttribute(name, value) { this.attributes[name] = value; },
            }),
        },
    };
    vm.createContext(context);
    vm.runInContext(`${extract("endReplyWithError")}\n${extract("incompleteNotice")}\n`
        + "globalThis.endReplyWithError = endReplyWithError; globalThis.incompleteNotice = incompleteNotice;", context);
    return context;
}

test("a stream that fails after partial text keeps the text and marks it incomplete", () => {
    const { endReplyWithError } = harness();
    const msg = { role: "assistant", content: "The first half of", isStreaming: true };
    endReplyWithError(msg, "Model stopped with an error: runner crashed");
    assert.equal(msg.role, "assistant");
    assert.equal(msg.content, "The first half of");
    assert.equal(msg.incomplete, true);
    assert.equal(msg.error, "Model stopped with an error: runner crashed");
});

test("a stream that fails before any text shows the error instead of a blank reply", () => {
    const { endReplyWithError } = harness();
    const msg = { role: "assistant", content: " \n", isStreaming: true };
    endReplyWithError(msg, "Cannot connect to node");
    assert.equal(msg.role, "error");
    assert.equal(msg.content, "❌ Error: Cannot connect to node");
    assert.equal(msg.incomplete, undefined);
});

test("an incomplete reply loaded from history says it is incomplete and why", () => {
    const { incompleteNotice } = harness();
    const notice = incompleteNotice({ role: "assistant", content: "partial", incomplete: true,
        error: "Model stopped with an error: runner crashed" });
    assert.equal(notice.className, "message-incomplete");
    assert.equal(notice.attributes.role, "note");
    assert.equal(notice.textContent, "Incomplete reply: Model stopped with an error: runner crashed");
});

test("renderMessages shows the incomplete notice for finished replies", () => {
    const render = extract("renderMessages");
    assert.match(render, /if \(!m\.isStreaming && m\.incomplete\) \{[\s\S]*?incompleteNotice\(m\)/);
});
