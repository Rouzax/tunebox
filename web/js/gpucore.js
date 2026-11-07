// The GPU test's plan and the pure arithmetic it runs on: which cells it
// measures in what order, how long that takes, the analyser's bands, the
// spectrum texture's bins and the longest frame after a preset switch.

// Each probe shader stands for a class of possible visualizer styles.
export const PROBES = [
    {id: 'light', label: 'Plasma (light)'},
    {id: 'cover', label: 'Tunnel (cover)'},
    {id: 'heavy', label: 'Noise (heavy)'},
];

// From light to heavy: no custom shaders; two typical presets; many
// shape instances; long ray-marched warp and composite shaders.
export const PRESETS = [
    {id: 'hur', file: 'geiss-hurricane.json', label: 'Geiss - Hurricane'},
    {id: 'mbl', file: 'flexi-mindblob-shiny-mix.json', label: 'Flexi - mindblob [shiny mix]'},
    {id: 'dsc', file: 'martin-disco-mix-4.json', label: 'martin - disco mix 4'},
    {id: 'saw', file: 'sawtooth-grin-roam.json', label: 'sawtooth grin roam'},
    {id: 'cas', file: 'martin-castle-in-the-air.json', label: 'martin - castle in the air'},
];

// Render scales as divisors of the canvas's device pixels: 1, 1/2, 1/3.
// Butterchurn at full size is not measured: the player would never run a
// Milkdrop preset at full resolution on the car's screen.
export const PROBE_SCALES = [1, 2, 3];
export const PRESET_SCALES = [2, 3];
// How long frames are watched after a preset is asked for, beyond the
// moment it has loaded: shader compiles can land on the first draws.
export const SWITCH_WATCH_S = 2;

// Off first, as the baseline every other cell is read against. A preset's
// first cell carries `load`, the switch whose cost is measured.
export function gpuPlan() {
    const plan = [{kind: 'off', id: 'off', label: 'Off', scale: 1}];
    for (const probe of PROBES) {
        for (const scale of PROBE_SCALES) {
            plan.push({kind: 'probe', id: probe.id, label: probe.label, scale});
        }
    }
    for (const preset of PRESETS) {
        PRESET_SCALES.forEach((scale, i) => {
            plan.push({kind: 'preset', id: preset.id, label: preset.label, file: preset.file, scale, load: i === 0});
        });
    }
    return plan;
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
