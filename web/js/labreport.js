// The lab's results as one compact JSON text for a QR code. Short keys;
// v changes whenever a key's meaning does.
export const FORMAT = 2;
// The order of each style's numbers in r, carried in the payload so a
// photo of the code reads on its own.
const ROW_FIELDS = 'na,nw,nd,la,lw,ld';
// Level L: the code is read from a still photo of a screen, where fewer,
// larger modules help more than the extra correction of M.
export const QR_LEVEL = 'L';

// Bounds on the free-text fields, so the code stays small enough to read
// from a photo whatever the browser reports.
const MAX_AGENT = 170;
const MAX_RENDERER = 100;
const MAX_VENDOR = 40;

const tenth = (value) => Math.round(value * 10) / 10;
// The QR library encodes only the low byte of each character, so anything
// outside printable ASCII becomes '?'.
const cut = (text, max) => (typeof text === 'string' ? text.slice(0, max).replace(/[^\x20-\x7e]/g, '?') : null);
const flag = (value) => (value ? 1 : 0);
const number = (value) => (typeof value === 'number' && Number.isFinite(value) ? value : null);

// r maps each style to the page's average and worst frame rate and the
// visualizer's own draw rate in normal quality, then the same in low
// power (ROW_FIELDS). A mode the test did not reach is null, and so is
// the draw rate of a style audioMotion does not draw.
function byStyle(results) {
    const out = {};
    for (const result of results) {
        const row = out[result.style] || [null, null, null, null, null, null];
        const at = result.low ? 3 : 0;
        row[at] = tenth(result.avg_fps);
        row[at + 1] = tenth(result.worst_fps);
        // Whole draws per second: enough to tell 30 from 60, and it keeps the
        // code within version 20.
        row[at + 2] = typeof result.draw_fps === 'number' ? Math.round(result.draw_fps) : null;
        out[result.style] = row;
    }
    return out;
}

// results: [{style, low, avg_fps, worst_fps, draw_fps}].
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
        rf: ROW_FIELDS,
        r: byStyle(results),
    });
}
