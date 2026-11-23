// Backlog: "Fix vision capability detection for Ollama models" (frontend half).
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

const context = vm.createContext({});
vm.runInContext(`${extract("modelIdLooksVisionCapable")}\n${extract("normalizeModelMeta")}\n`
    + "globalThis.normalizeModelMeta = normalizeModelMeta;", context);
const meta = (model) => ({ ...context.normalizeModelMeta(model) });

test("the router's Ollama capabilities win over the model name, both ways", () => {
    assert.deepEqual(meta({ id: "qwen3.5:4b", vision: true, capabilities_source: "ollama" }),
        { id: "qwen3.5:4b", vision: true, capabilitiesSource: "ollama" });
    assert.deepEqual(meta({ id: "vision-helper:latest", vision: false, capabilities_source: "ollama" }),
        { id: "vision-helper:latest", vision: false, capabilitiesSource: "ollama" });
});

test("without Ollama capabilities the name guess still applies and is labelled as a guess", () => {
    assert.deepEqual(meta({ id: "llava-vision:7b", vision: true, capabilities_source: "name" }),
        { id: "llava-vision:7b", vision: true, capabilitiesSource: "name" });
    assert.deepEqual(meta("qwen2.5vl:7b"), { id: "qwen2.5vl:7b", vision: true, capabilitiesSource: "name" });
    assert.deepEqual(meta({ id: "gemma3:27b", vision: false }), { id: "gemma3:27b", vision: false, capabilitiesSource: "name" });
});
