import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createStorage} from '../js/storage.js';

function memory() {
    const data = new Map();
    return {getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, v), data};
}

const blocked = {
    getItem() {
        throw new Error('SecurityError');
    },
    setItem() {
        throw new Error('QuotaExceededError');
    },
};

test('values are kept under tunebox. keys', () => {
    const backend = memory();
    const storage = createStorage(backend);
    storage.set('volume', 70);
    assert.equal(backend.data.get('tunebox.volume'), '70');
    assert.equal(storage.number('volume', 50, 0, 100), 70);
});

test('corrupt or out-of-range numbers fall back', () => {
    const backend = memory();
    const storage = createStorage(backend);
    for (const raw of ['abc', '200', '-1', '', 'NaN']) {
        backend.data.set('tunebox.volume', raw);
        assert.equal(storage.number('volume', 50, 0, 100), 50, raw);
    }
});

test('choices outside the allowed list fall back', () => {
    const backend = memory();
    const storage = createStorage(backend);
    backend.data.set('tunebox.visualizer', 'removed-style');
    assert.equal(storage.choice('visualizer', ['wave', 'off'], 'wave'), 'wave');
    backend.data.set('tunebox.visualizer', 'off');
    assert.equal(storage.choice('visualizer', ['wave', 'off'], 'wave'), 'off');
});

test('flags are true only for "1"', () => {
    const backend = memory();
    const storage = createStorage(backend);
    assert.equal(storage.flag('playing'), false);
    storage.set('playing', 1);
    assert.equal(storage.flag('playing'), true);
    storage.set('playing', 0);
    assert.equal(storage.flag('playing'), false);
});

test('blocked or missing storage never throws', () => {
    for (const backend of [blocked, null]) {
        const storage = createStorage(backend);
        storage.set('volume', 10);
        assert.equal(storage.get('volume'), null);
        assert.equal(storage.number('volume', 50, 0, 100), 50);
    }
});
