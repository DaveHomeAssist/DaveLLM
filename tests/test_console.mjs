import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({});
vm.runInContext(readFileSync(new URL('../static/console.js', import.meta.url), 'utf8'), context);
const now = Date.parse('2026-10-01T12:00:00Z');
const age = (hours) => context.conversationAge({ updated_at: new Date(now - hours * 3600000).toISOString() }, now);

test('panel widths preserve a usable chat column and reject invalid saved values', () => {
    const result = context.consolePanelLayout(1300, { history: 560, inspector: 960 }, { history: true, inspector: true });
    assert.ok(result.widths.history + result.widths.inspector + 24 <= 820);
    assert.ok(result.widths.history >= 220);
    assert.ok(result.widths.inspector >= 280);
    const invalid = context.consolePanelLayout(3700, { history: '900', inspector: Infinity }, { history: true, inspector: true }, true);
    assert.equal(invalid.widths.history, 360);
    assert.equal(invalid.widths.inspector, 560);
    const narrow = context.consolePanelLayout(1100, { inspector: 960 }, { history: false, inspector: true });
    assert.equal(narrow.widths.inspector, 608);
    assert.equal(narrow.maximum.inspector, 608);
});

test('age bands cover every boundary without losing the requested categories', () => {
    for (const [hours, band] of [[0, 'fresh'], [23.99, 'fresh'], [24, 'recent'], [71.99, 'recent'], [72, 'week'], [168, 'week'], [168.01, 'old']]) {
        assert.equal(age(hours).band, band, `${hours} hours`);
    }
    assert.equal(age(-1).text, '0m');
    assert.equal(context.conversationAge({ updated_at: 'invalid' }, now).band, 'unknown');
    assert.equal(context.conversationAge({ created_at: new Date(now).toISOString() }, now).band, 'fresh');
});

test('the pre-send size line warns only over the node limit and credits a warm cache', () => {
    assert.equal(context.promptSizeText(null, 'Duncan'), null);
    assert.equal(context.promptSizeText({ prompt_tokens: 'n/a' }, 'Duncan'), null);
    const over = context.promptSizeText({ prompt_tokens: 13520, prompt_token_limit: 2500, over_limit: true }, 'Duncan');
    assert.equal(over.over, true);
    assert.equal(over.text, '≈13,520 tokens · Duncan reads ~2,500 quickly');
    assert.match(over.title, /may take several minutes/);
    const under = context.promptSizeText({ prompt_tokens: 1240, prompt_token_limit: 2500, over_limit: false }, 'Duncan');
    assert.deepEqual([under.text, under.over], ['≈1,240 tokens', false]);
    const unprofiled = context.promptSizeText({ prompt_tokens: 900, prompt_token_limit: null, over_limit: false });
    assert.equal(unprofiled.text, '≈900 tokens');
    const warm = context.promptSizeText({ prompt_tokens: 12, prompt_token_limit: 2500, over_limit: false, reads_new_message_only: true }, 'Duncan');
    assert.deepEqual([warm.text, warm.over], ['≈12 new tokens to read', false]);
    assert.match(warm.title, /^Duncan still holds the rest/);
    assert.match(context.promptSizeText({ prompt_tokens: 3000, prompt_token_limit: 2500, over_limit: true }).text, /this node reads/);
});

function navigationHarness() {
    const document = { body: { dataset: {} }, activeElement: null };
    const elements = new Map();
    const buttons = [];
    for (const view of ['chat', 'projects', 'cluster', 'settings']) {
        const panel = {
            id: `${view}View`, hidden: view !== 'chat',
            focus(options) {
                assert.equal(this.hidden, false, 'focus must never enter an inactive view');
                assert.equal(options.preventScroll, true);
                document.activeElement = this;
            },
        };
        elements.set(panel.id, panel);
        buttons.push({ tagName: 'BUTTON', dataset: { view }, attributes: {},
            setAttribute(name, value) { this.attributes[name] = value; },
            removeAttribute(name) { delete this.attributes[name]; } });
    }
    const skip = { attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } };
    document.getElementById = (id) => elements.get(id);
    document.querySelectorAll = () => [document.body, ...buttons];
    document.querySelector = () => skip;
    const shell = vm.createContext({ document });
    vm.runInContext(readFileSync(new URL('../static/console.js', import.meta.url), 'utf8'), shell);
    vm.runInContext('renderProjectCards = () => {}; refreshCluster = () => {}; updateConsoleContext = () => {};', shell);
    return { shell, document, elements, buttons, skip };
}

test('every view switch exposes one active target and moves focus beyond navigation', () => {
    const { shell, document, elements, buttons, skip } = navigationHarness();
    for (const view of ['projects', 'cluster', 'settings', 'chat']) {
        shell.setConsoleView(view);
        assert.equal(document.body.dataset.view, view);
        assert.equal(document.activeElement.id, `${view}View`);
        assert.equal(skip.attributes.href, `#${view}View`);
        assert.deepEqual([...elements.values()].filter((item) => !item.hidden).map((item) => item.id), [`${view}View`]);
        assert.deepEqual(buttons.filter((button) => button.attributes['aria-current'] === 'page').map((button) => button.dataset.view), [view]);
    }
});

test('same-view updates and initial setup do not steal focus from an input', () => {
    const { shell, document } = navigationHarness();
    const input = { id: 'draft' };
    document.activeElement = input;
    shell.setConsoleView('chat');
    assert.equal(document.activeElement, input);
    shell.setConsoleView('settings');
    document.activeElement = input;
    shell.setConsoleView('settings');
    assert.equal(document.activeElement, input);
});

test('skip activation explicitly focuses the active view without fragment navigation', () => {
    const { shell, document } = navigationHarness();
    for (const view of ['chat', 'projects', 'cluster', 'settings']) {
        shell.setConsoleView(view);
        let prevented = false;
        document.activeElement = null;
        shell.skipToConsoleContent({ preventDefault() { prevented = true; } });
        assert.equal(prevented, true);
        assert.equal(document.activeElement.id, `${view}View`);
    }
});

test('unknown views fall back to the visible chat landmark', () => {
    const { shell, document, skip } = navigationHarness();
    shell.setConsoleView('projects');
    shell.setConsoleView('unknown');
    assert.equal(document.activeElement.id, 'chatView');
    assert.equal(skip.attributes.href, '#chatView');
});
