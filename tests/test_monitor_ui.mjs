import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../static/console.js", import.meta.url), "utf8");

function extract(name) {
    const start = source.search(new RegExp(`^function ${name}\\(`, "m"));
    assert.ok(start >= 0, `${name} is defined`);
    const end = source.indexOf("\n}\n", start);
    assert.ok(end > start, `${name} ends`);
    return source.slice(start, end + 2);
}

function element() {
    return {
        textContent: "", className: "", hidden: false, children: [], dataset: {}, title: "",
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children = [...children]; },
        get childElementCount() { return this.children.length; },
    };
}

function loadMonitor() {
    const ids = Object.fromEntries([
        "onCallState", "onCallFreshness", "onCallSummary", "onCallTargets", "onCallRecovery", "onCallHistory",
    ].map((id) => [id, element()]));
    const context = {
        document: { getElementById: (id) => ids[id] },
        uiElement: (_tag, className = "", text = "") => Object.assign(element(), { className, textContent: text }),
        console,
    };
    vm.createContext(context);
    vm.runInContext(`${extract("monitorAge")}\n${extract("renderOnCallStatus")}\nglobalThis.renderOnCallStatus = renderOnCallStatus;`, context);
    return { context, ids };
}

test("on-call monitor renders a healthy structured snapshot and history", () => {
    const { context, ids } = loadMonitor();
    context.renderOnCallStatus({ status: "fulfilled", value: {
        current: {
            checked_at: new Date().toISOString(), overall: "healthy", summary: "All targets healthy.", recovery: null,
            targets: [{ name: "Walter", endpoint: "http://100.64.0.2:11434/api/tags", status: "healthy", latency_ms: 85.776, http_status: 200 }],
        },
        history: [{ checked_at: new Date().toISOString(), overall: "healthy", summary: "All targets healthy." }],
        error: null,
    }});
    assert.equal(ids.onCallState.textContent, "healthy");
    assert.match(ids.onCallState.className, /oncall-state-healthy/);
    assert.equal(ids.onCallSummary.textContent, "All targets healthy.");
    assert.equal(ids.onCallTargets.children[0].children[0].textContent, "Walter");
    assert.equal(ids.onCallTargets.children[0].children[2].textContent, "85.8 ms · HTTP 200");
    assert.equal(ids.onCallHistory.children.length, 1);
});

test("on-call monitor never presents missing or stale data as healthy", () => {
    const missing = loadMonitor();
    missing.context.renderOnCallStatus({ status: "fulfilled", value: { current: null, history: [], error: "No status yet." } });
    assert.equal(missing.ids.onCallState.textContent, "Waiting");
    assert.match(missing.ids.onCallState.className, /unknown/);

    const stale = loadMonitor();
    stale.context.renderOnCallStatus({ status: "fulfilled", value: {
        current: { checked_at: "2020-01-01T00:00:00Z", overall: "healthy", summary: "Old result", targets: [] },
        history: [], error: null,
    }});
    assert.equal(stale.ids.onCallState.textContent, "Stale");
    assert.match(stale.ids.onCallState.className, /degraded/);
});
