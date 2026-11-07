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

test('gpuPayload carries its own marker, the environment and every cell', () => {
    const data = JSON.parse(gpuPayload(realistic()));
    assert.equal(data.k, KIND);
    assert.equal(data.k, 'gpu');
    assert.equal(data.v, FORMAT);
    assert.equal(data.w, 10);
    assert.deepEqual(data.vw, [1372, 686]);
    assert.equal(data.dpr, 1.75);
    assert.equal(data.e.tx, 4096);
    assert.deepEqual(data.rs, [1, 2, 3]);
    assert.deepEqual(data.bs, [2, 3]);
    assert.deepEqual(Object.keys(data.r), ['off', 'light', 'cover', 'heavy'].concat(PRESETS.map((p) => p.id)));
    assert.deepEqual(data.r.off, [60, 57]);
    assert.deepEqual(data.r.heavy, [60, 57, 60, 57, 60, 57]);
    assert.deepEqual(data.r.cas, [60, 57, 60, 57]);
    assert.deepEqual(data.su.light, 12);
    assert.deepEqual(data.su.hur, [1235, 1500]);
    assert.equal(data.x, undefined);
});

test('gpuPayload leaves cells the run did not reach as null', () => {
    const data = JSON.parse(gpuPayload(realistic({
        results: [{kind: 'probe', id: 'cover', scale: 2, avg_fps: 31.2, worst_fps: 28}, {kind: 'preset', id: 'dsc', scale: 3, avg_fps: 20, worst_fps: 9}],
        setup: {},
    })));
    assert.deepEqual(data.r, {cover: [null, null, 31, 28, null, null], dsc: [null, null, 20, 9]});
    assert.deepEqual(data.su, {});
});

test('gpuPayload names tests that could not run', () => {
    const data = JSON.parse(gpuPayload(realistic({results: [], setup: {}, failed: {gl2: 'missing', cas: 'load failed for a long reason'}})));
    assert.deepEqual(data.x, {gl2: 'missing', cas: 'load failed '});
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
    // Every number at its widest, every string at its cap.
    const text = gpuPayload(realistic({
        userAgent: long,
        env: Object.assign({}, base.env, {renderer: long, vendor: long}),
        results: base.results.map((r) => Object.assign({}, r, {avg_fps: 144.4, worst_fps: 120})),
        setup: Object.assign({}, base.setup, {light: 12345, cover: 12345, heavy: 12345},
            ...PRESETS.map((p) => ({[p.id]: {load: 12345, worst: 12345}}))),
    }));
    const matrix = qrMatrix(qrcode, text, QR_LEVEL);
    assert.ok(matrix.length <= 97, `${matrix.length} modules for ${text.length} bytes`);
    assert.ok(matrix.every((row) => row.length === matrix.length));
});
