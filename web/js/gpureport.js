// The GPU test's results as one compact JSON text for a QR code, apart
// from the lab's own: k names the test, v changes whenever a key's
// meaning does.
import {PRESETS, PRESET_SCALES, PROBE_SCALES, capMs} from './gpucore.js';

export const KIND = 'gpu';
export const FORMAT = 2;
// Level L, as the lab's code: read from a still photo of a screen.
export const QR_LEVEL = 'L';

const MAX_AGENT = 130;
const MAX_RENDERER = 80;
const MAX_VENDOR = 30;

// The QR library encodes only the low byte of each character, so anything
// outside printable ASCII becomes '?'.
const cut = (text, max) => (typeof text === 'string' ? text.slice(0, max).replace(/[^\x20-\x7e]/g, '?') : null);
const flag = (value) => (value ? 1 : 0);
const number = (value) => (typeof value === 'number' && Number.isFinite(value) ? value : null);
// A reported ratio such as 1.7500000476837158 would cost the code a row.
const ratio = (value) => (typeof value === 'number' && Number.isFinite(value) ? Math.round(value * 100) / 100 : null);
const whole = (value) => (typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : null);

// Failures that stop more than one test: no WebGL 2, the context lost,
// Butterchurn unable to start. A single preset's failure is its 0 in p.
const RUN_WIDE = ['gl2', 'gl', 'bc'];

// r maps Off and each probe shader to its average and worst frame rate
// per render scale in PROBE_SCALES order (Off has scale 1 only). A cell
// the run did not reach is null.
function byTest(results) {
    const out = {};
    for (const result of results) {
        if (result.kind === 'preset') {
            continue;
        }
        const scales = result.kind === 'off' ? [1] : PROBE_SCALES;
        const at = scales.indexOf(result.scale);
        if (at < 0) {
            continue;
        }
        const row = out[result.id] || scales.reduce((cells) => cells.concat([null, null]), []);
        row[at * 2] = whole(result.avg_fps);
        row[at * 2 + 1] = whole(result.worst_fps);
        out[result.id] = row;
    }
    return out;
}

// p holds one entry per preset in PRESETS order: [average fps, worst fps,
// the longest frame in ms around its load], 0 when it failed, null when
// the run did not reach it. Its load time itself is left out: the pass
// rule reads the stall.
function byPreset(results, setup, failed) {
    return PRESETS.map((preset) => {
        if (failed[preset.id]) {
            return 0;
        }
        const result = results.filter((r) => r.kind === 'preset' && r.id === preset.id && r.scale === PRESET_SCALES[0])[0];
        if (!result) {
            return null;
        }
        const timed = setup[preset.id];
        return [whole(result.avg_fps), whole(result.worst_fps), capMs(timed && timed.worst)];
    });
}

// su: per probe shader the milliseconds to compile and link its programs.
function bySetup(setup) {
    const out = {};
    Object.keys(setup).forEach((id) => {
        if (typeof setup[id] === 'number') {
            out[id] = capMs(setup[id]);
        }
    });
    return out;
}

// view and fullscreen describe the page when the run started. results:
// [{kind ('off', 'probe' or 'preset'), id, scale, avg_fps, worst_fps}];
// setup: {probeId: ms, presetId: {load, worst}}; failed: {id: reason} for
// tests that could not run.
export function gpuPayload({userAgent, screen, view, fullscreen, dpr, windowS, env, results, setup, failed}) {
    const reasons = failed || {};
    const timed = setup || {};
    const payload = {
        k: KIND,
        v: FORMAT,
        ua: cut(userAgent, MAX_AGENT),
        sc: [screen.width, screen.height],
        vw: [view.width, view.height],
        fs: flag(fullscreen),
        dpr: ratio(dpr),
        w: windowS,
        e: {
            g1: flag(env.webgl1),
            g2: flag(env.webgl2),
            gr: cut(env.renderer, MAX_RENDERER),
            gv: cut(env.vendor, MAX_VENDOR),
            tx: number(env.maxTexture),
            oc: flag(env.offscreen),
            hc: number(env.cores),
            dm: number(env.memory),
        },
        r: byTest(results),
        su: bySetup(timed),
        p: byPreset(results, timed, reasons),
    };
    const runWide = RUN_WIDE.filter((id) => reasons[id]);
    if (runWide.length) {
        payload.x = {};
        runWide.forEach((id) => {
            payload.x[id] = cut(String(reasons[id]), 12);
        });
    }
    return JSON.stringify(payload);
}
