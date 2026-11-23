// Pure helpers for the image dialog. No DOM access, so tests/test_image_tools.mjs can run them in Node.
(function (root) {
    "use strict";

    // Remix: everything needed to put a saved image's recipe back into the form.
    function remixFields(manifest) {
        const m = manifest || {};
        const s = m.settings || {};
        const source = m.source || null;
        return {
            prompt: m.original_prompt || "",
            negative: m.negative_prompt || "",
            seed: Number.isInteger(m.seed) ? m.seed : null,
            width: Number(s.width) || 768,
            height: Number(s.height) || 768,
            steps: Number(s.steps) || 4,
            cfg: s.cfg === undefined ? 1 : Number(s.cfg),
            sampler: s.sampler || "euler",
            scheduler: s.scheduler || "simple",
            mode: s.mode === "img2img" || s.mode === "inpaint" ? s.mode : "txt2img",
            strength: s.strength === undefined ? 0.6 : Number(s.strength),
            stylePreset: (m.style_preset && m.style_preset.id) || "",
            // Uploaded sources and masks are not kept, so only library sources can be reloaded.
            sourceImageId: source && source.kind === "library" ? source.id : null,
            needsNewSource: Boolean(source && source.kind === "upload"),
            needsNewMask: s.mode === "inpaint",
        };
    }

    function sizeLabel(preset) {
        const tier = preset.tier === "max" ? " · max" : "";
        return `${preset.ratio} · ${preset.width} × ${preset.height}${tier}`;
    }

    // The size list: aspect presets from the router, plus the current size if it is not one of them.
    function sizeOptions(aspects, current) {
        const options = (aspects || []).map((p) => ({ value: `${p.width}x${p.height}`, label: sizeLabel(p) }));
        if (current && !options.some((o) => o.value === current)) {
            const [w, h] = current.split("x");
            options.unshift({ value: current, label: `Custom · ${w} × ${h}` });
        }
        return options;
    }

    function libraryQuery(filters) {
        const f = filters || {};
        const params = new URLSearchParams();
        if (f.q && f.q.trim()) params.set("q", f.q.trim());
        if (f.since) params.set("since", f.since);
        if (f.until) params.set("until", f.until);
        if (f.model) params.set("model", f.model);
        if (f.seed !== undefined && f.seed !== null && String(f.seed).trim() !== "") params.set("seed", String(f.seed).trim());
        if (f.favorite) params.set("favorite", "true");
        if (f.collection) params.set("collection", f.collection);
        params.set("limit", String(f.limit || 120));
        const text = params.toString();
        return text ? `?${text}` : "";
    }

    function jobStatusText(job) {
        if (!job) return "";
        const p = job.progress || {};
        if (job.status === "queued") return `Queued · #${job.position} in line`;
        if (job.status === "running") return p.max ? `Running · step ${p.value}/${p.max}` : "Running";
        if (job.status === "done") return "Done";
        if (job.status === "cancelled") return "Cancelled";
        return `Failed${job.error ? ` · ${job.error}` : ""}`;
    }

    function jobFraction(job) {
        if (!job) return 0;
        if (job.status === "done") return 1;
        const p = job.progress || {};
        return p.max ? Math.min(1, p.value / p.max) : 0;
    }

    function parseCollections(text) {
        const out = [];
        for (const raw of String(text || "").split(",")) {
            const name = raw.trim().replace(/\s+/g, " ");
            if (name && !out.includes(name)) out.push(name);
        }
        return out;
    }

    function appendNegative(current, text) {
        const have = String(current || "").split(",").map((s) => s.trim()).filter(Boolean);
        for (const part of String(text || "").split(",").map((s) => s.trim()).filter(Boolean)) {
            if (!have.includes(part)) have.push(part);
        }
        return have.join(", ");
    }

    // Masks: the editor paints with any opaque color; ComfyUI wants white = repaint on black.
    function maskFromAlpha(rgba) {
        const out = new Uint8ClampedArray(rgba.length);
        for (let i = 0; i < rgba.length; i += 4) {
            const v = rgba[i + 3] > 0 ? 255 : 0;
            out[i] = v; out[i + 1] = v; out[i + 2] = v; out[i + 3] = 255;
        }
        return out;
    }

    function hasMaskPixels(rgba) {
        for (let i = 3; i < rgba.length; i += 4) if (rgba[i] > 0) return true;
        return false;
    }

    const api = { remixFields, sizeOptions, sizeLabel, libraryQuery, jobStatusText, jobFraction,
        parseCollections, appendNegative, maskFromAlpha, hasMaskPixels };
    root.DaveImageTools = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
