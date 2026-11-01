import assert from 'node:assert/strict';
import {test} from 'node:test';
import {TEST_WINDOW_S, frameStats, testWindowS} from '../js/labstats.js';

// Frame timestamps in ms, one every `step` ms from `from` for `count` frames.
function steady(from, step, count) {
    return Array.from({length: count}, (_, i) => from + i * step);
}

test('frameStats of a steady 60 fps run', () => {
    const stats = frameStats(steady(1000, 1000 / 60, 601));
    assert.equal(stats.frames, 600);
    assert.equal(stats.avg_fps, 60);
    assert.equal(stats.worst_fps, 60);
    assert.equal(stats.p5_fps, 60);
});

test('frameStats takes worst as the slowest one-second window', () => {
    // Two seconds at 60 fps, one second at 20 fps, two at 60 again.
    const times = steady(0, 1000 / 60, 120);
    times.push(...steady(2000, 50, 20));
    times.push(...steady(3000, 1000 / 60, 121));
    const stats = frameStats(times);
    assert.equal(stats.worst_fps, 20);
    assert.ok(stats.avg_fps > 45 && stats.avg_fps < 55, `avg ${stats.avg_fps}`);
    assert.equal(stats.p5_fps, 20);
});

test('frameStats counts a long stall as a window with no frames', () => {
    const times = steady(0, 1000 / 60, 61);
    times.push(2500, 2516);
    assert.equal(frameStats(times).worst_fps, 0);
});

test('frameStats of a run shorter than a second uses the average as worst', () => {
    const stats = frameStats(steady(0, 20, 26));
    assert.equal(stats.avg_fps, 50);
    assert.equal(stats.worst_fps, 50);
});

test('frameStats of too few frames is all zero', () => {
    for (const times of [[], [5]]) {
        assert.deepEqual(frameStats(times), {avg_fps: 0, worst_fps: 0, p5_fps: 0, frames: 0});
    }
});

test('testWindowS takes ?labsec= within its range and 10 s otherwise', () => {
    assert.equal(TEST_WINDOW_S, 10);
    assert.equal(testWindowS(null), 10);
    assert.equal(testWindowS(''), 10);
    assert.equal(testWindowS('soon'), 10);
    assert.equal(testWindowS('1'), 1);
    assert.equal(testWindowS('2.5'), 2.5);
    assert.equal(testWindowS('0'), 1);
    assert.equal(testWindowS('-4'), 1);
    assert.equal(testWindowS('600'), 30);
});
