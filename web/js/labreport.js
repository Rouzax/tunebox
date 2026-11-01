// The lab's results as one compact JSON text for a QR code. Short keys;
// v changes whenever a key's meaning does.
export const FORMAT = 1;
// Level L: the code is read from a still photo of a screen, where fewer,
// larger modules help more than the extra correction of M.
export const QR_LEVEL = 'L';

// Bounds on the free-text fields, so the code stays small enough to read
// from a photo whatever the browser reports.
const MAX_AGENT = 200;
const MAX_RENDERER = 120;
const MAX_VENDOR = 60;

const tenth = (value) => Math.round(value * 10) / 10;
const cut = (text, max) => (typeof text === 'string' ? text.slice(0, max) : null);
const flag = (value) => (value ? 1 : 0);
const number = (value) => (typeof value === 'number' && Number.isFinite(value) ? value : null);

// r maps each style to [average, worst] in normal quality followed by the
// same in low power; a mode the test did not reach is null.
function byStyle(results) {
    const out = {};
    for (const result of results) {
        const row = out[result.style] || [null, null, null, null];
        const at = result.low ? 2 : 0;
        row[at] = tenth(result.avg_fps);
        row[at + 1] = tenth(result.worst_fps);
        out[result.style] = row;
    }
    return out;
}

// results: [{style, low, avg_fps, worst_fps}].
export function labPayload({userAgent, screen, dpr, windowS, env, results}) {
    return JSON.stringify({
        v: FORMAT,
        ua: cut(userAgent, MAX_AGENT),
        sc: [screen.width, screen.height],
        dpr: number(dpr),
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
        r: byStyle(results),
    });
}
