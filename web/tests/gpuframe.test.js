import assert from 'node:assert/strict';
import {test} from 'node:test';
import {MAX_SCALE, RENDER_CAP, STEP_FPS, createPace, renderSize, startScale} from '../js/gpuframe.js';

test('renderSize caps the long side at 1280 device pixels, then scales', () => {
    assert.deepEqual(renderSize(876, 579, 1.75, 2), {width: 640, height: 423});
    assert.deepEqual(renderSize(489, 518, 1.75, 2), {width: 428, height: 453});
    assert.deepEqual(renderSize(1372, 686, 1.75, 2), {width: 640, height: 320});
    assert.deepEqual(renderSize(1372, 686, 1.75, 3), {width: 427, height: 213});
    assert.deepEqual(renderSize(876, 579, 1.75, 1), {width: 1280, height: 846});
    assert.equal(RENDER_CAP, 1280);
    assert.deepEqual(renderSize(0, 0, 1, 2), {width: 1, height: 1});
});

test('startScale: the style\'s own, a step smaller in low power, never above the floor, at most a third', () => {
    assert.equal(MAX_SCALE, 3);
    assert.equal(startScale(1, false, 1), 1);
    assert.equal(startScale(1, true, 1), 2);
    assert.equal(startScale(2, false, 1), 2);
    assert.equal(startScale(2, true, 1), 3);
    assert.equal(startScale(3, true, 1), 3);
    assert.equal(startScale(1, false, 2), 2);
    assert.equal(startScale(2, true, 3), 3);
});

// Frames at fps from `from` to `to` ms; the times frame() said slow.
function run(pace, from, to, fps) {
    const slow = [];
    for (let t = from; t <= to; t += 1000 / fps) {
        if (pace.frame(t)) {
            slow.push(t);
        }
    }
    return slow;
}

test('createPace says slow once per three-second window drawn below 50 fps', () => {
    assert.equal(STEP_FPS, 50);
    const slow = run(createPace(), 0, 6100, 40);
    assert.equal(slow.length, 2);
    assert.ok(slow[0] >= 2990 && slow[0] < 3100, String(slow[0]));
});

test('createPace never says slow at 60 fps, nor just above 50', () => {
    assert.deepEqual(run(createPace(), 0, 20000, 60), []);
    assert.deepEqual(run(createPace(), 0, 20000, 52), []);
});

test('a pause between frames starts a new window instead of counting as slow', () => {
    const pace = createPace();
    assert.deepEqual(run(pace, 0, 1000, 60), []);
    // A page in the background for 1.5 s: without the gap rule the window
    // ending near 3 s would average about 34 fps.
    assert.deepEqual(run(pace, 2500, 6000, 60), []);
});

test('reset starts a new window', () => {
    const pace = createPace();
    assert.deepEqual(run(pace, 0, 2900, 40), []);
    pace.reset();
    assert.deepEqual(run(pace, 2925, 5800, 40), []);
});
