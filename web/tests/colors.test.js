import assert from 'node:assert/strict';
import {test} from 'node:test';
import {hueDistance, paletteFromPixels, paletteFromRgb, parseHexColor, rgbToHsl} from '../js/colors.js';

function pixels(...colours) {
    // 32 x 32 RGBA, filled with the colours in equal bands.
    const data = new Uint8ClampedArray(32 * 32 * 4);
    for (let i = 0; i < 32 * 32; i++) {
        const [r, g, b] = colours[Math.floor((i / (32 * 32)) * colours.length)];
        data.set([r, g, b, 255], i * 4);
    }
    return data;
}

test('rgb to hsl', () => {
    assert.deepEqual(rgbToHsl(255, 0, 0), [0, 1, 0.5]);
    assert.deepEqual(rgbToHsl(128, 128, 128).slice(0, 2), [0, 0]);
});

test('hue distance wraps around the circle', () => {
    assert.equal(hueDistance(350, 10), 20);
    assert.equal(hueDistance(90, 270), 180);
});

test('a greyscale cover has no palette', () => {
    assert.equal(paletteFromPixels(pixels([0, 0, 0], [128, 128, 128], [255, 255, 255])), null);
});

test('a one-colour cover gets its hue neighbours', () => {
    const palette = paletteFromPixels(pixels([0, 80, 220]));
    assert.equal(palette.length, 3);
    const [hue] = palette[0];
    assert.ok(Math.abs(hue - 218) <= 2, `hue ${hue}`);
    assert.equal(hueDistance(palette[1][0], hue), 30);
    assert.equal(hueDistance(palette[2][0], hue), 30);
});

test('reds on both sides of 0 average to red, not cyan', () => {
    const [[hue]] = paletteFromPixels(pixels([230, 20, 40], [230, 40, 20]));
    assert.ok(hueDistance(hue, 0) <= 10, `hue ${hue}`);
});

test('two distinct colours are both picked', () => {
    const palette = paletteFromPixels(pixels([220, 30, 30], [30, 30, 220]));
    const hues = palette.slice(0, 2).map(([h]) => h).sort((a, b) => a - b);
    assert.ok(hueDistance(hues[0], 0) <= 10 || hueDistance(hues[1], 0) <= 10);
    assert.ok(hues.some((h) => hueDistance(h, 240) <= 10));
});

test('saturation is floored at 60%', () => {
    for (const [, sat] of paletteFromPixels(pixels([170, 110, 90]))) {
        assert.ok(sat >= 60);
    }
});

test('the accent colour and its neighbours', () => {
    assert.deepEqual(parseHexColor('#62a8ea'), [0x62, 0xa8, 0xea]);
    assert.equal(parseHexColor('rgb(1,2,3)'), null);
    const palette = paletteFromRgb(0x62, 0xa8, 0xea);
    assert.equal(palette[0][0], 209);
    assert.deepEqual(palette.map(([h]) => h), [209, 239, 179]);
});
