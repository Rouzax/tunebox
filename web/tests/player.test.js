import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createEmitter} from '../js/events.js';
import {createPlayer} from '../js/player.js';

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
            volume: 1,
            muted: false,
            crossOrigin: null,
            error: null,
            plays: 0,
            playResult: null,
            play() {
                audio.plays += 1;
                return audio.playResult || Promise.resolve();
            },
            load() {},
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

function harness() {
    const timers = fakeTimers();
    const events = createEmitter();
    const made = [];
    const log = [];
    for (const name of ['state', 'audio', 'webaudio-unavailable', 'autoplay-refused']) {
        events.on(name, (value) => log.push(name === 'state' ? value : name));
    }
    const player = createPlayer({events, makeAudio: fakeAudio(made), timers, now: timers.now});
    return {timers, made, log, player};
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

test('stop clears the stream and nothing retries afterwards', () => {
    const {timers, made, player} = harness();
    player.play('https://example.invalid/a.aac');
    made[0].fire('playing');
    player.stop();
    assert.equal(made[0].src, '');
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
