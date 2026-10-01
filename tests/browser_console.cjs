// Optional browser acceptance: CONSOLE_URL=http://127.0.0.1:8769 node tests/browser_console.cjs
// Uses Playwright supplied by the developer environment; no runtime dependency.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const base = process.env.CONSOLE_URL || 'http://127.0.0.1:8769';
const output = process.env.CONSOLE_SCREENSHOTS || '/tmp/davellm-console-acceptance';
const now = Date.now();
const projectName = `Design acceptance ${now}`;
const conversations = [0.1, 48, 96, 240].map((hours, i) => ({
    conversation_id: `design_${i}`, title: ['Router retry backoff', 'Ollama context length notes', 'HA routine audit', 'Tailnet ACL cleanup'][i],
    updated_at: new Date(now - hours * 3600000).toISOString(), created_at: new Date(now - hours * 3600000).toISOString(),
    project_id: null, messages: [{ role: 'user', content: 'Show the retry configuration.' }, { role: 'assistant', content: '## Retry configuration\n\nUse a bounded delay. **Keep the limit explicit.**\n\n```json\n{"limit": 8}\n```\n\n<script>window.untrustedExecuted = true</script>' }],
    last_message: 'Use a bounded delay.', message_count: 2,
}));
const nodes = [{ id: 'fixture', name: 'Fixture GPU', url: 'http://fixture.invalid:11434' }, { id: 'offline', name: 'Offline node', url: 'http://offline.invalid:11434' }];
const model = 'gpt-oss:120b-long-model-name';
let sent = null;
let decided = null;
let runStatus = 'approval_required';
const pending = { call_id: 'call_design', tool_name: 'file.edit', permission: 'write_files', arguments: { path: 'example.txt', old_text: 'before', new_text: 'after', expected_count: 1 }, digest: 'fixture-digest', definition_fingerprint: 'fixture-definition', nonce: 'fixture-nonce', expires_at: new Date(now + 300000).toISOString() };
const run = () => ({ run_id: 'run_design', status: runStatus, reason_code: runStatus, snapshot: { steps: 2, pending_call: runStatus === 'approval_required' ? pending : null, transcript: [] } });
(async () => {
    fs.mkdirSync(output, { recursive: true });
    const browser = await chromium.launch({ executablePath: process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
    const errors = [];
    try {
        const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
        await context.addInitScript(() => sessionStorage.setItem('dave_api_key_session', 'console-fixture-key'));
        const page = await context.newPage();
        page.on('pageerror', (error) => errors.push(error.message));
        await page.route(`${base}/**`, async (route) => {
            const url = new URL(route.request().url());
            const path = url.pathname;
            const json = (data) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
            if (path === '/nodes') return json(nodes);
            if (path === '/nodes/status') return json([{ node_id: 'fixture', status: 'online', latency: 9 }, { node_id: 'offline', status: 'offline', latency: null }]);
            if (path === '/nodes/fixture/models') return json({ models: [{ id: model }], error: null });
            if (path === '/conversations') return json(conversations);
            if (/^\/conversations\/design_\d$/.test(path)) return json(conversations.find((c) => path.endsWith(c.conversation_id)));
            if (path.endsWith('/memories')) return json({ memories: [] });
            if (path === '/chat/stream') {
                sent = route.request().postDataJSON();
                return route.fulfill({ contentType: 'text/event-stream', body: 'data: {"token":"Fixture reply","done":false}\n\ndata: {"done":true}\n\n' });
            }
            if (path === '/tools/agent/runs' && route.request().method() === 'POST') return json(run());
            if (path === '/tools/agent/runs/run_design') return json(run());
            if (path.endsWith('/run_design/events')) return route.fulfill({ contentType: 'text/event-stream', body: '' });
            if (path.endsWith('/run_design/decisions')) { decided = route.request().postDataJSON(); runStatus = 'approval_rejected'; return json(run()); }
            return route.continue();
        });
        await page.goto(base, { waitUntil: 'networkidle' });
        assert.equal(await page.locator('html').getAttribute('data-theme'), 'light');
        assert.equal(await page.locator('.age-heading').count(), 4);
        assert.equal(await page.locator('.age-fresh.convo-item').count(), 1);
        await page.locator('.convo-item').first().click();
        await page.locator('.code-toolbar').waitFor();
        assert.equal(await page.evaluate(() => window.untrustedExecuted), undefined);
        assert.equal(await page.locator('.code-toolbar span').innerText(), 'json');
        assert.equal(await page.locator('.code-block code').innerText(), '{"limit": 8}');
        await page.locator('#promptInput').fill('Review the next edit.');
        await page.locator('#sendBtn').click();
        await page.locator('.message-assistant').filter({ hasText: 'Fixture reply' }).waitFor();
        assert.equal(sent.prompt, 'Review the next edit.');
        assert.equal(sent.model, model);
        assert.equal(sent.node_id, 'fixture');
        for (const width of [390, 820, 1024, 1280, 1440, 1920, 5120]) {
            await page.setViewportSize({ width, height: width === 5120 ? 1440 : 900 });
            await page.waitForTimeout(100);
            const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
            assert.equal(overflow, false, `horizontal overflow at ${width}`);
            const send = await page.locator('#sendBtn').boundingBox();
            assert.ok(send && send.y + send.height <= (width === 5120 ? 1440 : 900), `composer outside viewport at ${width}`);
            await page.screenshot({ path: `${output}/chat-${width}.png` });
        }
        await page.setViewportSize({ width: 390, height: 844 });
        assert.equal(await page.locator('#contextModelValue').innerText(), model);
        const selectedModel = await page.locator('#contextModelValue').boundingBox();
        assert.ok(selectedModel.width < 390 && selectedModel.height > 0);
        await page.locator('#contextModelBtn').click();
        await page.locator('#runtimeDialog').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#modelSelect').inputValue(), model);
        await page.getByRole('button', { name: 'Done', exact: true }).click();
        await page.locator('#themeToggle').click();
        assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
        await page.screenshot({ path: `${output}/phone-dark.png` });
        await page.locator('#inspectorToggle').click();
        await page.locator('#inspectorPanel').waitFor({ state: 'visible' });
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#inspectorPanel').isVisible(), false);
        await page.locator('[data-view="cluster"]').click();
        await page.locator('#clusterChecked').filter({ hasText: 'checked' }).waitFor();
        assert.equal(await page.locator('.node-card').count(), 2);
        assert.equal(await page.locator('.model-tag').innerText(), model);
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#clusterView').isVisible(), true);
        assert.equal(await page.locator('.node-url').first().isVisible(), false);
        await page.screenshot({ path: `${output}/cluster-phone.png` });
        await page.setViewportSize({ width: 1440, height: 900 });
        assert.equal(await page.locator("#clusterView").isVisible(), true);
        await page.screenshot({ path: `${output}/cluster-desktop.png` });
        await page.locator('[data-view="projects"]').click();
        await page.locator('#newProjectButton').click();
        await page.locator('#newProjectDialog input[name="name"]').fill(projectName);
        await page.locator('#newProjectDialog button[value="create"]').click();
        const projectCard = page.locator('#projectCards .card').filter({ hasText: projectName });
        await projectCard.waitFor();
        await projectCard.getByRole('button', { name: 'Open project' }).click();
        await page.locator('#projectHomeDialog').waitFor({ state: 'visible' });
        await page.locator('#projectHomeStatus').filter({ hasText: 'All four' }).waitFor();
        await page.locator('#projectHomeClose').click();
        await page.locator('[data-view="chat"]').click();
        await page.locator('#promptInput').fill('Review the next edit.');
        await page.locator('#runToolsBtn').click();
        await page.getByRole('heading', { name: 'Approval required' }).waitFor();
        assert.equal(await page.locator('#sendBtn').isDisabled(), true);
        assert.match(await page.locator('#approvalCountdown').innerText(), /Expires in/);
        await page.screenshot({ path: `${output}/approval-desktop.png` });
        await page.setViewportSize({ width: 390, height: 844 });
        await page.screenshot({ path: `${output}/approval-phone.png` });
        await page.getByRole('button', { name: 'Reject', exact: true }).click();
        assert.equal(decided.decision, 'reject');
        assert.equal(decided.digest, pending.digest);
        assert.equal(decided.nonce, pending.nonce);
        await page.waitForFunction(() => !document.getElementById('sendBtn').disabled);
        assert.equal(await page.locator('#promptInput').inputValue(), 'Review the next edit.');
        assert.equal(errors.length, 0, errors.join('\n'));
        assert.equal(await page.evaluate(() => localStorage.getItem('dave_api_key')), null);
        // Credential entry also renders without a key, and cancellation never persists it.
        const fresh = await browser.newContext();
        const auth = await fresh.newPage();
        await auth.goto(base, { waitUntil: 'domcontentloaded' });
        await auth.locator('#credentialDialog').waitFor({ state: 'visible' });
        assert.equal(await auth.locator('#credentialInput').getAttribute('type'), 'password');
        await auth.locator('#credentialDialog button[value="cancel"]').click();
        assert.equal(await auth.evaluate(() => sessionStorage.getItem('dave_api_key_session')), null);
        console.log('PASS: seven widths, themes, age groups, safe Markdown, runtime picker, inspector, cluster, project creation, exact approval rejection, draft preservation, streaming, credential cancellation.');
    } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exit(1); });
