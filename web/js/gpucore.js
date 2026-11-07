// The GPU test's plan and the pure arithmetic it runs on: which cells it
// measures in what order, how long that takes, the analyser's bands, the
// spectrum texture's bins, the longest frame after a preset switch and the
// rule a preset must pass to ship.

// Each probe shader stands for a class of possible visualizer styles.
export const PROBES = [
    {id: 'light', label: 'Plasma (light)'},
    {id: 'cover', label: 'Tunnel (cover)'},
    {id: 'heavy', label: 'Noise (heavy)'},
];

// Candidates for the player's MilkDrop group, across authors and looks,
// picked for few shape instances and short warp and composite shaders:
// hundreds of shape instances or a long warp shader are what dropped a
// preset to a few frames per second, or froze the car's screen while it
// loaded. The first three already passed in the car. Each line names the
// preset's original file in butterchurn-presets.
export const PRESETS = [
    {id: 'hu', file: 'geiss-hurricane.json', label: 'Geiss - Hurricane'}, // Geiss - Hurricane
    {id: 'mb', file: 'flexi-mindblob-shiny-mix.json', label: 'Flexi - mindblob [shiny mix]'}, // Flexi - mindblob [shiny mix]
    {id: 'dm', file: 'martin-disco-mix-4.json', label: 'martin - disco mix 4'}, // martin - disco mix 4
    {id: 'sm', file: 'flexi-martin-geiss-sherwin-maxawow.json', label: 'Flexi, martin + geiss - sherwin maxawow'}, // Flexi, martin + geiss - dedicated to the sherwin maxawow
    {id: 'rw', file: 'flexi-martin-geiss-rogue-wave-strike.json', label: 'Flexi, martin + geiss - rogue wave strike'}, // Flexi, martin + geiss - painterly rogue wave strike
    {id: 'ps', file: 'flexi-psychenapping.json', label: 'Flexi - psychenapping'}, // Flexi - psychenapping
    {id: 'dv', file: 'flexi-martin-dive.json', label: 'Flexi + Martin - dive'}, // Flexi + Martin - dive
    {id: 'rt', file: 'flexi-reality-tunnel.json', label: 'Flexi - reality tunnel'}, // Flexi - reality tunnel
    {id: 'rd', file: 'geiss-reaction-diffusion-2.json', label: 'Geiss - Reaction Diffusion 2'}, // Geiss - Reaction Diffusion 2
    {id: 'hk', file: 'rovastar-geiss-hyperspace-kaleidoscope.json', label: 'Rovastar + Geiss - Hyperspace kaleidoscope'}, // Rovastar + Geiss - Hyperspace - kaleidoscope
    {id: 'po', file: 'aderrasi-potion-of-spirits.json', label: 'Aderrasi - Potion of Spirits'}, // Aderrasi - Potion of Spirits
    {id: 'ud', file: 'unchained-unified-drag-2.json', label: 'Unchained - Unified Drag 2'}, // Unchained - Unified Drag 2
    {id: 'tv', file: 'zylot-true-visionary-final-mix.json', label: 'Zylot - True Visionary'}, // Zylot - True Visionary (Final Mix)
    {id: 'sg', file: 'eos-zylot-skylight-stained-glass.json', label: 'Eo.S. + Zylot - skylight'}, // Eo.S. + Zylot - skylight (Stained Glass Majesty mix)
    {id: 'gm', file: 'martin-gate-to-moria.json', label: 'martin - gate to moria'}, // martin - gate to moria
    {id: 'bt', file: 'martin-reflections-on-black-tiles.json', label: 'martin - reflections on black tiles'}, // martin - reflections on black tiles
    {id: 'dh', file: 'cope-the-drain-to-heaven.json', label: 'cope - the drain to heaven'}, // cope - the drain to heaven
    {id: 'lc', file: 'shifter-liquid-circuitry-conjugoth.json', label: 'shifter - liquid circuitry'}, // shifter - liquid circuitry - conjugoth
];

// Render scales as divisors of the canvas's device pixels: 1, 1/2, 1/3.
// Presets run at half size only: the player draws MilkDrop at half size,
// and a preset smooth there was smooth at 1/3 too in the car.
export const PROBE_SCALES = [1, 2, 3];
export const PRESET_SCALES = [2];
// How long frames are watched after a preset is asked for, beyond the
// moment it has loaded: shader compiles can land on the first draws.
export const SWITCH_WATCH_S = 2;

// Off first, as the baseline every other cell is read against. Each
// preset has one cell, which measures its load as well.
export function gpuPlan() {
    const plan = [{kind: 'off', id: 'off', label: 'Off', scale: 1}];
    for (const probe of PROBES) {
        for (const scale of PROBE_SCALES) {
            plan.push({kind: 'probe', id: probe.id, label: probe.label, scale});
        }
    }
    for (const preset of PRESETS) {
        plan.push({kind: 'preset', id: preset.id, label: preset.label, file: preset.file, scale: PRESET_SCALES[0], load: true});
    }
    return plan;
}

// What a preset must manage in the car to ship in the player. stallMs
// bounds the longest frame around its load: a long load freezes whatever
// is on screen, a blend from the preset before included.
export const PASS = {avg: 55, worst: 45, stallMs: 100};
// Milliseconds are written whole and capped, keeping the QR code small;
// 999 reads "999 or more", a fail under PASS.
export const MS_CAP = 999;

export function capMs(ms) {
    return typeof ms === 'number' && Number.isFinite(ms) ? Math.min(MS_CAP, Math.round(ms)) : null;
}

// misses names each part of PASS the preset fell short of.
export function presetVerdict(stats, stallMs) {
    const misses = [];
    if (!(stats.avg_fps >= PASS.avg)) {
        misses.push('average');
    }
    if (!(stats.worst_fps >= PASS.worst)) {
        misses.push('worst');
    }
    if (!(typeof stallMs === 'number' && stallMs <= PASS.stallMs)) {
        misses.push('load');
    }
    return {pass: misses.length === 0, misses};
}

// Whole seconds the plan takes at windowS per cell, leaving out the load
// times themselves, which only the device knows.
export function planSeconds(plan, windowS) {
    const loads = plan.filter((cell) => cell.load).length;
    return Math.round(plan.length * windowS + loads * SWITCH_WATCH_S);
}

// "about 3 min 30 s", for the note shown when a run starts.
export function describeSeconds(seconds) {
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    if (!minutes) {
        return `about ${rest} s`;
    }
    return rest ? `about ${minutes} min ${rest} s` : `about ${minutes} min`;
}

const BAND_EDGES_HZ = [20, 250, 2000, 8000];

// Bass, mids and highs in 0..1: the mean of the byte bins (0..255) whose
// centre falls in each band.
export function bands(bins, sampleRate, fftSize) {
    const hzPerBin = sampleRate / fftSize;
    const out = [];
    for (let band = 0; band < 3; band++) {
        const from = Math.max(0, Math.ceil(BAND_EDGES_HZ[band] / hzPerBin));
        const to = Math.min(bins.length, Math.ceil(BAND_EDGES_HZ[band + 1] / hzPerBin));
        let sum = 0;
        for (let i = from; i < to; i++) {
            sum += bins[i];
        }
        out.push(to > from ? sum / ((to - from) * 255) : 0);
    }
    return {bass: out[0], mid: out[1], high: out[2]};
}

// Folds the analyser's bins into out's length, keeping each group's
// loudest bin, so a narrow peak survives the smaller texture.
export function shrink(bins, out) {
    const group = bins.length / out.length;
    for (let i = 0; i < out.length; i++) {
        let peak = 0;
        const end = Math.min(bins.length, Math.round((i + 1) * group));
        for (let j = Math.round(i * group); j < end; j++) {
            peak = Math.max(peak, bins[j]);
        }
        out[i] = peak;
    }
    return out;
}

// The longest gap between frames (ms), 0 with fewer than two frames.
export function worstGapMs(times) {
    let worst = 0;
    for (let i = 1; i < times.length; i++) {
        worst = Math.max(worst, times[i] - times[i - 1]);
    }
    return worst;
}
