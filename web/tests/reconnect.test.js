import assert from 'node:assert/strict';
import {test} from 'node:test';
import {STALL_MS, createWatchdog, keepAudioRunning, lagSeconds, retryDelayMs} from '../js/reconnect.js';

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

test('lag is the time since the stream was asked for, minus the time played, clamped', () => {
    assert.equal(lagSeconds(12400, 10), 2);
    assert.equal(lagSeconds(0, 0), 0);
    assert.equal(lagSeconds(5000, 10), 0);
    assert.equal(lagSeconds(500000, 0), 120);
});

test('a suspended or interrupted audio context is resumed while playback is wanted', async () => {
    let wanted = true;
    const context = {
        state: 'running',
        resumes: 0,
        resume() {
            this.resumes += 1;
            return Promise.reject(new Error('not allowed yet'));
        },
    };
    const resume = keepAudioRunning(context, () => wanted);
    for (const state of ['suspended', 'interrupted']) {
        context.state = state;
        context.onstatechange();
    }
    assert.equal(context.resumes, 2);
    context.state = 'running';
    context.onstatechange();
    wanted = false;
    context.state = 'suspended';
    resume();
    assert.equal(context.resumes, 2);
    context.state = 'closed';
    wanted = true;
    resume();
    assert.equal(context.resumes, 2);
    // A rejected resume() must not surface as an unhandled rejection.
    await new Promise((done) => setImmediate(done));
});
