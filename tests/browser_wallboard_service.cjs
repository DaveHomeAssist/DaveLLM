// Exercise the staged Python service with synthetic data only.
const {chromium} = require('playwright');
const {spawn, spawnSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

(async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'wallboard-service-'));
    const bundle = path.join(temp, 'bundle'), status = path.join(temp, 'status.json');
    let server, browser;
    try {
        const staged = spawnSync('python3', [path.join(root, 'scripts/stage_wallboard.py'), bundle], {encoding: 'utf8'});
        assert.equal(staged.status, 0, staged.stderr);
        const record = {checked_at: new Date().toISOString(), overall: 'healthy', recovery: null,
            targets: ['Local','Dominic','Walter','Duncan'].map(name => ({name, endpoint: 'http://fixture.invalid/health', status: 'healthy', latency_ms: 12, http_status: 200}))};
        fs.writeFileSync(status, JSON.stringify({version: 1, published_at: record.checked_at, current: record, history: [record]}));
        server = spawn('python3', ['-u', '-c', 'from pathlib import Path; import sys; from serve_wallboard import make_server; s=make_server(Path(sys.argv[1]),Path(sys.argv[2]),0); print(s.server_port,flush=True); s.serve_forever()', bundle, status], {cwd: bundle, stdio: ['ignore','pipe','pipe']});
        const port = await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('Service readiness timeout')), 10000);
            const lines = readline.createInterface({input: server.stdout});
            lines.once('line', value => {clearTimeout(timer); lines.close(); resolve(Number(value));});
            server.once('error', error => {clearTimeout(timer); reject(error);});
            server.once('exit', code => {clearTimeout(timer); reject(new Error(`Service exited ${code}`));});
        });
        assert.ok(Number.isInteger(port) && port > 0);
        browser = await chromium.launch({headless: true, executablePath: process.env.CHROME_BIN || undefined, args: ['--no-sandbox']});
        const page = await browser.newPage({viewport: {width: 1440, height: 900}});
        const errors = [], external = [];
        page.on('pageerror', error => errors.push(error.message));
        page.on('console', message => {if (message.type() === 'error' && /content security|refused to/i.test(message.text())) errors.push(message.text());});
        await page.route('**/*', route => {
            if (new URL(route.request().url()).origin !== `http://127.0.0.1:${port}`) {external.push(route.request().url()); return route.abort();}
            return route.continue();
        });
        await page.goto(`http://127.0.0.1:${port}/`);
        await page.waitForFunction(() => document.querySelector('#onCallState').textContent === 'healthy');
        assert.equal(await page.locator('#onCallTargets .oncall-target').count(), 4);
        await page.click('#theme');
        assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
        fs.writeFileSync(status, '{"invalid":true}');
        await page.click('#refresh');
        await page.waitForFunction(() => document.querySelector('#source').textContent.includes('failed'));
        assert.equal(await page.locator('#onCallState').textContent(), 'healthy');
        assert.deepEqual(errors, []);
        assert.deepEqual(external, []);
        console.log('Staged Python service: render, CSP, theme, invalid source warning and no external requests passed');
    } finally {
        if (browser) await browser.close();
        if (server && server.exitCode === null) {
            await new Promise(resolve => {server.once('exit', resolve); server.kill('SIGTERM');});
        }
        fs.rmSync(temp, {recursive: true, force: true});
    }
})().catch(error => {console.error(error); process.exitCode = 1;});
