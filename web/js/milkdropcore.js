// The player's MilkDrop, the pure part: the presets that passed the car's
// GPU test (smooth at half size, no long stall on load), the size it draws
// at, the order Shuffle plays them in and the bass the cover glow follows.

export const PRESET_DIR = 'vendor/butterchurn-presets-3.0.0-beta.4/';
export const SHUFFLE_ID = 'md-shuffle';
// A new preset every 45 s, blended over Butterchurn's usual 2.7 s.
export const SHUFFLE_S = 45;
export const BLEND_S = 2.7;
// Device pixels on the long side before the render scale: full screen or a
// large monitor costs no more than the car's split window.
export const RENDER_CAP = 1280;

export const PRESETS = [
    ['geiss-hurricane.json', 'Geiss - Hurricane'],
    ['flexi-mindblob-shiny-mix.json', 'Flexi - mindblob'],
    ['martin-disco-mix-4.json', 'martin - disco mix 4'],
    ['flexi-martin-geiss-sherwin-maxawow.json', 'Flexi, martin + geiss - sherwin maxawow'],
    ['flexi-martin-geiss-rogue-wave-strike.json', 'Flexi, martin + geiss - rogue wave strike'],
    ['flexi-psychenapping.json', 'Flexi - psychenapping'],
    ['flexi-martin-dive.json', 'Flexi + Martin - dive'],
    ['flexi-reality-tunnel.json', 'Flexi - reality tunnel'],
    ['geiss-reaction-diffusion-2.json', 'Geiss - Reaction Diffusion 2'],
    ['rovastar-geiss-hyperspace-kaleidoscope.json', 'Rovastar + Geiss - Hyperspace'],
    ['aderrasi-potion-of-spirits.json', 'Aderrasi - Potion of Spirits'],
    ['unchained-unified-drag-2.json', 'Unchained - Unified Drag 2'],
    ['zylot-true-visionary-final-mix.json', 'Zylot - True Visionary'],
    ['eos-zylot-skylight-stained-glass.json', 'Eo.S. + Zylot - skylight'],
    ['martin-gate-to-moria.json', 'martin - gate to moria'],
    ['martin-reflections-on-black-tiles.json', 'martin - reflections on black tiles'],
    ['cope-the-drain-to-heaven.json', 'cope - the drain to heaven'],
    ['shifter-liquid-circuitry-conjugoth.json', 'shifter - liquid circuitry'],
].map(([file, label]) => ({id: `md-${file.slice(0, -'.json'.length)}`, file, label}));

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

// A shuffled pass through ids, then another: every preset plays once
// before any repeats. random() returns a number in [0, 1).
export function createCycle(ids, random) {
    let left = ids.slice();
    let pass = [];
    function refill() {
        pass = left.slice();
        for (let i = pass.length - 1; i > 0; i--) {
            const j = Math.floor(random() * (i + 1));
            const swap = pass[i];
            pass[i] = pass[j];
            pass[j] = swap;
        }
    }
    return {
        // Within a pass the one showing is already used; only a new pass
        // can start with it, and then it moves to that pass's end.
        next(current) {
            if (!left.length) {
                return null;
            }
            if (!pass.length) {
                refill();
                if (pass[0] === current && pass.length > 1) {
                    pass.push(pass.shift());
                }
            }
            return pass.shift();
        },
        // A preset that would not load leaves the cycle for good.
        drop(id) {
            left = left.filter((other) => other !== id);
            pass = pass.filter((other) => other !== id);
        },
    };
}

// The glow reads bass against its recent peak, so loud and quiet tracks
// both use the full range; the floor keeps silence from being amplified
// into motion. The same rule as effects.js.
export function followBass(level, peak) {
    const next = Math.max(level, peak * 0.995, 0.2);
    return {glow: (level / next) * 0.35, peak: next};
}
