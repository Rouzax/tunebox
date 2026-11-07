import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {PASS, PRESETS, PROBES, bands, capMs, describeSeconds, gpuPlan, planSeconds, presetVerdict, shrink, worstGapMs} from '../js/gpucore.js';

test('the plan measures Off first, then every probe at three scales, then every preset once at half size', () => {
    const plan = gpuPlan();
    assert.deepEqual(plan[0], {kind: 'off', id: 'off', label: 'Off', scale: 1});
    const probes = plan.filter((cell) => cell.kind === 'probe');
    assert.deepEqual(probes.map((cell) => `${cell.id}/${cell.scale}`),
        ['light/1', 'light/2', 'light/3', 'cover/1', 'cover/2', 'cover/3', 'heavy/1', 'heavy/2', 'heavy/3']);
    const presets = plan.filter((cell) => cell.kind === 'preset');
    assert.deepEqual(presets.map((cell) => cell.id), PRESETS.map((preset) => preset.id));
    assert.ok(presets.every((cell) => cell.scale === 2 && cell.load === true));
    assert.equal(plan.length, 1 + PROBES.length * 3 + PRESETS.length);
});

test('a full run takes a little over five minutes before load times', () => {
    const seconds = planSeconds(gpuPlan(), 10);
    assert.equal(seconds, 316);
    assert.equal(describeSeconds(seconds), 'about 5 min 16 s');
    assert.equal(describeSeconds(180), 'about 3 min');
    assert.equal(describeSeconds(30), 'about 30 s');
});

test('a preset passes only with all three: average, worst second and load', () => {
    assert.deepEqual(PASS, {avg: 55, worst: 45, stallMs: 100});
    assert.deepEqual(presetVerdict({avg_fps: 55, worst_fps: 45}, 100), {pass: true, misses: []});
    assert.deepEqual(presetVerdict({avg_fps: 54.9, worst_fps: 45}, 100), {pass: false, misses: ['average']});
    assert.deepEqual(presetVerdict({avg_fps: 59, worst_fps: 44}, 33), {pass: false, misses: ['worst']});
    assert.deepEqual(presetVerdict({avg_fps: 59, worst_fps: 57}, 101), {pass: false, misses: ['load']});
    // The castle in the car: smooth once loaded, a 1283 ms frame on load.
    assert.deepEqual(presetVerdict({avg_fps: 57, worst_fps: 51}, 1283), {pass: false, misses: ['load']});
    assert.deepEqual(presetVerdict({avg_fps: 2, worst_fps: 0}, 967), {pass: false, misses: ['average', 'worst', 'load']});
    // A load that was never timed cannot pass.
    assert.deepEqual(presetVerdict({avg_fps: 60, worst_fps: 59}, null), {pass: false, misses: ['load']});
});

test('capMs rounds and caps at 999', () => {
    assert.equal(capMs(32.6), 33);
    assert.equal(capMs(1283), 999);
    assert.equal(capMs(999.4), 999);
    assert.equal(capMs(undefined), null);
    assert.equal(capMs(NaN), null);
});

test('every preset is vendored and runs as WebAssembly', () => {
    for (const preset of PRESETS) {
        const data = JSON.parse(readFileSync(new URL(`../vendor/butterchurn-presets-3.0.0-beta.4/${preset.file}`, import.meta.url), 'utf8'));
        assert.equal(typeof data.init_eqs_eel, 'string', preset.file);
        // No JavaScript form: the page's policy could not run it anyway.
        assert.equal(data.init_eqs_str, undefined, preset.file);
    }
});

test('the candidates are the 18 agreed presets, ids unique and short', () => {
    assert.equal(PRESETS.length, 18);
    assert.deepEqual(PRESETS.map((preset) => preset.id),
        ['hu', 'mb', 'dm', 'sm', 'rw', 'ps', 'dv', 'rt', 'rd', 'hk', 'po', 'ud', 'tv', 'sg', 'gm', 'bt', 'dh', 'lc']);
    assert.equal(new Set(PRESETS.map((preset) => preset.file)).size, 18);
    assert.ok(PRESETS.every((preset) => /^[a-z]{2}$/.test(preset.id) && preset.label.length <= 45));
});

test('bands average the bins of bass, mids and highs', () => {
    // 48000 Hz over 512 points: 93.75 Hz per bin.
    const bins = new Uint8Array(256);
    bins.fill(255, 1, 3);
    const quiet = bands(bins, 48000, 512);
    assert.equal(quiet.bass, 1);
    assert.equal(quiet.mid, 0);
    assert.equal(quiet.high, 0);
    bins.fill(51, 3, 86);
    assert.ok(Math.abs(bands(bins, 48000, 512).high - 0.2) < 1e-9);
    assert.deepEqual(bands(new Uint8Array(0), 48000, 512), {bass: 0, mid: 0, high: 0});
});

test('shrink keeps each group\'s loudest bin', () => {
    const bins = new Uint8Array(256);
    bins[5] = 200;
    bins[255] = 7;
    const out = shrink(bins, new Uint8Array(128));
    assert.equal(out[2], 200);
    assert.equal(out[127], 7);
    assert.equal(out.reduce((sum, value) => sum + value, 0), 207);
});

test('worstGapMs is the longest frame', () => {
    assert.equal(worstGapMs([0, 16, 33, 150, 166]), 117);
    assert.equal(worstGapMs([5]), 0);
    assert.equal(worstGapMs([]), 0);
});
