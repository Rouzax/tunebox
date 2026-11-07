import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {PRESETS, PROBES, bands, describeSeconds, gpuPlan, planSeconds, shrink, worstGapMs} from '../js/gpucore.js';

test('the plan measures Off first, then every probe at three scales, then every preset at two', () => {
    const plan = gpuPlan();
    assert.deepEqual(plan[0], {kind: 'off', id: 'off', label: 'Off', scale: 1});
    const probes = plan.filter((cell) => cell.kind === 'probe');
    assert.deepEqual(probes.map((cell) => `${cell.id}/${cell.scale}`),
        ['light/1', 'light/2', 'light/3', 'cover/1', 'cover/2', 'cover/3', 'heavy/1', 'heavy/2', 'heavy/3']);
    const presets = plan.filter((cell) => cell.kind === 'preset');
    assert.equal(presets.length, PRESETS.length * 2);
    assert.deepEqual(presets.filter((cell) => cell.load).map((cell) => cell.id), PRESETS.map((preset) => preset.id));
    assert.ok(presets.every((cell, i) => cell.scale === (i % 2 ? 3 : 2) && cell.load === (i % 2 === 0)));
    assert.equal(plan.length, 1 + PROBES.length * 3 + PRESETS.length * 2);
});

test('a full run takes three to four minutes before load times', () => {
    const seconds = planSeconds(gpuPlan(), 10);
    assert.equal(seconds, 210);
    assert.equal(describeSeconds(seconds), 'about 3 min 30 s');
    assert.equal(describeSeconds(180), 'about 3 min');
    assert.equal(describeSeconds(30), 'about 30 s');
});

test('every preset is vendored and runs as WebAssembly', () => {
    for (const preset of PRESETS) {
        const data = JSON.parse(readFileSync(new URL(`../vendor/butterchurn-presets-3.0.0-beta.4/${preset.file}`, import.meta.url), 'utf8'));
        assert.equal(typeof data.init_eqs_eel, 'string', preset.file);
        // No JavaScript form: the page's policy could not run it anyway.
        assert.equal(data.init_eqs_str, undefined, preset.file);
    }
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
