// Focused acceptance for the composer tool picker. Uses a disposable local router.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');

const base = process.env.CONSOLE_URL || 'http://127.0.0.1:8769';
let submitted = null;

(async () => {
    const browser = await chromium.launch({
        executablePath: process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        headless: process.env.CONSOLE_VISIBLE !== 'true',
        slowMo: process.env.CONSOLE_VISIBLE === 'true' ? 220 : 0,
    });
    try {
        const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
        await context.addInitScript(() => sessionStorage.setItem('dave_api_key_session', 'console-fixture-key'));
        const page = await context.newPage();
        await page.route(`${base}/**`, async (route) => {
            const request = route.request();
            const path = new URL(request.url()).pathname;
            const json = (value) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) });
            if (path === '/nodes') return json([{ id: 'fixture', name: 'Fixture', url: 'http://fixture.invalid:11434' }]);
            if (path === '/nodes/status') return json([{ node_id: 'fixture', status: 'online', latency: 5 }]);
            if (path === '/nodes/fixture/models') return json({ models: [{ id: 'fixture-model' }], error: null });
            if (path === '/conversations') return json([]);
            if (path === '/projects') return json([]);
            if (path === '/monitoring/health') return json({ model_health: {}, recent_errors: [] });
            if (path === '/tools') return json({ tools: {
                'calc.eval': { description: 'Evaluate bounded arithmetic', permission: 'read' },
                'json.validate': { description: 'Validate JSON text', permission: 'read' },
            } });
            if (path === '/tools/agent/runs' && request.method() === 'POST') {
                submitted = request.postDataJSON();
                return json({ run_id: 'run_picker', status: 'completed', reason_code: 'completed', snapshot: { transcript: [], pending_call: null } });
            }
            if (path === '/tools/agent/runs/run_picker') return json({ run_id: 'run_picker', status: 'completed', reason_code: 'completed', snapshot: { transcript: [], pending_call: null } });
            return route.continue();
        });
        await page.goto(base, { waitUntil: 'domcontentloaded' });
        await page.locator('#toolPickerToggle').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#runToolsBtn').count(), 0);

        await page.locator('#promptInput').fill('/calc');
        await page.getByRole('option', { name: /calc\.eval/ }).click();
        assert.equal(await page.locator('#promptInput').inputValue(), '');
        assert.equal(await page.locator('#selectedTools').innerText(), 'calc.eval ×');

        await page.locator('#promptInput').fill('Calculate 12 * 9.');
        await page.locator('#sendBtn').click();
        await page.locator('#runLedgerStatus').filter({ hasText: 'completed' }).waitFor();
        assert.deepEqual(submitted.selected_tools, ['calc.eval']);
        assert.equal(submitted.messages.at(-1).content, 'Calculate 12 * 9.');

        await page.setViewportSize({ width: 390, height: 844 });
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
        assert.equal(overflow, false);
        console.log('Tool picker browser acceptance passed.');
    } finally {
        await browser.close();
    }
})().catch((error) => { console.error(error); process.exitCode = 1; });
