import assert from 'node:assert/strict';
import {test} from 'node:test';
import {PIN_MAX, messageFor, onceAsked, press, shouldRetry, trapFocus} from '../js/pin.js';

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
    assert.match(messageFor({status: 401, error: 'pin_wrong'}), /Wrong PIN/);
    assert.match(messageFor({status: 429, error: 'slow_down'}), /Too many tries/);
    assert.match(messageFor({status: 0, error: null}), /Could not reach tunebox/);
    assert.match(messageFor({status: 500, error: null}), /Could not reach tunebox/);
});

test('a busy answer retries once', () => {
    assert.equal(shouldRetry({status: 429, error: 'busy'}), true);
    assert.equal(shouldRetry({status: 429, error: 'slow_down'}), false);
    assert.equal(messageFor({status: 429, error: 'busy'}), 'Busy, trying again...');
    assert.equal(messageFor({status: 429, error: 'slow_down'}), 'Too many tries. Wait a few minutes and try again.');
    assert.equal(messageFor({status: 401, error: 'pin_wrong'}), 'Wrong PIN. Try again.');
    assert.equal(messageFor({status: 0, error: null}), 'Could not reach tunebox. Try again.');
});

test('focus stays inside the keypad', () => {
    const first = {focus() { this.focused = true; }};
    const last = {focus() { this.focused = true; }};
    const container = {querySelectorAll: () => [first, last]};
    let prevented = false;
    trapFocus(container, {key: 'Tab', shiftKey: false, target: last, preventDefault() { prevented = true; }});
    assert.equal(prevented, true);
    assert.equal(first.focused, true);
    prevented = false;
    trapFocus(container, {key: 'Tab', shiftKey: true, target: first, preventDefault() { prevented = true; }});
    assert.equal(prevented, true);
    assert.equal(last.focused, true);
    prevented = false;
    trapFocus(container, {key: 'Tab', shiftKey: false, target: first, preventDefault() { prevented = true; }});
    assert.equal(prevented, false);
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
