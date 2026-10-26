import assert from 'node:assert/strict';
import {test} from 'node:test';

import {createFlip} from '../js/flip.js';

test('a tap turns to the photo and back', () => {
    const flip = createFlip();
    assert.deepEqual(flip.song('/covers/p.jpg'), {photo: '/covers/p.jpg', flipped: false, canFlip: true});
    assert.equal(flip.tap().flipped, true);
    assert.equal(flip.tap().flipped, false);
});

test('without a photo a tap does nothing', () => {
    const flip = createFlip();
    assert.deepEqual(flip.song(null), {photo: null, flipped: false, canFlip: false});
    assert.equal(flip.tap().flipped, false);
    assert.equal(flip.song(undefined).canFlip, false);
});

test('a new song starts on its cover', () => {
    const flip = createFlip();
    flip.song('/covers/p.jpg');
    flip.tap();
    assert.deepEqual(flip.song('/covers/q.jpg'), {photo: '/covers/q.jpg', flipped: false, canFlip: true});
});

test('a photo arriving late can be turned to; one going away turns back', () => {
    const flip = createFlip();
    flip.song(null);
    assert.equal(flip.photo('/covers/p.jpg').canFlip, true);
    assert.equal(flip.tap().flipped, true);
    assert.equal(flip.photo('/covers/p2.jpg').flipped, true);
    assert.deepEqual(flip.photo(null), {photo: null, flipped: false, canFlip: false});
});
