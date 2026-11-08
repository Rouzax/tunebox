import assert from 'node:assert/strict';
import {test} from 'node:test';
import qrcode from '../vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {FORMAT, QR_LEVEL, labPayload} from '../js/labreport.js';
import {qrMatrix} from '../js/qr.js';

const STYLES = ['off', 'wave', 'bars', 'led', 'butterfly', 'ring', 'neon', 'ambient', 'scope', 'lava', 'aurora', 'swirl', 'tunnel', 'kaleido', 'synthwave', 'ink', 'md-shuffle'];
// Off and the styles audioMotion does not draw: the lab knows no draw rate
// for them.
const EFFECTS = ['off', 'scope', 'lava', 'aurora', 'swirl', 'tunnel', 'kaleido', 'synthwave', 'ink', 'md-shuffle'];

function realistic(overrides) {
    const results = [];
    for (const id of STYLES) {
        const effect = EFFECTS.indexOf(id) >= 0;
        results.push({style: id, mode: 'normal', avg_fps: 59.94, worst_fps: 57.12, draw_fps: effect ? null : 59.7});
        results.push({style: id, mode: 'low', avg_fps: 59.5, worst_fps: 58.04, draw_fps: effect ? null : 29.96});
    }
    return Object.assign({
        userAgent: 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.109 Safari/537.36 XPENG-G9/5.4.1.1234 CarBrowser/1.0',
        screen: {width: 2560, height: 1440},
        view: {width: 853, height: 1290},
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
    assert.deepEqual(data.vw, [853, 1290]);
    assert.equal(data.dpr, 1.5);
    assert.equal(data.e.g1, 1);
    assert.equal(data.e.g2, 1);
    assert.equal(data.e.tx, 16384);
    assert.deepEqual(Object.keys(data.r), STYLES);
    assert.equal(data.rf, undefined);
    assert.equal(FORMAT, 5);
    assert.deepEqual(data.r.wave, [60, 57, 60, 60, 58, 30]);
    assert.deepEqual(data.r.lava, [60, 57, 60, 58]);
    assert.deepEqual(data.r.ink, [60, 57, 60, 58]);
});

test('labPayload leaves a mode the test did not reach as null', () => {
    const data = JSON.parse(labPayload(realistic({results: [
        {style: 'wave', mode: 'low', avg_fps: 12, worst_fps: 9, draw_fps: 8},
        {style: 'off', mode: 'normal', avg_fps: 59, worst_fps: 58, draw_fps: null},
    ]})));
    assert.deepEqual(data.r, {wave: [null, null, null, 12, 9, 8], off: [59, 58, null, null]});
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
    assert.ok(data.ua.length <= 130);
    assert.ok(data.e.gr.length <= 80);
    assert.ok(data.e.gv.length <= 30);
});

test('labPayload keeps the text ASCII, so the code holds what was measured', () => {
    // The QR library keeps only the low byte of each character.
    const text = labPayload(realistic({userAgent: 'Mozilla/5.0 Café', env: Object.assign(realistic().env, {renderer: 'Mali-G78 ™ “GPU”'})}));
    assert.ok(/^[\x20-\x7e]*$/.test(text), text);
    const data = JSON.parse(text);
    assert.equal(data.ua, 'Mozilla/5.0 Caf?');
    assert.equal(data.e.gr, 'Mali-G78 ? ?GPU?');
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
