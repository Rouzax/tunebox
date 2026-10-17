import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createEmitter} from '../js/events.js';

test('handlers get emitted values until removed', () => {
    const events = createEmitter();
    const seen = [];
    const off = events.on('track', (value) => seen.push(value));
    events.emit('track', 1);
    off();
    events.emit('track', 2);
    events.emit('other', 3);
    assert.deepEqual(seen, [1]);
});

test('a throwing handler does not stop the others', () => {
    const events = createEmitter();
    const seen = [];
    events.on('x', () => {
        throw new Error('boom');
    });
    events.on('x', (value) => seen.push(value));
    events.emit('x', 'ok');
    assert.deepEqual(seen, ['ok']);
});
