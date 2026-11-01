// Disposable HTTP fixture only. No live backend, credentials or hardware.
const {chromium} = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
let mode = 'healthy', minutes = 0;
const record = () => ({checked_at: new Date(Date.now() - minutes * 60000).toISOString(), overall: mode === 'outage' ? 'outage' : 'healthy', summary: '<script>globalThis.injected=true</script>', recovery: null,
    targets: ['Local','Dominic','Walter','Duncan'].map(name => ({name,endpoint:'http://fixture.invalid/health',status:mode === 'outage' ? 'down' : 'healthy',latency_ms:mode === 'outage' ? null : 12.5,http_status:mode === 'outage' ? 503 : 200}))});
const server = http.createServer((req,res) => {
    const url = new URL(req.url,'http://localhost');
    if (url.pathname === '/wallboard-status.json') {
        if (mode === 'failure') {res.writeHead(503).end();return;}
        res.setHeader('Content-Type','application/json');
        res.end(mode === 'invalid' ? '{"version":2}' : JSON.stringify({version:1,current:record(),history:[record()]}));return;
    }
    const files = {'/wallboard.html':'text/html','/wallboard.js':'text/javascript','/on-call.js':'text/javascript','/wallboard.css':'text/css'};
    if (!files[url.pathname]) {res.writeHead(404).end();return;}
    res.setHeader('Content-Type',files[url.pathname]);res.end(fs.readFileSync(path.join(__dirname,'../static',url.pathname)));
});
(async () => {
    await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
    const browser = await chromium.launch({headless:true,executablePath:process.env.CHROME_BIN || undefined,args:['--no-sandbox']});
    try {
        const page = await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
        await page.goto(`http://127.0.0.1:${server.address().port}/wallboard.html`);
        await page.waitForFunction(()=>document.querySelector('#onCallState').textContent==='healthy');
        assert.equal(await page.evaluate(()=>globalThis.injected),undefined);
        for (const [width,height] of [[3840,2160],[1920,1080],[1440,900],[2560,720],[375,812],[320,256]]) {
            await page.setViewportSize({width,height});
            assert.equal(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight && document.documentElement.scrollWidth<=innerWidth),true,`${width}x${height} root overflow`);
            const fits=await page.locator('#onCallTargets').evaluate(e=>e.scrollHeight<=e.clientHeight+1);
            assert.ok(fits,`${width}x${height} target overflow`);
        }
        if (process.env.WALLBOARD_SCREENSHOT) await page.screenshot({path:process.env.WALLBOARD_SCREENSHOT});
        await page.setViewportSize({width:1440,height:900});
        await page.click('#theme');assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
        await page.reload();assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
        mode='outage';await page.click('#refresh');await page.waitForFunction(()=>document.querySelector('#onCallState').textContent==='outage');
        mode='invalid';await page.click('#refresh');await page.waitForFunction(()=>document.querySelector('#source').textContent.includes('failed'));
        assert.equal(await page.locator('#onCallState').textContent(),'outage');
        mode='healthy';minutes=76;await page.click('#refresh');await page.waitForFunction(()=>document.querySelector('#onCallState').textContent==='Delayed');
        minutes=121;await page.click('#refresh');await page.waitForFunction(()=>document.querySelector('#onCallState').textContent==='Stale');
        mode='failure';await page.reload();await page.waitForFunction(()=>document.querySelector('#onCallState').textContent==='Waiting');
        assert.deepEqual(errors,[]);console.log('Wallboard: six viewports, themes, malformed/missing data, outage retention and freshness passed');
    } finally {await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
