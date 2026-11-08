// How large the GPU engines draw: the canvas's device pixels, capped on
// the long side so full screen or a large monitor costs no more than the
// car's split window, divided by a render scale. A device that cannot
// keep STEP_FPS draws a step smaller for the rest of the page view; it
// never steps back up, so the size does not swing back and forth.

export const RENDER_CAP = 1280;
// Render scales are divisors of the capped size: 1, 1/2, 1/3.
export const MAX_SCALE = 3;
export const STEP_FPS = 50;
export const STEP_WINDOW_MS = 3000;
// A gap between frames this long is a page in the background or a one-off
// stall (a shader building, a texture upload), not slow drawing.
export const STEP_GAP_MS = 250;

// The canvas's size in device pixels, the long side capped at RENDER_CAP,
// divided by scale (2 for half size).
export function renderSize(cssWidth, cssHeight, dpr, scale) {
    let width = cssWidth * dpr;
    let height = cssHeight * dpr;
    const long = Math.max(width, height);
    if (long > RENDER_CAP) {
        width = (width * RENDER_CAP) / long;
        height = (height * RENDER_CAP) / long;
    }
    return {width: Math.max(1, Math.round(width / scale)), height: Math.max(1, Math.round(height / scale))};
}

// The scale a style starts at: its own, a step smaller in low power, and
// never larger than a step-down already reached in this page view.
export function startScale(base, lowPower, floor) {
    return Math.min(MAX_SCALE, Math.max(base + (lowPower ? 1 : 0), floor));
}

// Counts frames in windows of STEP_WINDOW_MS; frame(now) is true at the
// end of a window drawn below STEP_FPS.
export function createPace() {
    let from = null;
    let last = 0;
    let count = 0;
    return {
        reset() {
            from = null;
        },
        frame(now) {
            if (from === null || now - last > STEP_GAP_MS) {
                from = now;
                last = now;
                count = 0;
                return false;
            }
            count += 1;
            last = now;
            if (now - from < STEP_WINDOW_MS) {
                return false;
            }
            const fps = (count * 1000) / (now - from);
            from = now;
            count = 0;
            return fps < STEP_FPS;
        },
    };
}
