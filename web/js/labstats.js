// Frame rates from requestAnimationFrame timestamps (ms, ascending).
const WINDOW_MS = 1000;
// Frame timestamps sit on the display's vsync, so one landing exactly a
// second after the window's first frame belongs to it despite rounding.
const SLACK_MS = 0.5;

const tenth = (value) => Math.round(value * 10) / 10;

// avg_fps: frames over the whole span. worst_fps: the fewest frames that
// ended within any one second, from a window starting at each frame, so a
// stall shows even when the average hides it. p5_fps: the rate of the
// 95th-percentile frame time. A span under a second has no full window:
// its worst is its average.
export function frameStats(times) {
    const frames = times.length - 1;
    if (frames < 1 || times[frames] <= times[0]) {
        return {avg_fps: 0, worst_fps: 0, p5_fps: 0, frames: 0};
    }
    const span = times[frames] - times[0];
    const avg = (frames * 1000) / span;
    let worst = Infinity;
    let end = 0;
    for (let start = 0; times[start] + WINDOW_MS <= times[frames] + SLACK_MS; start++) {
        while (end < frames && times[end + 1] <= times[start] + WINDOW_MS + SLACK_MS) {
            end += 1;
        }
        worst = Math.min(worst, end - start);
    }
    const gaps = [];
    for (let i = 1; i <= frames; i++) {
        gaps.push(times[i] - times[i - 1]);
    }
    gaps.sort((a, b) => a - b);
    const slow = gaps[Math.min(frames - 1, Math.ceil(frames * 0.95) - 1)];
    return {
        avg_fps: tenth(avg),
        worst_fps: worst === Infinity ? tenth(avg) : worst,
        p5_fps: tenth(1000 / slow),
        frames,
    };
}

// Seconds per style and mode in the lab's test: 10, or ?labsec= within
// 1 to 30 (a short run is for the browser check).
export const TEST_WINDOW_S = 10;

export function testWindowS(raw) {
    const value = raw === null || raw === '' ? Number.NaN : Number(raw);
    return Number.isFinite(value) ? Math.min(30, Math.max(1, value)) : TEST_WINDOW_S;
}
