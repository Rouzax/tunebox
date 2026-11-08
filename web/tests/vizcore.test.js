import assert from 'node:assert/strict';
import {test} from 'node:test';
import {SHADERS} from '../js/gpushaders.js';
import {PRESETS, SHUFFLE_ID} from '../js/milkdropcore.js';
import {GROUPS, STYLES, availableStyles, chooseStyle, groupOf, groupsFor} from '../js/vizcore.js';

test('every style but Off sits in exactly one of the four groups', () => {
    assert.deepEqual(GROUPS.map((g) => g.id), ['bars', 'waves', 'ambient', 'milkdrop']);
    assert.ok(GROUPS.every((g) => g.image === `icons/viz/${g.id}.webp`));
    assert.equal(new Set(STYLES.map((s) => s.id)).size, STYLES.length);
    const byGroup = (id) => STYLES.filter((s) => s.group === id).map((s) => s.id);
    assert.deepEqual(byGroup('bars'), ['bars', 'led', 'butterfly', 'ring', 'neon', 'ambient']);
    assert.deepEqual(byGroup('waves'), ['wave', 'scope']);
    assert.deepEqual(byGroup('ambient'), ['lava', 'aurora', 'swirl', 'tunnel', 'kaleido', 'synthwave', 'ink']);
    assert.deepEqual(byGroup('milkdrop'), [SHUFFLE_ID].concat(PRESETS.map((p) => p.id)));
    assert.deepEqual(STYLES.filter((s) => s.group === null).map((s) => s.id), ['off']);
    assert.equal(STYLES.find((s) => s.id === 'milkdrop'), undefined);
});

test('each style names its engine', () => {
    const engine = (id) => STYLES.find((s) => s.id === id).engine;
    assert.equal(engine('wave'), 'motion');
    assert.equal(engine('scope'), 'effects');
    assert.equal(engine('aurora'), 'effects');
    assert.equal(engine('swirl'), 'effects');
    assert.equal(STYLES.find((s) => s.id === 'swirl').effect, 'swirl');
    assert.equal(engine(SHUFFLE_ID), 'milkdrop');
    assert.equal(engine(PRESETS[3].id), 'milkdrop');
    assert.equal(engine('off'), 'off');
    assert.equal(STYLES.find((s) => s.id === SHUFFLE_ID).shuffle, true);
    assert.equal(STYLES.find((s) => s.id === PRESETS[3].id).preset, PRESETS[3].file);
});

test('without WebGL 2 there is no MilkDrop group and no MilkDrop style', () => {
    assert.deepEqual(groupsFor(false).map((g) => g.id), ['bars', 'waves', 'ambient']);
    assert.equal(groupsFor(true).length, 4);
    assert.ok(availableStyles(false).every((s) => s.engine !== 'milkdrop'));
    assert.equal(availableStyles(true).length, STYLES.length);
});

test('chooseStyle keeps a known choice and maps the retired and the impossible to a working one', () => {
    assert.equal(chooseStyle('neon', true), 'neon');
    assert.equal(chooseStyle('off', false), 'off');
    // The 2D style once called Milkdrop is Swirl now: the same look.
    assert.equal(chooseStyle('milkdrop', true), 'swirl');
    assert.equal(chooseStyle('milkdrop', false), 'swirl');
    assert.equal(chooseStyle(PRESETS[5].id, true), PRESETS[5].id);
    assert.equal(chooseStyle(PRESETS[5].id, false), 'wave');
    assert.equal(chooseStyle('md-a-preset-no-longer-shipped', true), SHUFFLE_ID);
    assert.equal(chooseStyle('nonsense', true), 'wave');
    assert.equal(chooseStyle(null, true), 'wave');
});

test('groupOf finds a style\'s group; Off and unknown ids have none', () => {
    assert.equal(groupOf('ring'), 'bars');
    assert.equal(groupOf('scope'), 'waves');
    assert.equal(groupOf(SHUFFLE_ID), 'milkdrop');
    assert.equal(groupOf('off'), null);
    assert.equal(groupOf('nope'), null);
});

test('the GPU styles name their shader and start scale', () => {
    const gpu = STYLES.filter((s) => s.engine === 'gpu');
    assert.deepEqual(gpu.map((s) => [s.id, s.label, s.shader, s.scale]), [
        ['tunnel', 'Cover tunnel', 'tunnel', 1],
        ['kaleido', 'Cover kaleidoscope', 'kaleido', 1],
        ['synthwave', 'Synthwave road', 'synthwave', 1],
        ['ink', 'Liquid ink', 'ink', 2],
    ]);
    assert.ok(gpu.every((s) => SHADERS[s.shader]));
});

test('without WebGL 2 the GPU styles are not offered, and a stored one plays Wave', () => {
    assert.ok(availableStyles(false).every((s) => s.engine !== 'gpu'));
    assert.deepEqual(groupsFor(false).map((g) => g.id), ['bars', 'waves', 'ambient']);
    assert.equal(chooseStyle('tunnel', false), 'wave');
    assert.equal(chooseStyle('ink', true), 'ink');
});
