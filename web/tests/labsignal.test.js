import assert from 'node:assert/strict';
import {test} from 'node:test';
import {KICK_PERIOD_S, LAB_SECONDS, labSignal} from '../js/labsignal.js';

const RATE = 8000;

function energy(samples, fromS, lengthS) {
    const from = Math.round(fromS * RATE);
    const to = from + Math.round(lengthS * RATE);
    let sum = 0;
    for (let i = from; i < to; i++) {
        sum += samples[i] * samples[i];
    }
    return sum / (to - from);
}

test('labSignal fills the whole loop at the given sample rate', () => {
    assert.equal(labSignal(RATE, 1).length, RATE * LAB_SECONDS);
    assert.equal(labSignal(44100, 1, 2).length, 88200);
});

test('labSignal gives the same samples for the same seed and other noise for another', () => {
    const first = labSignal(RATE, 7);
    assert.deepEqual(labSignal(RATE, 7), first);
    assert.notDeepEqual(labSignal(RATE, 8), first);
});

test('labSignal stays within full scale and is never silent', () => {
    const samples = labSignal(48000, 3);
    let peak = 0;
    for (const sample of samples) {
        assert.ok(Number.isFinite(sample));
        peak = Math.max(peak, Math.abs(sample));
    }
    assert.ok(peak <= 1, `peak ${peak}`);
    assert.ok(peak > 0.5, `peak ${peak}`);
    // The sweep runs under everything, so no stretch is silent.
    for (let s = 0; s < LAB_SECONDS; s += 0.25) {
        assert.ok(energy(samples, s, 0.05) > 0.001, `silent at ${s} s`);
    }
});

test('labSignal kicks every half second and the kick dies down in between', () => {
    const samples = labSignal(RATE, 1);
    assert.equal(KICK_PERIOD_S, 0.5);
    for (let k = 0; k < LAB_SECONDS / KICK_PERIOD_S; k++) {
        const onset = k * KICK_PERIOD_S;
        const kick = energy(samples, onset, 0.03);
        const between = energy(samples, onset + 0.38, 0.03);
        assert.ok(kick > 3 * between, `kick ${k}: ${kick} vs ${between}`);
    }
});
