// The lab's results as one compact JSON text for a QR code. Short keys;
// v changes whenever a key's meaning does.
export const FORMAT = 4;
// Where each mode's numbers start in a row. Still glow asks only about
// the page, so it has no draw rate.
const MODE_AT = {normal: 0, low: 3, still: 6};
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
// power, then the page's two in normal quality with the cover glow held
// still (na,nw,nd,la,lw,ld,sa,sw, fixed by the format). A mode the test
// did not reach is null, and so is the draw rate of a style audioMotion
// does not draw.
function byStyle(results) {
    const out = {};
    for (const result of results) {
        const row = out[result.style] || [null, null, null, null, null, null, null, null];
        const at = MODE_AT[result.mode];
        // Whole frames per second: the table on screen keeps the tenths, and
        // whole numbers keep the code within version 20.
        row[at] = Math.round(result.avg_fps);
        row[at + 1] = Math.round(result.worst_fps);
        if (result.mode !== 'still') {
            row[at + 2] = typeof result.draw_fps === 'number' ? Math.round(result.draw_fps) : null;
        }
        out[result.style] = row;
    }
    return out;
}

// results: [{style, mode ('normal', 'low' or 'still'), avg_fps, worst_fps,
// draw_fps}].
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
