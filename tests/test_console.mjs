import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const context = vm.createContext({});
vm.runInContext(readFileSync(new URL('../static/console.js', import.meta.url), 'utf8'), context);
const now = Date.parse('2026-10-01T12:00:00Z');
const age = (hours) => context.conversationAge({ updated_at: new Date(now - hours * 3600000).toISOString() }, now);

test('age bands cover every boundary without losing the requested categories', () => {
    for (const [hours, band] of [[0, 'fresh'], [23.99, 'fresh'], [24, 'recent'], [71.99, 'recent'], [72, 'week'], [168, 'week'], [168.01, 'old']]) {
        assert.equal(age(hours).band, band, `${hours} hours`);
    }
    assert.equal(age(-1).text, '0m');
    assert.equal(context.conversationAge({ updated_at: 'invalid' }, now).band, 'unknown');
    assert.equal(context.conversationAge({ created_at: new Date(now).toISOString() }, now).band, 'fresh');
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
