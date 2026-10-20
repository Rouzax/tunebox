import assert from 'node:assert/strict';
import {test} from 'node:test';
import {PIN_MAX, messageFor, onceAsked, press} from '../js/pin.js';

test('press adds digits up to the longest PIN and deletes with back', () => {
    let entry = '';
    for (const key of '123456789') {
        entry = press(entry, key);
    }
    assert.equal(entry, '12345678');
    assert.equal(entry.length, PIN_MAX);
    assert.equal(press(entry, 'back'), '1234567');
    assert.equal(press('', 'back'), '');
});

test('press ignores everything that is not a digit', () => {
    for (const key of ['a', 'Enter', ' ', '12', '１', 'ok']) {
        assert.equal(press('12', key), '12');
    }
});

test('each refusal has its own message', () => {
    assert.match(messageFor(401), /Wrong PIN/);
    assert.match(messageFor(429), /Too many tries/);
    assert.match(messageFor(0), /Could not reach tunebox/);
    assert.match(messageFor(500), /Could not reach tunebox/);
});

test('a 401 from now asks for the PIN and refreshes after it', async () => {
    let asked = 0;
    let after = 0;
    let open = null;
    const locked = onceAsked(() => {
        asked += 1;
        return new Promise((resolve) => {
            open = resolve;
        });
    }, () => {
        after += 1;
    });
    // Every poll while the screen shows gets a 401 too.
    locked();
    locked();
    locked();
    assert.equal(asked, 1);
    open(true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(after, 1);
    locked();
    assert.equal(asked, 2);
});
