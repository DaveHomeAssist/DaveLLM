import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const context = { URLSearchParams, Uint8ClampedArray };
vm.createContext(context);
vm.runInContext(readFileSync(new URL("../static/image-tools.js", import.meta.url), "utf8"), context);
const tools = context.DaveImageTools;

test("remix reloads prompt, seed, size and settings from a manifest", () => {
    const fields = tools.remixFields({
        original_prompt: "foggy pier", structured_prompt: "a foggy pier", negative_prompt: "text", seed: 42,
        settings: { width: 832, height: 640, steps: 6, cfg: 1.5, sampler: "euler_ancestral", scheduler: "karras", mode: "img2img", strength: 0.35 },
        style_preset: { id: "cinematic", name: "Cinematic", text: "..." },
        source: { kind: "library", id: "img-20261117T000000Z-0000aaaa" },
    });
    assert.equal(fields.prompt, "foggy pier");
    assert.equal(fields.seed, 42);
    assert.deepEqual([fields.width, fields.height, fields.steps, fields.cfg], [832, 640, 6, 1.5]);
    assert.equal(fields.sampler, "euler_ancestral");
    assert.equal(fields.mode, "img2img");
    assert.equal(fields.strength, 0.35);
    assert.equal(fields.stylePreset, "cinematic");
    assert.equal(fields.sourceImageId, "img-20261117T000000Z-0000aaaa");
    const upload = tools.remixFields({ seed: 1, settings: { mode: "inpaint" }, source: { kind: "upload", sha256: "x" } });
    assert.equal(upload.sourceImageId, null);
    assert.equal(upload.needsNewSource, true);
    assert.equal(upload.needsNewMask, true);
    const empty = tools.remixFields({});
    assert.equal(empty.mode, "txt2img");
    assert.equal(empty.seed, null);
    assert.equal(empty.cfg, 1);
});

test("size options come from aspect presets and keep a custom size", () => {
    const aspects = [{ ratio: "1:1", tier: "standard", width: 768, height: 768 }, { ratio: "16:9", tier: "max", width: 1344, height: 768 }];
    const options = tools.sizeOptions(aspects, "768x768");
    assert.deepEqual(options.map((o) => o.value), ["768x768", "1344x768"]);
    assert.equal(options[1].label, "16:9 · 1344 × 768 · max");
    assert.equal(tools.sizeOptions(aspects, "512x320")[0].label, "Custom · 512 × 320");
});

test("library query encodes gallery filters", () => {
    assert.equal(tools.libraryQuery({ q: " red bike ", since: "2026-11-01", seed: 7, favorite: true, collection: "Q4" }),
        "?q=red+bike&since=2026-11-01&seed=7&favorite=true&collection=Q4&limit=120");
    assert.equal(tools.libraryQuery({ seed: "" }), "?limit=120");
});

test("job status text and progress", () => {
    assert.equal(tools.jobStatusText({ status: "queued", position: 2 }), "Queued · #2 in line");
    assert.equal(tools.jobStatusText({ status: "running", progress: { value: 3, max: 8 } }), "Running · step 3/8");
    assert.equal(tools.jobStatusText({ status: "running", progress: { value: 0, max: 0 } }), "Running");
    assert.equal(tools.jobStatusText({ status: "failed", error: "boom" }), "Failed · boom");
    assert.equal(tools.jobFraction({ status: "running", progress: { value: 2, max: 8 } }), 0.25);
    assert.equal(tools.jobFraction({ status: "done" }), 1);
});

test("collections and negative presets merge without duplicates", () => {
    assert.deepEqual(Array.from(tools.parseCollections(" Posters , Q4,Posters,, ")), ["Posters", "Q4"]);
    assert.equal(tools.appendNegative("text, blurry", "blurry, watermark"), "text, blurry, watermark");
    assert.equal(tools.appendNegative("", "text"), "text");
});

test("mask export turns painted pixels white and the rest black", () => {
    const rgba = new Uint8ClampedArray([255, 0, 0, 120, 0, 0, 0, 0]);
    assert.deepEqual(Array.from(tools.maskFromAlpha(rgba)), [255, 255, 255, 255, 0, 0, 0, 255]);
    assert.equal(tools.hasMaskPixels(rgba), true);
    assert.equal(tools.hasMaskPixels(new Uint8ClampedArray(8)), false);
});
