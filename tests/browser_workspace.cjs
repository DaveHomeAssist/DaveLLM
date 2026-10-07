"use strict";

// Synthetic API fixture. Never starts the installed backend or contacts a node.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const root = path.resolve(__dirname, "../static");
const tools = Object.fromEntries(Array.from({ length: 35 }, (_, i) => [i === 0 ? "calc.eval" : `fixture.tool${i}`, {
    description: "Read bounded information without changing anything. This deliberately long description must remain one line.", permission: "read",
}]));
const assets = new Set(["/", "/index.html", "/style.css", "/console.js", "/app.js", "/anticipation.js", "/prompt-contract.js", "/brand.svg", "/favicon.svg", "/vendor/lucide/lucide.svg", "/vendor/gsap/gsap.min.js"]);
const api = {
    "/tools": { tools }, "/projects": { projects: [] }, "/conversations": [],
    "/nodes": [{ id: "fixture", name: "Fixture", url: "http://fixture.invalid" }],
    "/nodes/status": [{ node_id: "fixture", status: "online", latency: 1 }],
    "/nodes/fixture/models": { models: [{ id: "fixture-model" }] },
    "/monitoring/health": { model_health: {}, recent_errors: [] },
    "/analytics/costs": { total_cost: 0, by_model: {} },
};
const rejected = [];
const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://fixture.invalid").pathname;
    if (request.method !== "GET") { rejected.push(`${request.method} ${pathname}`); response.writeHead(405).end(); return; }
    if (Object.hasOwn(api, pathname)) { response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(api[pathname])); return; }
    if (!assets.has(pathname)) { rejected.push(pathname); response.writeHead(404).end(); return; }
    const file = path.join(root, pathname === "/" ? "index.html" : pathname);
    response.setHeader("Content-Type", ({ ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" })[path.extname(file)] || "text/html");
    response.end(fs.readFileSync(file));
});

(async () => {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    const browser = await chromium.launch({ headless: true });
    try {
        const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
        context.setDefaultTimeout(8000);
        await context.addInitScript(() => sessionStorage.setItem("dave_api_key_session", "fixture-only"));
        const page = await context.newPage();
        const errors = [];
        let submitted = null;
        page.on("pageerror", (error) => errors.push(error.message));
        await page.route("**/*", (route) => {
            if (new URL(route.request().url()).origin !== base) { rejected.push("external request"); return route.abort(); }
            const pathname = new URL(route.request().url()).pathname;
            if (pathname === "/tools/agent/runs" && route.request().method() === "POST") {
                submitted = route.request().postDataJSON();
                return route.fulfill({ json: { run_id: "run_fixture", status: "completed", reason_code: "completed", snapshot: { transcript: [], pending_call: null } } });
            }
            if (pathname === "/tools/agent/runs/run_fixture") return route.fulfill({ json: { run_id: "run_fixture", status: "completed", reason_code: "completed", snapshot: { transcript: [], pending_call: null } } });
            if (pathname === "/tools/agent/runs/run_fixture/events") return route.fulfill({ contentType: "text/event-stream", body: 'data: {"run_id":"run_fixture","sequence":1,"kind":"terminal","status":"completed"}\n\n' });
            return route.continue();
        });
        await page.goto(base);
        await page.locator("#toolPickerToggle").waitFor({ state: "visible" });
        await page.locator("#toolPickerToggle").click();
        assert.equal(await page.locator(".tool-picker-option").count(), 35);
        await page.locator("#toolSearch").fill("calc.eval");
        assert.equal(await page.locator(".tool-picker-option").count(), 1);
        await page.getByRole("option", { name: /calc.eval/ }).click();
        assert.equal(await page.locator("#selectedTools").innerText(), "calc.eval ×");
        await page.locator("#toolSearch").fill("");
        await page.locator(".tool-picker-option").first().focus();
        await page.keyboard.press("ArrowDown");
        assert.equal(await page.evaluate(() => document.activeElement.dataset.toolName), "fixture.tool1");
        await page.keyboard.press("Enter");
        assert.equal(await page.evaluate(() => document.activeElement.dataset.toolName), "fixture.tool1");
        await page.keyboard.press("Enter");
        const row = await page.locator(".tool-picker-option").first().evaluate((element) => {
            const label = element.querySelector("strong").getBoundingClientRect();
            const detail = element.querySelector("small").getBoundingClientRect();
            return { aligned: Math.abs(label.y - detail.y) < 4, height: element.getBoundingClientRect().height,
                nowrap: getComputedStyle(element.querySelector("small")).whiteSpace, title: element.title };
        });
        assert.ok(row.aligned && row.height <= 44 && row.nowrap === "nowrap" && row.title.includes("deliberately long"));
        await page.locator("#toolPickerToggle").click();
        await page.locator("#inspectorToggle").click();
        const history = page.locator("#historyResize");
        const inspector = page.locator("#inspectorResize");
        const width = (name) => page.locator(name).evaluate((element) => element.getBoundingClientRect().width);
        const start = await width("#historyPanel");
        await history.focus();
        await page.keyboard.press("ArrowRight");
        assert.equal(await width("#historyPanel"), start + 10);
        await page.reload();
        await page.locator("#toolPickerToggle").waitFor();
        assert.equal(await width("#historyPanel"), start + 10);
        await page.locator("#inspectorToggle").click();
        await history.focus(); await page.keyboard.press("Enter");
        assert.equal(await width("#historyPanel"), start);
        const oldInspector = await width("#inspectorPanel");
        const handle = await inspector.boundingBox();
        await page.mouse.move(handle.x + handle.width / 2, handle.y + 60);
        await page.mouse.down(); await page.mouse.move(handle.x - 80, handle.y + 60); await page.mouse.up();
        assert.ok(await width("#inspectorPanel") > oldInspector + 60);
        assert.ok(await width("#chatPanel") >= 480);
        await inspector.focus(); await page.keyboard.press("End");
        assert.ok(await width("#chatPanel") >= 480);
        await page.keyboard.press("Enter");
        await page.locator("#historyToggle").click();
        assert.equal(await history.isVisible(), false);
        assert.equal(await inspector.isVisible(), true);
        await page.locator("#historyToggle").click();
        for (const theme of ["light", "dark"]) {
            if (await page.locator("html").getAttribute("data-theme") !== theme) await page.locator("#themeToggle").click();
            for (const [w, h] of [[1440, 900], [375, 812], [844, 390], [320, 256], [3840, 1080]]) {
                await page.setViewportSize({ width: w, height: h });
                if (await page.locator("#inspectorClose").isVisible()) await page.locator("#inspectorClose").click();
                await page.locator("#toolPickerToggle").click();
                const geometry = await page.evaluate(() => ({
                    root: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
                    viewport: [innerWidth, innerHeight],
                    send: document.getElementById("sendBtn").getBoundingClientRect().bottom,
                    pickerBackground: getComputedStyle(document.getElementById("toolPicker")).backgroundColor,
                    outside: Array.from(document.querySelectorAll("body *")).filter((el) => {
                        const r = el.getBoundingClientRect();
                        return r.width && r.height && r.bottom > innerHeight && !el.closest(".tool-picker-list");
                    }).slice(0,8).map((el) => ({id:el.id,cls:el.className,bottom:el.getBoundingClientRect().bottom})),
                }));
                assert.ok(geometry.root[0] <= w && geometry.root[1] <= h, `${theme} ${w}x${h}: ${JSON.stringify(geometry)}`);
                assert.ok(geometry.send <= h, `${theme} ${w}x${h}: Send clipped`);
                assert.notEqual(geometry.pickerBackground, "rgba(0, 0, 0, 0)", "Tool menu must be opaque");
                const lastTool = page.locator(".tool-picker-option").last();
                await lastTool.focus();
                const lastBounds = await lastTool.boundingBox();
                assert.ok(lastBounds.y >= 0 && lastBounds.y + lastBounds.height <= h, "Last tool reachable inside bounded list");
                if (process.env.CONSOLE_SCREENSHOTS) {
                    fs.mkdirSync(process.env.CONSOLE_SCREENSHOTS, { recursive: true });
                    await page.screenshot({ path: path.join(process.env.CONSOLE_SCREENSHOTS, `${theme}-${w}.png`) });
                }
                await page.locator("#closeToolsBtn").click();
                if (w === 3840) {
                    await page.locator("#inspectorToggle").click();
                    assert.equal(await width("#historyPanel"), 360);
                    assert.equal(await width("#inspectorPanel"), 560);
                    assert.ok(await width(".footer-inputs") >= 1100);
                    assert.ok(await width("#chatPanel") >= 480);
                    if (process.env.CONSOLE_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.CONSOLE_SCREENSHOTS, `${theme}-ultrawide-panels.png`) });
                    await page.locator("#inspectorClose").click();
                }
                if (h <= 500) {
                    await page.locator("#contextDisclosure summary").click();
                    assert.equal(await page.locator("#contextTemplateBtn").isVisible(), true);
                    const contextBounds = await page.locator("#contextStrip").boundingBox();
                    assert.ok(contextBounds.y >= 0 && contextBounds.y + contextBounds.height <= h);
                    await page.locator("#closeCompactContext").click();
                }
            }
        }
        await page.locator("#promptInput").fill("/calc");
        await page.getByRole("option", { name: /calc.eval/ }).click();
        assert.equal(await page.locator("#promptInput").inputValue(), "");
        await page.locator("#promptInput").fill("Fixture tool selection only.");
        await page.locator("#sendBtn").click();
        await page.locator("#runLedgerStatus").filter({ hasText: "completed" }).waitFor();
        assert.deepEqual(submitted.selected_tools, ["calc.eval"]);
        assert.equal(submitted.messages.at(-1).content, "Fixture tool selection only.");
        assert.deepEqual(errors, []);
        assert.deepEqual(rejected, []);
        console.log("Workspace fixture passed: tool rows, keyboard/pointer resize, reset, persistence, clamping, two themes/five viewports.");
    } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => { server.closeAllConnections(); server.close(); });
