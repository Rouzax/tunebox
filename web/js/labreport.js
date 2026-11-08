// The lab's results as one compact JSON text for a QR code. Short keys;
// v changes whenever a key's meaning does.
export const FORMAT = 5;
// Level L: the code is read from a still photo of a screen, where fewer,
// larger modules help more than the extra correction of M.
export const QR_LEVEL = 'L';

// Bounds on the free-text fields, so the code stays small enough to read
// from a photo whatever the browser reports.
const MAX_AGENT = 130;
const MAX_RENDERER = 80;
const MAX_VENDOR = 30;

// The QR library encodes only the low byte of each character, so anything
// outside printable ASCII becomes '?'.
const cut = (text, max) => (typeof text === 'string' ? text.slice(0, max).replace(/[^\x20-\x7e]/g, '?') : null);
const flag = (value) => (value ? 1 : 0);
const number = (value) => (typeof value === 'number' && Number.isFinite(value) ? value : null);

// r maps each style to the page's average and worst frame rate and the
// visualizer's own draw rate in normal quality, then the same in low
// power: [na, nw, nd, la, lw, ld]. A style with no draw count (every
// engine but audioMotion's) leaves the draw rates out: [na, nw, la, lw].
// A mode the test did not reach is null.
function byStyle(results) {
    const counted = {};
    for (const result of results) {
        if (typeof result.draw_fps === 'number') {
            counted[result.style] = true;
        }
    }
    const out = {};
    for (const result of results) {
        const draws = Boolean(counted[result.style]);
        const row = out[result.style] || (draws ? [null, null, null, null, null, null] : [null, null, null, null]);
        const at = result.mode === 'low' ? (draws ? 3 : 2) : 0;
        // Whole frames per second: the table on screen keeps the tenths, and
        // whole numbers keep the code within version 20.
        row[at] = Math.round(result.avg_fps);
        row[at + 1] = Math.round(result.worst_fps);
        if (draws) {
            row[at + 2] = typeof result.draw_fps === 'number' ? Math.round(result.draw_fps) : null;
        }
        out[result.style] = row;
    }
    return out;
}

// results: [{style, mode ('normal' or 'low'), avg_fps, worst_fps, draw_fps}].
export function labPayload({userAgent, screen, view, dpr, windowS, env, results}) {
    return JSON.stringify({
        v: FORMAT,
        ua: cut(userAgent, MAX_AGENT),
        sc: [screen.width, screen.height],
        // The window, which a split screen makes smaller than the screen.
        vw: [view.width, view.height],
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
