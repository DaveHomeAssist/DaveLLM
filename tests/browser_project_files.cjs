"use strict";

// Run against a disposable backend with /tmp/project-output inside DAVE_TOOL_ROOTS.
// No model inference or installed desktop state is used.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const base = process.env.PROJECT_FILES_URL || "http://127.0.0.1:18879";
const auth = { "X-API-Key": "project-files-fixture" };
(async () => {
    const browser = await chromium.launch({ headless: true });
    try {
        const context = await browser.newContext();
        const created = await context.request.post(`${base}/projects`, {
            headers: auth, data: { name: "File destination acceptance" }
        });
        assert.equal(created.status(), 200);
        const project = await created.json();
        await context.addInitScript(() => sessionStorage.setItem("dave_api_key_session", "project-files-fixture"));
        const page = await context.newPage();
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.route(`${base}/nodes**`, (route) => route.fulfill({ json: [] }));
        await page.goto(base);
        await page.locator("#projectSelect").selectOption(project.project_id);
        await page.locator("#editProject").click();
        await page.locator("#projectHomeDialog[open]").waitFor();
        await page.locator("#projectOutputDirectory").fill("/tmp/project-output");
        await page.locator("#saveProjectInstructions").click();
        await page.locator("#projectHomeStatus").filter({ hasText: "Project settings saved" }).waitFor();
        assert.equal((await (await context.request.get(`${base}/projects/${project.project_id}`, { headers: auth })).json()).output_directory, "/tmp/project-output");
        await page.locator("#projectHomeClose").click();
        await page.locator("#editProject").click();
        await page.locator("#projectOutputDirectory").waitFor();
        assert.equal(await page.locator("#projectOutputDirectory").inputValue(), "/tmp/project-output");
        await page.locator("#projectOutputDirectory").fill("/outside");
        await page.locator("#saveProjectInstructions").click();
        await page.locator("#projectHomeStatus").filter({ hasText: "Save failed" }).waitFor();
        assert.equal((await (await context.request.get(`${base}/projects/${project.project_id}`, { headers: auth })).json()).output_directory, "/tmp/project-output");
        for (const theme of ["light", "dark"]) {
            await page.evaluate((value) => document.documentElement.dataset.theme = value, theme);
            for (const [width, height] of [[1440, 900], [375, 812], [844, 390], [320, 256], [3840, 1080]]) {
                await page.setViewportSize({ width, height });
                await page.locator("#projectOutputDirectory").scrollIntoViewIfNeeded();
                const sizes = await page.evaluate(() => ({
                    width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight,
                    clientWidth: document.documentElement.clientWidth, clientHeight: document.documentElement.clientHeight
                }));
                assert.ok(sizes.width <= sizes.clientWidth && sizes.height <= sizes.clientHeight,
                    `${theme} ${width}x${height}: root overflow ${JSON.stringify(sizes)}`);
                const box = await page.locator("#projectOutputDirectory").boundingBox();
                assert.ok(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= width && box.y + box.height <= height,
                    `${theme} ${width}x${height}: output field is clipped`);
                await page.locator("#projectOutputDirectory").focus();
                assert.ok(await page.locator("#projectOutputDirectory").evaluate((el) => document.activeElement === el));
                await page.screenshot({ path: `/work/project-files-${theme}-${width}.png` });
            }
        }
        await page.locator("#projectHomeClose").click();
        await page.evaluate(() => {
            activeToolRunId = "run_folder_preview";
            renderToolRun({
                run_id: activeToolRunId, status: "approval_required", reason_code: "approval_required",
                context: { output_directory: "/tmp/project-output" },
                snapshot: { steps: 1, transcript: [], pending_call: {
                    call_id: "write_preview", tool_name: "file.write", permission: "write_files",
                    arguments: { path: "monitor.py", content: "print('ok')" },
                    expires_at: new Date(Date.now() + 300000).toISOString()
                } }
            });
        });
        assert.ok((await page.locator("#runLedgerApproval").innerText()).includes("Output folder: /tmp/project-output"));
        assert.deepEqual(errors, []);
        console.log("PASS: project folder save/readback, reopen, invalid-save preservation, keyboard focus, 10 theme/viewport checks");
    } finally {
        await browser.close();
    }
})().catch((error) => { console.error(error); process.exitCode = 1; });
