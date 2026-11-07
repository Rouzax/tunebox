// The GPU test's results as one compact JSON text for a QR code, apart
// from the lab's own: k names the test, v changes whenever a key's
// meaning does.
import {PRESET_SCALES, PROBE_SCALES} from './gpucore.js';

export const KIND = 'gpu';
export const FORMAT = 1;
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

// r maps each test to its average and worst frame rate per render scale,
// in the order of rs for Off and the probe shaders (Off has scale 1 only)
// and of bs for the Butterchurn presets. A cell the run did not reach is
// null.
function byTest(results) {
    const out = {};
    for (const result of results) {
        const scales = result.kind === 'preset' ? PRESET_SCALES : result.kind === 'off' ? [1] : PROBE_SCALES;
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

// su: per probe shader the milliseconds to compile and link its programs;
// per preset [loadPreset's milliseconds, the longest frame in ms from the
// switch until SWITCH_WATCH_S after it loaded].
function bySetup(setup) {
    const out = {};
    Object.keys(setup).forEach((id) => {
        const value = setup[id];
        out[id] = typeof value === 'number' ? whole(value) : [whole(value.load), whole(value.worst)];
    });
    return out;
}

// results: [{kind ('off', 'probe' or 'preset'), id, scale, avg_fps,
// worst_fps}]; setup: {probeId: ms, presetId: {load, worst}}; failed:
// {id: reason} for tests that could not run.
export function gpuPayload({userAgent, screen, view, dpr, windowS, env, results, setup, failed}) {
    const payload = {
        k: KIND,
        v: FORMAT,
        ua: cut(userAgent, MAX_AGENT),
        sc: [screen.width, screen.height],
        vw: [view.width, view.height],
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
        rs: PROBE_SCALES,
        bs: PRESET_SCALES,
        r: byTest(results),
        su: bySetup(setup || {}),
    };
    const reasons = failed || {};
    if (Object.keys(reasons).length) {
        payload.x = {};
        Object.keys(reasons).forEach((id) => {
            payload.x[id] = cut(String(reasons[id]), 12);
        });
    }
    return JSON.stringify(payload);
}
