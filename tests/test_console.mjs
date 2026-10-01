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
