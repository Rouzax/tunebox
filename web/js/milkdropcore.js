// The player's MilkDrop, the pure part: the presets that passed the car's
// GPU test (smooth at half size, no long stall on load), the order Shuffle
// plays them in and the bass the cover glow follows.

export const PRESET_DIR = 'vendor/butterchurn-presets-3.0.0-beta.4/';
export const SHUFFLE_ID = 'md-shuffle';
// A new preset every 45 s, blended over Butterchurn's usual 2.7 s.
export const SHUFFLE_S = 45;
export const BLEND_S = 2.7;

// The authors' names are in the file names and in web/js/gpucore.js; the
// player shows a short name for what the preset looks like. Geiss's
// Reaction Diffusion 2 passed the car's test but stays nearly black, so it
// is left out here (the GPU test still measures it).
export const PRESETS = [
    ['geiss-hurricane.json', 'Hurricane'],
    ['flexi-mindblob-shiny-mix.json', 'Neon Tendrils'],
    ['martin-disco-mix-4.json', 'Light Streaks'],
    ['flexi-martin-geiss-sherwin-maxawow.json', 'Snakeskin'],
    ['flexi-martin-geiss-rogue-wave-strike.json', 'Painted Wave'],
    ['flexi-psychenapping.json', 'Pink Lace'],
    ['flexi-martin-dive.json', 'Marble Dive'],
    ['flexi-reality-tunnel.json', 'Nebula'],
    ['rovastar-geiss-hyperspace-kaleidoscope.json', 'Kaleidoscope'],
    ['aderrasi-potion-of-spirits.json', 'Fire Mirror'],
    ['unchained-unified-drag-2.json', 'Crystal Orb'],
    ['zylot-true-visionary-final-mix.json', 'Mandala'],
    ['eos-zylot-skylight-stained-glass.json', 'Spiral Glass'],
    ['martin-gate-to-moria.json', 'Stone Gate'],
    ['martin-reflections-on-black-tiles.json', 'Night Tiles'],
    ['cope-the-drain-to-heaven.json', 'Light Burst'],
    ['shifter-liquid-circuitry-conjugoth.json', 'Acid Circuit'],
].map(([file, label]) => ({id: `md-${file.slice(0, -'.json'.length)}`, file, label}));

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
