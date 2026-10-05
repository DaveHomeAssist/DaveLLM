import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// A minimal DOM: just enough of createElement, text nodes and attributes for appendMarkdown.
class FakeText {
    constructor(text) { this.nodeType = 3; this.data = String(text); }
    get textContent() { return this.data; }
}

class FakeElement {
    constructor(tag) {
        this.nodeType = 1;
        this.tagName = tag.toUpperCase();
        this.childNodes = [];
        this.attributes = new Map();
        this.className = '';
        this.style = {};
        this.listeners = {};
    }
    append(...nodes) { for (const node of nodes) this.childNodes.push(typeof node === 'string' ? new FakeText(node) : node); }
    appendChild(node) { this.append(node); return node; }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    addEventListener(type, handler) { this.listeners[type] = handler; }
    get lastElementChild() { return [...this.childNodes].reverse().find((node) => node.nodeType === 1) || null; }
    get textContent() { return this.childNodes.map((node) => node.textContent).join(''); }
    set textContent(value) { this.childNodes = value ? [new FakeText(value)] : []; }
}

// Serialize to compact HTML so assertions read like the rendered markup.
function html(node) {
    if (node.nodeType === 3) return node.data.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const tag = node.tagName.toLowerCase();
    const attrs = [...node.attributes].map(([name, value]) => ` ${name}="${value}"`).join('');
    const cls = node.className ? ` class="${node.className}"` : '';
    const style = node.style.textAlign ? ` style="text-align:${node.style.textAlign}"` : '';
    return `<${tag}${cls}${attrs}${style}>${node.childNodes.map(html).join('')}</${tag}>`;
}

const document = {
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (text) => new FakeText(text),
};
const context = vm.createContext({ document });
vm.runInContext(readFileSync(new URL('../static/console.js', import.meta.url), 'utf8'), context);

const render = (source) => {
    const container = new FakeElement('div');
    context.appendMarkdown(container, source);
    return container.childNodes.map(html).join('');
};

test('inline emphasis, code, strikethrough and escapes', () => {
    assert.equal(
        render('A **bold** and *italic* with `x < y`, ~~gone~~, _under_ and \\*literal\\*.'),
        '<p>A <strong>bold</strong> and <em>italic</em> with <code>x &lt; y</code>, <del>gone</del>, <em>under</em> and *literal*.</p>',
    );
    assert.equal(render('**bold with *nested* italic**'), '<p><strong>bold with <em>nested</em> italic</strong></p>');
    assert.equal(render('snake_case_name and 2 * 3 * 4 stay plain'), '<p>snake_case_name and 2 * 3 * 4 stay plain</p>');
});

test('model HTML stays text and only http(s) or mailto become links', () => {
    assert.equal(render('<img src=x onerror=alert(1)> <b>hi</b>'), '<p>&lt;img src=x onerror=alert(1)&gt; &lt;b&gt;hi&lt;/b&gt;</p>');
    assert.equal(
        render('[docs](https://ollama.com) and [bad](javascript:alert(1)) and https://example.com/a.'),
        '<p><a href="https://ollama.com" target="_blank" rel="noopener noreferrer">docs</a> and bad and '
        + '<a href="https://example.com/a" target="_blank" rel="noopener noreferrer">https://example.com/a</a>.</p>',
    );
    assert.equal(
        render('[https://a.example](https://a.example)'),
        '<p><a href="https://a.example" target="_blank" rel="noopener noreferrer">https://a.example</a></p>',
    );
});

test('headings, rules and blockquotes split from neighbouring text', () => {
    assert.equal(render('### Wrap-up\nLine one\ncontinues'), '<h4>Wrap-up</h4><p>Line one\ncontinues</p>');
    assert.equal(render('# Title'), '<h2>Title</h2>');
    assert.equal(render('Above\n\n---\n\nBelow'), '<p>Above</p><hr></hr><p>Below</p>');
    assert.equal(render('> Note: **quoted**\n> second line'), '<blockquote><p>Note: <strong>quoted</strong>\nsecond line</p></blockquote>');
});

test('unordered, ordered and nested lists', () => {
    assert.equal(render('- one\n- two'), '<ul><li>one</li><li>two</li></ul>');
    assert.equal(
        render('1. Open the page.\n2. Pull a model:\n   - `llama3` for chat\n   - `llava` for images\n3. Restart.'),
        '<ol><li>Open the page.</li><li>Pull a model:<ul><li><code>llama3</code> for chat</li><li><code>llava</code> for images</li></ul></li><li>Restart.</li></ol>',
    );
    assert.equal(render('3. three\n4. four'), '<ol start="3"><li>three</li><li>four</li></ol>');
    assert.equal(render('Intro:\n- a\n- b\n\nAfter'), '<p>Intro:</p><ul><li>a</li><li>b</li></ul><p>After</p>');
    assert.equal(render('- a\n\n- b'), '<ul><li>a</li><li>b</li></ul>');
});

test('tables keep alignment and inline formatting', () => {
    assert.equal(
        render('| Node | Speed |\n|------|------:|\n| **Dominic** | 31 tok/s |\n| Walter | 12 tok/s |'),
        '<div class="table-wrap"><table><thead><tr><th>Node</th><th style="text-align:right">Speed</th></tr></thead>'
        + '<tbody><tr><td><strong>Dominic</strong></td><td style="text-align:right">31 tok/s</td></tr>'
        + '<tr><td>Walter</td><td style="text-align:right">12 tok/s</td></tr></tbody></table></div>',
    );
    assert.equal(render('a | b\nnot a divider'), '<p>a | b\nnot a divider</p>');
});

test('code fences keep their text, including an unclosed fence while streaming', () => {
    const fenced = render('```python\ndef hello():\n    return "<world>"\n```\nafter');
    assert.match(fenced, /<span>python<\/span>/);
    assert.match(fenced, /<pre><code>def hello\(\):\n {4}return "&lt;world&gt;"<\/code><\/pre><\/div><p>after<\/p>$/);
    assert.match(render('```js\nconst a = 1;'), /<pre><code>const a = 1;<\/code><\/pre>/);
    assert.match(render('~~~\n**not bold**\n~~~'), /<code>\*\*not bold\*\*<\/code>/);
});
