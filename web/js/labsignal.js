// The lab's test signal: a slow sweep across the analysers' range, short
// noise bursts and a kick on every beat, so every band and every kind of
// motion shows. The noise comes from a seeded generator, so every run of
// the frame-rate test draws the same frames.

export const LAB_SECONDS = 20;
export const KICK_PERIOD_S = 0.5;

const SWEEP_FROM_HZ = 30;
const SWEEP_TO_HZ = 16000;
const SWEEP_LEVEL = 0.2;
// Bursts sit between kicks, where the previous kick has died down.
const BURST_PERIOD_S = 2;
const BURST_AT_S = 1.25;
const BURST_LENGTH_S = 0.08;
const BURST_LEVEL = 0.25;
const KICK_LEVEL = 0.6;
const KICK_LENGTH_S = 0.25;
const KICK_DECAY_S = 0.06;
const KICK_FROM_HZ = 120;
const KICK_TO_HZ = 45;

// mulberry32: small, fast, and the same sequence on every engine.
function random(seed) {
    let state = seed >>> 0;
    return () => {
        state = (state + 0x6d2b79f5) >>> 0;
        let t = state;
        t = Math.imul(t ^ (t >>> 15), t | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

// Mono samples in [-1, 1] for `seconds` at `rate` samples per second.
export function labSignal(rate, seed, seconds = LAB_SECONDS) {
    const length = Math.round(rate * seconds);
    const samples = new Float32Array(length);
    const next = random(seed);
    // A logarithmic sweep's phase is the integral of f0 * k^(t/T).
    const ratio = Math.log(SWEEP_TO_HZ / SWEEP_FROM_HZ);
    const sweepScale = (2 * Math.PI * SWEEP_FROM_HZ * seconds) / ratio;
    const kickLength = Math.round(KICK_LENGTH_S * rate);
    const kickPeriod = Math.round(KICK_PERIOD_S * rate);
    for (let i = 0; i < length; i++) {
        const t = i / rate;
        let sample = SWEEP_LEVEL * Math.sin(sweepScale * (Math.exp((ratio * t) / seconds) - 1));
        const inKick = i % kickPeriod;
        if (inKick < kickLength) {
            const k = inKick / rate;
            // The pitch falls from KICK_FROM_HZ toward KICK_TO_HZ as it decays.
            const phase = 2 * Math.PI * (KICK_TO_HZ * k + (KICK_FROM_HZ - KICK_TO_HZ) * KICK_DECAY_S * (1 - Math.exp(-k / KICK_DECAY_S)));
            sample += KICK_LEVEL * Math.exp(-k / KICK_DECAY_S) * Math.sin(phase);
        }
        const inBurst = (t - BURST_AT_S) % BURST_PERIOD_S;
        if (t >= BURST_AT_S && inBurst < BURST_LENGTH_S) {
            // A short fade at both ends keeps the burst from clicking.
            const fade = Math.min(1, inBurst / 0.005, (BURST_LENGTH_S - inBurst) / 0.005);
            sample += BURST_LEVEL * fade * (next() * 2 - 1);
        }
        samples[i] = Math.max(-1, Math.min(1, sample));
    }
    return samples;
}
