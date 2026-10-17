import assert from 'node:assert/strict';
import {test} from 'node:test';
import {STALL_MS, bufferSeconds, createWatchdog, retryDelayMs} from '../js/reconnect.js';

test('retries back off and then stay at a minute', () => {
    assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 20].map(retryDelayMs), [2000, 4000, 8000, 16000, 30000, 60000, 60000, 60000]);
});

test('the watchdog reports a stall when playback time stops moving', () => {
    const watchdog = createWatchdog();
    watchdog.start(0);
    assert.equal(watchdog.observe(0, 1000), 'ok');
    assert.equal(watchdog.observe(1, 2000), 'ok');
    assert.equal(watchdog.observe(1, 2000 + STALL_MS - 1), 'ok');
    assert.equal(watchdog.observe(1, 2000 + STALL_MS), 'stalled');
});

test('a stall right after connecting counts from the start', () => {
    const watchdog = createWatchdog();
    watchdog.start(0);
    assert.equal(watchdog.observe(0, STALL_MS), 'stalled');
});

test('healthy after a minute of advancing playback', () => {
    const watchdog = createWatchdog();
    watchdog.start(0);
    for (let t = 1000; t <= 61000; t += 1000) {
        watchdog.observe(t / 1000, t);
    }
    assert.equal(watchdog.healthy(59000), false);
    assert.equal(watchdog.healthy(61000), true);
});

test('buffer seconds from the buffered ranges, clamped', () => {
    const ranges = (...ends) => ({length: ends.length, end: (i) => ends[i]});
    assert.equal(bufferSeconds(ranges(10, 40.4), 10), 30);
    assert.equal(bufferSeconds(ranges(), 0), 0);
    assert.equal(bufferSeconds(null, 0), 0);
    assert.equal(bufferSeconds(ranges(5), 10), 0);
    assert.equal(bufferSeconds(ranges(500), 0), 120);
});
