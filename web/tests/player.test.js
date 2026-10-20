import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createEmitter} from '../js/events.js';
import {createPlayer, pageVolume, resumesAfterPause} from '../js/player.js';

function fakeTimers() {
    let now = 0;
    let id = 0;
    const pending = new Map();
    return {
        now: () => now,
        setTimeout(fn, ms) {
            id += 1;
            pending.set(id, {fn, at: now + ms});
            return id;
        },
        clearTimeout(timer) {
            pending.delete(timer);
        },
        setInterval(fn, ms) {
            id += 1;
            pending.set(id, {fn, at: now + ms, every: ms});
            return id;
        },
        clearInterval(timer) {
            pending.delete(timer);
        },
        advance(ms) {
            const end = now + ms;
            for (;;) {
                let next = null;
                for (const entry of pending) {
                    if (entry[1].at <= end && (!next || entry[1].at < next[1].at)) {
                        next = entry;
                    }
                }
                if (!next) {
                    break;
                }
                now = next[1].at;
                if (next[1].every) {
                    next[1].at += next[1].every;
                } else {
                    pending.delete(next[0]);
                }
                next[1].fn();
            }
            now = end;
        },
    };
}

function fakeAudio(made) {
    return () => {
        const audio = new EventTarget();
        let src = '';
        // Like a real element, a new source starts playback time from zero.
        Object.defineProperty(audio, 'src', {
            get: () => src,
            set: (value) => {
                src = value;
                audio.currentTime = 0;
            },
        });
        Object.assign(audio, {
            currentTime: 0,
            paused: true,
            ended: false,
            volume: 1,
            muted: false,
            crossOrigin: null,
            error: null,
            plays: 0,
            playResult: null,
            play() {
                audio.paused = false;
                audio.plays += 1;
                return audio.playResult || Promise.resolve();
            },
            load() {},
            pause() {
                audio.paused = true;
                audio.fire('pause');
            },
            removeAttribute(name) {
                if (name === 'src') {
                    audio.src = '';
                }
            },
            fire(type) {
                audio.dispatchEvent(new Event(type));
            },
        });
        made.push(audio);
        return audio;
    };
}

function harness(options) {
    const timers = fakeTimers();
    const events = createEmitter();
    const made = [];
    const log = [];
    for (const name of ['state', 'audio', 'webaudio-unavailable', 'autoplay-refused', 'interrupted']) {
        events.on(name, (value) => log.push(name === 'state' ? value : name));
    }
    events.on('debug', (note) => log.push(`debug: ${note}`));
    const player = createPlayer(Object.assign({events, makeAudio: fakeAudio(made), timers, now: timers.now}, options));
    const refusals = [];
    events.on('autoplay-refused', (value) => refusals.push(value));
    return {timers, made, log, player, refusals};
}

test('play loads, then plays when audio flows', () => {
    const {made, log, player} = harness();
    player.play('https://example.invalid/a.aac');
    assert.equal(made[0].crossOrigin, 'anonymous');
    assert.equal(made[0].src, 'https://example.invalid/a.aac');
    made[0].fire('playing');
    assert.deepEqual(log, ['loading', 'audio', 'playing']);
});

test('the buffer is how far playback runs behind the request for the stream', () => {
    const {timers, made, player} = harness();
    player.play('https://example.invalid/a.aac');
    timers.advance(1500);
    made[0].fire('playing');
    for (let i = 0; i < 10; i++) {
        timers.advance(1000);
        made[0].currentTime += 1;
    }
    assert.equal(player.bufferS(), 2);
});

test('a reconnect measures the buffer from the new request', () => {
    const {timers, made, player} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    for (let i = 0; i < 30; i++) {
        timers.advance(1000);
        made[0].currentTime += 1;
    }
    made[0].fire('error');
    // The retry asks for the stream again 2 s later; it plays 2 s after that.
    timers.advance(4000);
    made[0].fire('playing');
    for (let i = 0; i < 5; i++) {
        timers.advance(1000);
        made[0].currentTime += 1;
    }
    assert.equal(player.bufferS(), 2);
});

test('a stalled stream reconnects with growing pauses', () => {
    const {timers, made, player} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    made[0].currentTime = 1;
    timers.advance(1000);
    timers.advance(8000);
    assert.equal(player.state(), 'reconnecting');
    assert.equal(made[0].plays, 1);
    timers.advance(2000);
    assert.equal(made[0].plays, 2);
    timers.advance(8000);
    assert.equal(player.state(), 'reconnecting');
    timers.advance(3999);
    assert.equal(made[0].plays, 2);
    timers.advance(1);
    assert.equal(made[0].plays, 3);
});

test('a slow start that is still receiving audio is not cut off', () => {
    const {timers, made, player} = harness();
    player.play('https://example.invalid/a.aac');
    for (let i = 0; i < 15; i++) {
        timers.advance(1000);
        made[0].fire('progress');
    }
    assert.equal(player.state(), 'loading');
    assert.equal(made[0].plays, 1);
    made[0].fire('playing');
    assert.equal(player.state(), 'playing');
});

test('reconnect decisions are reported for the debug panel', () => {
    const {timers, made, player, log} = harness();
    player.play('https://example.invalid/a.aac');
    timers.advance(9000);
    made[0].error = {code: 2};
    timers.advance(2000);
    made[0].fire('error');
    const notes = log.filter((entry) => entry.startsWith('debug:'));
    assert.deepEqual(notes, ['debug: stalled, reconnecting in 2 s', 'debug: media error 2, reconnecting in 4 s']);
});

test('coming back online reconnects at once and drops the pending retry', () => {
    const {timers, made, player} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].error = {code: 2};
    made[0].fire('error');
    assert.equal(player.state(), 'reconnecting');
    player.online();
    assert.equal(made[0].plays, 2);
    timers.advance(1999);
    assert.equal(made[0].plays, 2);
    made[0].fire('playing');
    assert.equal(player.state(), 'playing');
});

test('a stream refused under CORS is retried without Web Audio, once', () => {
    const {made, log, player} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].error = {code: 4};
    made[0].fire('error');
    assert.equal(made.length, 2);
    assert.equal(made[1].crossOrigin, null);
    assert.ok(log.includes('webaudio-unavailable'));
    made[1].error = {code: 4};
    made[1].fire('error');
    assert.equal(made.length, 2);
    assert.equal(player.state(), 'reconnecting');
});

// Browsers also report an HTTP error on a new source (a 503 while a server
// restarts) as code 4. A stream that has played is not unplayable.
test('a stream that played and then fails with code 4 is retried, not given up', () => {
    const {timers, made, log, player} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    made[0].error = {code: 4};
    made[0].fire('error');
    assert.equal(player.state(), 'reconnecting');
    assert.equal(made.length, 1);
    assert.ok(!log.includes('webaudio-unavailable'));
    timers.advance(2000);
    assert.equal(made[0].plays, 2);
});

test('a refused autoplay stops quietly', async () => {
    const {made, log, player} = harness();
    const refusal = Object.assign(new Error('no'), {name: 'NotAllowedError'});
    const make = made;
    player.play('https://example.invalid/a.aac');
    // The first element exists now; make its next play() refuse.
    make[0].playResult = Promise.reject(refusal);
    player.play('https://example.invalid/a.aac');
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(player.state(), 'stopped');
    assert.ok(log.includes('autoplay-refused'));
});

test('a refused restart during a reconnect is reported as one', async () => {
    const {timers, made, player, refusals} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    made[0].playResult = Promise.reject(Object.assign(new Error('no'), {name: 'NotAllowedError'}));
    made[0].fire('error');
    timers.advance(2000);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(player.state(), 'stopped');
    assert.deepEqual(refusals, [{resumed: true}]);
});

test('elsewhere a pause from outside stops the player instead of being undone', () => {
    const {timers, made, player, log} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    made[0].currentTime = 3;
    // A phone call, or headphones unplugged: resuming would play aloud.
    made[0].paused = true;
    made[0].fire('pause');
    assert.equal(player.state(), 'stopped');
    assert.ok(log.includes('interrupted'));
    timers.advance(20000);
    assert.equal(made[0].plays, 1);
});

test('only the car kind of browser resumes after a pause from outside', () => {
    const car = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
    const android = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36';
    const iphone = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';
    const mac = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
    assert.equal(resumesAfterPause(car), true);
    assert.equal(resumesAfterPause(android), false);
    assert.equal(resumesAfterPause(iphone), false);
    assert.equal(resumesAfterPause(mac), false);
});

// The system takes the audio from outside: the element pauses.
function pauseFromOutside(audio) {
    audio.paused = true;
    audio.fire('pause');
}

// Audio that flows: the watchdog sees time move.
function flow(timers, audio, ms) {
    for (let i = 0; i < ms / 1000; i++) {
        audio.currentTime += 1;
        timers.advance(1000);
    }
}

test('a brief pause from outside resumes at live by itself', () => {
    const {timers, made, player, log} = harness({resumeAfterPause: true});
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    flow(timers, made[0], 3000);
    // A phone connecting to the car takes the audio for a moment.
    pauseFromOutside(made[0]);
    assert.equal(player.state(), 'reconnecting');
    assert.equal(made[0].src, '');
    timers.advance(2000);
    assert.equal(made[0].plays, 2);
    assert.equal(made[0].src, 'https://example.invalid/a.aac');
    made[0].fire('playing');
    assert.equal(player.state(), 'playing');
    assert.ok(!log.includes('interrupted'));
});

test('a pause soon after resuming means the audio is held: it stops', () => {
    const {timers, made, player, log} = harness({resumeAfterPause: true});
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    flow(timers, made[0], 3000);
    // A phone call: the system pauses again right after the resume.
    pauseFromOutside(made[0]);
    timers.advance(2000);
    made[0].fire('playing');
    flow(timers, made[0], 1000);
    pauseFromOutside(made[0]);
    assert.equal(player.state(), 'stopped');
    assert.ok(log.includes('interrupted'));
    timers.advance(60000);
    assert.equal(made[0].plays, 2);
});

test('resumes that never play give up after the last try', () => {
    const {timers, made, player, log} = harness({resumeAfterPause: true});
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    flow(timers, made[0], 3000);
    pauseFromOutside(made[0]);
    for (const wait of [2000, 6000, 15000]) {
        timers.advance(wait);
        pauseFromOutside(made[0]);
    }
    assert.equal(made[0].plays, 4);
    assert.equal(player.state(), 'stopped');
    assert.ok(log.includes('interrupted'));
    timers.advance(60000);
    assert.equal(made[0].plays, 4);
});

test('a pause long after a resume gets its own tries', () => {
    const {timers, made, player, log} = harness({resumeAfterPause: true});
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    flow(timers, made[0], 3000);
    pauseFromOutside(made[0]);
    timers.advance(2000);
    made[0].fire('playing');
    flow(timers, made[0], 60000);
    pauseFromOutside(made[0]);
    assert.equal(player.state(), 'reconnecting');
    assert.ok(!log.includes('interrupted'));
});

test('stop during the wait cancels the resume', () => {
    const {timers, made, player} = harness({resumeAfterPause: true});
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    flow(timers, made[0], 3000);
    pauseFromOutside(made[0]);
    player.stop();
    timers.advance(60000);
    assert.equal(made[0].plays, 1);
    assert.equal(player.state(), 'stopped');
});

test('the pause at the end of a stream is not an interruption', () => {
    const {made, player, log} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    made[0].paused = true;
    made[0].ended = true;
    made[0].fire('pause');
    made[0].fire('ended');
    assert.equal(player.state(), 'reconnecting');
    assert.ok(!log.includes('interrupted'));
});

// Chromium takes a page without a media source off the system's media
// controls, after which the car's play key no longer reaches it.
test('stop pauses the stream instead of dropping it, and play starts again at live', () => {
    const {made, player, log} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    made[0].currentTime = 30;
    player.stop();
    assert.equal(made[0].paused, true);
    assert.equal(made[0].src, 'https://example.invalid/a.aac');
    assert.ok(!log.includes('interrupted'));
    player.play('https://example.invalid/a.aac');
    assert.equal(made.length, 1);
    assert.equal(made[0].currentTime, 0);
    assert.equal(made[0].plays, 2);
});

test('stop ends playback and nothing retries afterwards', () => {
    const {timers, made, player} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    player.stop();
    assert.equal(made[0].paused, true);
    assert.equal(player.state(), 'stopped');
    timers.advance(120000);
    assert.equal(made[0].plays, 1);
    assert.equal(player.bufferS(), 0);
});

test('volume and mute reach the element', () => {
    const {made, player} = harness();
    player.setVolume(0.3);
    player.setMuted(true);
    player.play('https://example.invalid/a.aac');
    assert.equal(made[0].volume, 0.3);
    assert.equal(made[0].muted, true);
});

test('the page level applies only while its slider is shown', () => {
    assert.equal(pageVolume(30, true), 0.3);
    // Without the slider nothing on the page could raise a lowered level;
    // the device's own volume is the control there.
    assert.equal(pageVolume(30, false), 1);
    assert.equal(pageVolume(50, false), 1);
});
