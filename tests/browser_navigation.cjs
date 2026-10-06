"use strict";

// Isolated navigation fixtures; never start the production backend or launcher.
const { app, BrowserWindow } = require("electron");
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { readFileSync, mkdtempSync } = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { tmpdir } = require("node:os");

const root = path.resolve(__dirname, "..");
const baseline = "179a79c0459a77976249bbf1f7a3672496726dcc";
const views = { chat: "Chat", projects: "Projects", cluster: "Cluster", settings: "Settings" };
const assets = new Set([
    "index.html", "style.css", "console.js", "prompt-contract.js", "anticipation.js",
    "app.js", "vendor/gsap/gsap.min.js", "vendor/lucide/lucide.svg", "brand.svg", "favicon.svg",
]);
const api = new Map([
    ["/tools", []], ["/projects", []], ["/conversations", []],
    ["/nodes", [{ id: "fixture", name: "Fixture node", url: "http://fixture.invalid" }]],
    ["/nodes/status", [{ node_id: "fixture", status: "online", latency: 1 }]],
    ["/nodes/fixture/models", { models: ["fixture-model"] }],
    ["/monitoring/health", { model_health: {}, recent_errors: [] }],
    ["/analytics/costs", { total_cost: 0, by_model: {} }],
]);
const blocked = [];
const unexpected = [];
let revision = baseline;
let server;
let window;
let origin;
let failed = false;
app.setPath("userData", mkdtempSync(path.join(tmpdir(), "davellm_navigation_")));
app.on("window-all-closed", () => {});
const deadline = setTimeout(() => { console.error("Navigation fixture deadline exceeded"); app.exit(1); }, 90000);

function staticFile(name) {
    let content = revision
        ? execFileSync("git", ["show", `${revision}:static/${name}`], { cwd: root })
        : readFileSync(path.join(root, "static", name));
    if (name === "index.html") {
        content = Buffer.from(content.toString().replace("<head>", '<head><script>sessionStorage.setItem("dave_api_key_session", "navigation-fixture-key");</script>'));
    }
    return content;
}

async function waitFor(check, label) {
    const until = Date.now() + 15000;
    while (Date.now() < until) {
        if (await check()) return;
        await new Promise((resolve) => setTimeout(resolve, 25));
    }
    throw new Error(`Timed out: ${label}`);
}

const evaluate = (source) => window.webContents.executeJavaScript(source);
async function key(keyCode) {
    window.focus();
    window.webContents.sendInputEvent({ type: "keyDown", keyCode });
    window.webContents.sendInputEvent({ type: "keyUp", keyCode });
    await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
}

async function openFixture(sourceRevision) {
    revision = sourceRevision;
    const errors = [];
    let ready = false;
    window = new BrowserWindow({ width: 1440, height: 900, useContentSize: true,
        webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, partition: `navigation-${sourceRevision || "candidate"}` } });
    window.webContents.session.webRequest.onBeforeRequest((details, callback) => {
        const allowed = details.url.startsWith(`${origin}/`);
        if (!allowed) blocked.push(details.url);
        callback({ cancel: !allowed });
    });
    window.webContents.on("console-message", (details) => {
        if (details.message === "✅ DaveLLM UI ready") ready = true;
        if (details.level === "error") errors.push(details.message);
    });
    window.webContents.on("render-process-gone", (_event, details) => errors.push(`Renderer exited: ${details.reason}`));
    await window.loadURL(origin);
    await waitFor(() => ready, "fixture startup");
    assert.deepEqual(errors, [], "fixture must initialize without renderer errors");
    window.webContents.debugger.attach("1.3");
    return errors;
}

async function mainLandmarks() {
    const { nodes } = await window.webContents.debugger.sendCommand("Accessibility.getFullAXTree");
    return nodes.filter((node) => !node.ignored && node.role?.value === "main").map((node) => node.name?.value || "");
}

async function activateView(view) {
    await evaluate(`document.querySelector('button[data-view="${view}"]').focus()`);
    await key("Enter");
    await waitFor(() => evaluate(`document.body.dataset.view === "${view}"`), `${view} activation`);
}

async function reproduceBaseline() {
    await openFixture(baseline);
    assert.deepEqual(await mainLandmarks(), ["Chat"]);
    assert.equal(await evaluate('document.querySelector("main").parentElement.closest("[role=region]").id'), "chatView");
    await activateView("projects");
    assert.deepEqual(await mainLandmarks(), [], "baseline Projects has no main landmark");
    assert.equal(await evaluate('document.activeElement.dataset.view'), "projects", "baseline leaves focus on rail");
    assert.equal(await evaluate('document.querySelector(".skip-link").getAttribute("href")'), "#chatPanel");
    assert.equal(await evaluate('document.getElementById("chatPanel").getClientRects().length'), 0, "baseline skip target is hidden");
    window.destroy();
    console.log(JSON.stringify({ baseline, reproduced: ["nested Chat main", "missing Projects main", "hidden skip target", "rail focus retained"] }));
}

async function verifyCandidate() {
    const errors = await openFixture(null);
    assert.equal(await evaluate("document.activeElement.tagName"), "BODY", "startup must not steal focus");
    let cases = 0;
    for (const [width, height] of [[1440, 900], [375, 812], [3840, 1080]]) {
        window.setContentSize(width, height);
        await waitFor(() => evaluate(`innerWidth === ${width} && innerHeight === ${height}`), "viewport resize");
        for (const theme of ["light", "dark"]) {
            await evaluate(`if (document.documentElement.dataset.theme !== "${theme}") document.getElementById("themeToggle").click()`);
            for (const view of ["projects", "cluster", "settings", "chat"]) {
                await activateView(view);
                const label = `${width}x${height} ${theme} ${view}`;
                const result = await evaluate(`(() => {
                    const main = document.getElementById("${view}View");
                    return {
                        active: document.activeElement.id,
                        mains: [...document.querySelectorAll("main")].filter(el => !el.hidden).map(el => el.id),
                        tag: main.tagName, parent: main.parentElement.tagName,
                        heading: document.getElementById(main.getAttribute("aria-labelledby")).textContent.trim(),
                        current: [...document.querySelectorAll('button[data-view][aria-current="page"]')].map(el => el.dataset.view),
                        skip: document.querySelector(".skip-link").getAttribute("href"),
                        theme: document.documentElement.dataset.theme,
                        overflow: [document.documentElement.scrollWidth > document.documentElement.clientWidth,
                            document.documentElement.scrollHeight > document.documentElement.clientHeight]
                    };
                })()`);
                assert.deepEqual(result, { active: `${view}View`, mains: [`${view}View`], tag: "MAIN", parent: "BODY",
                    heading: views[view], current: [view], skip: `#${view}View`, theme, overflow: [false, false] }, label);
                assert.deepEqual(await mainLandmarks(), [views[view]], `${label} accessibility tree`);
                const before = await evaluate("location.href");
                await evaluate('document.querySelector(".skip-link").focus()');
                assert.equal(await evaluate('getComputedStyle(document.querySelector(".skip-link")).top'), "8px", "keyboard skip link is visible");
                await key("Enter");
                assert.equal(await evaluate("document.activeElement.id"), `${view}View`, `${label} skip focus`);
                assert.equal(await evaluate("location.href"), before, "skip activation must not add fragment history");
                await key("Tab");
                assert.equal(await evaluate(`(() => {
                    const el = document.activeElement;
                    return document.getElementById("${view}View").contains(el) && el.id !== "${view}View"
                        && !el.disabled && el.getClientRects().length > 0;
                })()`), true, `${label} Tab enters active content`);
                cases += 1;
            }
        }
    }
    await evaluate('document.getElementById("promptInput").focus(); setConsoleView("chat")');
    assert.equal(await evaluate("document.activeElement.id"), "promptInput", "same-view update preserves draft focus");
    await evaluate('setConsoleView("unknown")');
    assert.equal(await evaluate("document.activeElement.id"), "promptInput", "invalid same-view fallback preserves focus");
    assert.deepEqual(errors, [], "no renderer errors during navigation");
    assert.deepEqual(unexpected, [], "no unknown or mutating fixture requests");
    assert.deepEqual(blocked, [], "no external network attempts");
    window.destroy();
    console.log(JSON.stringify({ candidate: "working tree", navigationCases: cases, keyboard: "Enter, skip Enter, Tab",
        landmarks: "one named top-level main per active view via Chromium accessibility tree", externalRequests: 0, backendWrites: 0, inference: "not run" }));
}

app.whenReady().then(async () => {
    server = http.createServer((request, response) => {
        const pathname = new URL(request.url, "http://fixture.invalid").pathname;
        if (request.method === "GET" && api.has(pathname)) {
            response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
            response.end(JSON.stringify(api.get(pathname)));
            return;
        }
        const name = pathname === "/" ? "index.html" : pathname.slice(1);
        if (request.method === "GET" && assets.has(name)) {
            const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
            response.writeHead(200, { "Content-Type": types[path.extname(name)], "Cache-Control": "no-store" });
            response.end(staticFile(name));
            return;
        }
        unexpected.push(`${request.method} ${pathname}`);
        response.writeHead(404);
        response.end("Unexpected fixture request");
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    origin = `http://127.0.0.1:${server.address().port}`;
    await reproduceBaseline();
    await verifyCandidate();
}).catch(async (error) => {
    failed = true;
    console.error(error.stack);
    if (window && !window.isDestroyed()) {
        console.error(JSON.stringify(await evaluate('({ view: document.body.dataset.view, focus: document.activeElement.id, title: document.title })').catch(() => ({}))));
    }
    console.error(JSON.stringify({ unexpected, blocked }));
}).finally(async () => {
    clearTimeout(deadline);
    if (window && !window.isDestroyed()) window.destroy();
    if (server) await new Promise((resolve) => server.close(resolve));
    app.exit(failed ? 1 : 0);
});
