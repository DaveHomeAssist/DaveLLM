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
