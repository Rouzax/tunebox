import assert from 'node:assert/strict';
import {test} from 'node:test';
import qrcode from '../vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {PRESETS, gpuPlan} from '../js/gpucore.js';
import {FORMAT, KIND, QR_LEVEL, gpuPayload} from '../js/gpureport.js';
import {qrMatrix} from '../js/qr.js';

function realistic(overrides) {
    const results = gpuPlan().map((cell) => ({kind: cell.kind, id: cell.id, scale: cell.scale, avg_fps: 59.94, worst_fps: 57.12}));
    const setup = {light: 12.4, cover: 31.6, heavy: 48.2};
    for (const preset of PRESETS) {
        setup[preset.id] = {load: 1234.5, worst: 1500.4};
    }
    return Object.assign({
        userAgent: 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.109 Safari/537.36 XPENG-G9/5.4.1.1234 CarBrowser/1.0',
        screen: {width: 2560, height: 1440},
        view: {width: 1372, height: 686},
        fullscreen: false,
        dpr: 1.75,
        windowS: 10,
        env: {
            webgl1: true, webgl2: true,
            renderer: 'ANGLE (Qualcomm, Adreno (TM) 640, OpenGL ES 3.2 V@0502.0 (GIT@b3f2a8e, I8f4d2b, 1612345678) (Date:01/01/21))',
            vendor: 'Google Inc. (Qualcomm)', maxTexture: 4096, offscreen: true, cores: 8, memory: 8,
        },
        results,
        setup,
        failed: {},
    }, overrides);
}

test('gpuPayload carries the marker, the device, the probes and one entry per preset', () => {
    const data = JSON.parse(gpuPayload(realistic()));
    assert.equal(data.k, KIND);
    assert.equal(data.k, 'gpu');
    assert.equal(data.v, 2);
    assert.equal(FORMAT, 2);
    assert.equal(data.w, 10);
    assert.deepEqual(data.vw, [1372, 686]);
    assert.equal(data.fs, 0);
    assert.equal(data.dpr, 1.75);
    assert.equal(data.e.tx, 4096);
    assert.equal(data.rs, undefined);
    assert.equal(data.bs, undefined);
    assert.deepEqual(Object.keys(data.r), ['off', 'light', 'cover', 'heavy']);
    assert.deepEqual(data.r.off, [60, 57]);
    assert.deepEqual(data.r.heavy, [60, 57, 60, 57, 60, 57]);
    assert.deepEqual(data.su, {light: 12, cover: 32, heavy: 48});
    assert.equal(data.p.length, PRESETS.length);
    // The stall, not the load time, and capped.
    assert.deepEqual(data.p[0], [60, 57, 999]);
    assert.equal(data.x, undefined);
    assert.equal(JSON.parse(gpuPayload(realistic({fullscreen: true}))).fs, 1);
});

test('a preset not reached is null, a preset that failed is 0', () => {
    const data = JSON.parse(gpuPayload(realistic({
        results: [{kind: 'preset', id: PRESETS[1].id, scale: 2, avg_fps: 58.4, worst_fps: 55}],
        setup: {[PRESETS[1].id]: {load: 92, worst: 33.4}},
        failed: {[PRESETS[2].id]: 'load'},
    })));
    assert.equal(data.p[0], null);
    assert.deepEqual(data.p[1], [58, 55, 33]);
    assert.equal(data.p[2], 0);
    assert.ok(data.p.slice(3).every((entry) => entry === null));
    assert.equal(data.x, undefined);
    assert.deepEqual(data.r, {});
    assert.deepEqual(data.su, {});
});

test('a run-wide failure is one x entry, whatever it took down', () => {
    const failed = {bc: 'butterchurn'};
    PRESETS.forEach((preset) => {
        failed[preset.id] = 'butterchurn';
    });
    const data = JSON.parse(gpuPayload(realistic({failed})));
    assert.deepEqual(data.x, {bc: 'butterchurn'});
    assert.ok(data.p.every((entry) => entry === 0));
    const gl = JSON.parse(gpuPayload(realistic({results: [], setup: {}, failed: {gl2: 'missing', gl: 'lost after a long while'}})));
    assert.deepEqual(gl.x, {gl2: 'missing', gl: 'lost after a'});
});

test('gpuPayload writes absent environment values as null and keeps the text ASCII', () => {
    const data = JSON.parse(gpuPayload(realistic({
        userAgent: 'Mozilla/5.0 Café',
        env: {webgl1: false, webgl2: false, renderer: null, vendor: null, maxTexture: null, offscreen: false, cores: null, memory: null},
    })));
    assert.deepEqual(data.e, {g1: 0, g2: 0, gr: null, gv: null, tx: null, oc: 0, hc: null, dm: null});
    assert.equal(data.ua, 'Mozilla/5.0 Caf?');
    assert.equal(JSON.parse(gpuPayload(realistic({dpr: 1.7500000476837158}))).dpr, 1.75);
});

test('a full-size payload fits one QR code at level L within version 20', () => {
    const long = 'x'.repeat(1000);
    const base = realistic();
    const setup = {light: 12345, cover: 12345, heavy: 12345};
    PRESETS.forEach((preset) => {
        setup[preset.id] = {load: 12345, worst: 12345};
    });
    // Every number at its widest, every string at its cap, a run-wide failure.
    const text = gpuPayload(realistic({
        userAgent: long,
        fullscreen: true,
        env: Object.assign({}, base.env, {renderer: long, vendor: long, maxTexture: 16384, cores: 10}),
        results: base.results.map((r) => Object.assign({}, r, {avg_fps: 144.4, worst_fps: 120})),
        setup,
        failed: {gl: 'lost after a long while'},
    }));
    const matrix = qrMatrix(qrcode, text, QR_LEVEL);
    assert.ok(matrix.length <= 97, `${matrix.length} modules for ${text.length} bytes`);
    assert.ok(matrix.every((row) => row.length === matrix.length));
});
