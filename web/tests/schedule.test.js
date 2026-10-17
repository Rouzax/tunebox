import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createSwitcher} from '../js/schedule.js';

const A = {artist: 'A', title: 'One', started_at: 1};
const B = {artist: 'B', title: 'Two', started_at: 2};

function harness() {
    let clock = 0;
    let timers = [];
    const shown = [];
    const switcher = createSwitcher({
        show: (track) => shown.push(track ? track.title : 'station'),
        now: () => clock,
        setTimer: (fn, ms) => {
            const timer = {fn, at: clock + ms};
            timers.push(timer);
            return timer;
        },
        clearTimer: (timer) => {
            timers = timers.filter((t) => t !== timer);
        },
    });
    const advance = (ms) => {
        clock += ms;
        for (const timer of timers.filter((t) => t.at <= clock)) {
            timers = timers.filter((t) => t !== timer);
            timer.fn();
        }
    };
    return {switcher, shown, advance};
}

test('shows current, then switches to upcoming on time', () => {
    const {switcher, shown, advance} = harness();
    switcher.update({current: A, upcoming: {track: B, in_s: 5}});
    assert.deepEqual(shown, ['One']);
    advance(4999);
    assert.deepEqual(shown, ['One']);
    advance(1);
    assert.deepEqual(shown, ['One', 'Two']);
});

test('a poll that still says the old track right after the switch does not flip back', () => {
    const {switcher, shown, advance} = harness();
    switcher.update({current: A, upcoming: {track: B, in_s: 5}});
    advance(5000);
    advance(200);
    switcher.update({current: A, upcoming: {track: B, in_s: 0}});
    assert.deepEqual(shown, ['One', 'Two']);
    switcher.update({current: B, upcoming: null});
    assert.deepEqual(shown, ['One', 'Two', 'Two']);
});

test('an upcoming station ID switches to the channel name', () => {
    const {switcher, shown, advance} = harness();
    switcher.update({current: A, upcoming: {track: null, in_s: 1}});
    advance(1000);
    assert.deepEqual(shown, ['One', 'station']);
});

test('a newer answer replaces the pending switch', () => {
    const {switcher, shown, advance} = harness();
    switcher.update({current: A, upcoming: {track: B, in_s: 5}});
    switcher.update({current: A, upcoming: null});
    advance(10000);
    assert.deepEqual(shown, ['One', 'One']);
});

test('reset drops a pending switch', () => {
    const {switcher, shown, advance} = harness();
    switcher.update({current: A, upcoming: {track: B, in_s: 5}});
    switcher.reset();
    advance(10000);
    assert.deepEqual(shown, ['One']);
});
