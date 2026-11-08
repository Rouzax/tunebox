import assert from 'node:assert/strict';
import {test} from 'node:test';
import {COVER_FADE_S, COVER_SIZE, coverFade, createLevels, parseCssColor} from '../js/gpustylecore.js';

const near = (actual, expected) => actual.forEach((value, i) => assert.ok(Math.abs(value - expected[i]) < 0.005, `${actual} vs ${expected}`));

test('parseCssColor reads what the root\'s palette properties hold', () => {
    near(parseCssColor('rgb(98, 168, 234)'), [98 / 255, 168 / 255, 234 / 255]);
    near(parseCssColor(' rgba(255, 0, 0, 0.5) '), [1, 0, 0]);
    near(parseCssColor('rgb(0 255 0)'), [0, 1, 0]);
    near(parseCssColor('#62a8ea'), [98 / 255, 168 / 255, 234 / 255]);
    near(parseCssColor('hsl(0, 100%, 50%)'), [1, 0, 0]);
    near(parseCssColor('hsl(120, 100%, 50%)'), [0, 1, 0]);
    near(parseCssColor('hsl(240 100% 50%)'), [0, 0, 1]);
    near(parseCssColor('hsl(-120, 100%, 50%)'), [0, 0, 1]);
    near(parseCssColor('hsl(210, 0%, 55%)'), [0.55, 0.55, 0.55]);
    near(parseCssColor('rgb(300, -5, 10)'), [1, 0, 10 / 255]);
});

test('parseCssColor answers null for anything else', () => {
    for (const text of ['', 'red', 'rgb(1, 2)', 'rgb(a, b, c)', 'color(srgb 1 0 0)', null, undefined]) {
        assert.equal(parseCssColor(text), null, String(text));
    }
});

test('createLevels: silence stays still, a steady level rises to the top of the range', () => {
    const levels = createLevels();
    for (let i = 0; i < 30; i++) {
        levels.update({bass: 0, mid: 0, high: 0});
    }
    assert.deepEqual(levels.update({bass: 0, mid: 0, high: 0}), {bass: 0, mid: 0, high: 0});
    let out = null;
    for (let i = 0; i < 40; i++) {
        out = levels.update({bass: 0.8, mid: 0.8, high: 0.8});
    }
    assert.ok(out.bass > 0.99 && out.mid > 0.99 && out.high > 0.99, JSON.stringify(out));
});

test('createLevels reads a quiet band against the floor and a fading peak', () => {
    const levels = createLevels();
    let out = null;
    for (let i = 0; i < 40; i++) {
        out = levels.update({bass: 0.1, mid: 0, high: 0});
    }
    assert.ok(Math.abs(out.bass - 0.5) < 0.01, String(out.bass));
    levels.update({bass: 1, mid: 0, high: 0});
    for (let i = 0; i < 2000; i++) {
        out = levels.update({bass: 0.5, mid: 0, high: 0});
    }
    // The loud moment's peak has faded; 0.5 is the top again.
    assert.ok(out.bass > 0.99, String(out.bass));
});

test('coverFade runs from 0 to 1 over a second', () => {
    assert.equal(COVER_SIZE, 256);
    assert.equal(COVER_FADE_S, 1);
    assert.equal(coverFade(5000, 5000), 0);
    assert.equal(coverFade(5500, 5000), 0.5);
    assert.equal(coverFade(9000, 5000), 1);
    assert.equal(coverFade(4000, 5000), 0);
});
