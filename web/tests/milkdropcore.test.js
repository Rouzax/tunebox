import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {BLEND_S, PRESETS, PRESET_DIR, SHUFFLE_ID, SHUFFLE_S, createCycle, followBass} from '../js/milkdropcore.js';

test('the 17 presets shown, each vendored and WebAssembly-only, with short names', () => {
    assert.equal(PRESETS.length, 17);
    assert.equal(new Set(PRESETS.map((p) => p.id)).size, 17);
    assert.ok(PRESETS.every((p) => p.label.length <= 14 && !/ - /.test(p.label)));
    assert.equal(PRESETS.find((p) => p.id === 'md-geiss-reaction-diffusion-2'), undefined);
    for (const preset of PRESETS) {
        assert.equal(preset.id, `md-${preset.file.replace(/\.json$/, '')}`);
        assert.notEqual(preset.id, SHUFFLE_ID);
        const data = JSON.parse(readFileSync(new URL(`../${PRESET_DIR}${preset.file}`, import.meta.url), 'utf8'));
        assert.equal(typeof data.init_eqs_eel, 'string', preset.file);
        assert.equal(data.init_eqs_str, undefined, preset.file);
    }
    assert.equal(SHUFFLE_S, 45);
    assert.equal(BLEND_S, 2.7);
});

function seeded(seed) {
    let state = seed;
    return () => {
        state = (state * 1103515245 + 12345) % 2147483648;
        return state / 2147483648;
    };
}

test('a shuffle plays every preset once before any repeats, never the one showing', () => {
    const ids = ['a', 'b', 'c', 'd'];
    const cycle = createCycle(ids, seeded(7));
    let current = null;
    const seen = [];
    for (let i = 0; i < 8; i++) {
        const next = cycle.next(current);
        assert.notEqual(next, current);
        seen.push(next);
        current = next;
    }
    assert.deepEqual([...seen.slice(0, 4)].sort(), ids);
    assert.deepEqual([...seen.slice(4)].sort(), ids);
});

test('a dropped preset never comes back, and an empty cycle gives null', () => {
    const cycle = createCycle(['a', 'b'], seeded(1));
    cycle.drop('a');
    for (let i = 0; i < 5; i++) {
        assert.equal(cycle.next(null), 'b');
    }
    // Only the one showing is left: it stays.
    assert.equal(cycle.next('b'), 'b');
    cycle.drop('b');
    assert.equal(cycle.next(null), null);
});

test('followBass reads the level against a decaying peak with a floor', () => {
    assert.deepEqual(followBass(0.1, 0.2), {glow: 0.1 / 0.2 * 0.35, peak: 0.2});
    const loud = followBass(0.8, 0.2);
    assert.equal(loud.peak, 0.8);
    assert.equal(loud.glow, 0.35);
    assert.equal(followBass(0, 0.5).peak, 0.5 * 0.995);
});
