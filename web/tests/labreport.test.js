import assert from 'node:assert/strict';
import {test} from 'node:test';
import qrcode from '../vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {FORMAT, QR_LEVEL, labPayload} from '../js/labreport.js';
import {qrMatrix} from '../js/qr.js';

const STYLES = ['wave', 'bars', 'led', 'butterfly', 'ring', 'neon', 'ambient', 'scope', 'lava', 'milkdrop', 'aurora'];

function realistic(overrides) {
    const results = [];
    for (const id of STYLES) {
        results.push({style: id, low: false, avg_fps: 59.94, worst_fps: 57.12});
        results.push({style: id, low: true, avg_fps: 31.5, worst_fps: 28.04});
    }
    return Object.assign({
        userAgent: 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.109 Safari/537.36 XPENG-G9/5.4.1.1234 CarBrowser/1.0',
        screen: {width: 2560, height: 1440},
        dpr: 1.5,
        windowS: 10,
        env: {
            webgl1: true, webgl2: true,
            renderer: 'ANGLE (Qualcomm, Adreno (TM) 650, OpenGL ES 3.2 V@0502.0 (GIT@b3f2a8e, I8f4d2b, 1612345678) (Date:01/01/21))',
            vendor: 'Google Inc. (Qualcomm)', maxTexture: 16384, offscreen: true, cores: 8, memory: 8,
        },
        results,
    }, overrides);
}

test('labPayload is compact JSON with the format version and every result', () => {
    const data = JSON.parse(labPayload(realistic()));
    assert.equal(data.v, FORMAT);
    assert.equal(data.w, 10);
    assert.deepEqual(data.sc, [2560, 1440]);
    assert.equal(data.dpr, 1.5);
    assert.equal(data.e.g1, 1);
    assert.equal(data.e.g2, 1);
    assert.equal(data.e.tx, 16384);
    assert.deepEqual(Object.keys(data.r), STYLES);
    assert.deepEqual(data.r.wave, [59.9, 57.1, 31.5, 28]);
});

test('labPayload leaves a mode the test did not reach as null', () => {
    const data = JSON.parse(labPayload(realistic({results: [{style: 'wave', low: false, avg_fps: 12, worst_fps: 9}]})));
    assert.deepEqual(data.r, {wave: [12, 9, null, null]});
});

test('labPayload writes absent environment values as null', () => {
    const data = JSON.parse(labPayload(realistic({
        env: {webgl1: false, webgl2: false, renderer: null, vendor: null, maxTexture: null, offscreen: false, cores: null, memory: null},
    })));
    assert.deepEqual(data.e, {g1: 0, g2: 0, gr: null, gv: null, tx: null, oc: 0, hc: null, dm: null});
});

test('labPayload cuts long strings so the code stays small', () => {
    const long = 'x'.repeat(1000);
    const data = JSON.parse(labPayload(realistic({userAgent: long, env: Object.assign(realistic().env, {renderer: long, vendor: long})})));
    assert.ok(data.ua.length <= 200);
    assert.ok(data.e.gr.length <= 120);
    assert.ok(data.e.gv.length <= 60);
});

test('a full-size payload fits one QR code at the lab\'s level', () => {
    const long = 'x'.repeat(1000);
    const text = labPayload(realistic({userAgent: long, env: Object.assign(realistic().env, {renderer: long, vendor: long})}));
    const matrix = qrMatrix(qrcode, text, QR_LEVEL);
    // Version 20 or below (97 modules): still a photographable code on a
    // car screen.
    assert.ok(matrix.length <= 97, `${matrix.length} modules for ${text.length} bytes`);
    assert.ok(matrix.every((row) => row.length === matrix.length));
});

test('a realistic payload makes a smaller code', () => {
    const text = labPayload(realistic());
    assert.ok(qrMatrix(qrcode, text, QR_LEVEL).length <= 89, `${text.length} bytes`);
});
