import assert from 'node:assert/strict';
import {test} from 'node:test';
import {isNight, sunTimes} from '../js/sun.js';

// Published sunrise and sunset for Amsterdam, in UTC.
const AMSTERDAM = [
    ['2026-06-21', '03:18', '20:06'],
    ['2026-12-21', '07:48', '15:29'],
    ['2026-10-17', '06:09', '16:44'],
];

function minutes(date) {
    return date.getUTCHours() * 60 + date.getUTCMinutes();
}

function clock(text) {
    const [h, m] = text.split(':').map(Number);
    return h * 60 + m;
}

test('sun times match published Amsterdam times within two minutes', () => {
    for (const [day, rise, set] of AMSTERDAM) {
        const times = sunTimes(new Date(`${day}T12:00:00Z`), 52.37, 4.9);
        assert.ok(Math.abs(minutes(times.rise) - clock(rise)) <= 2, `${day} rise ${times.rise.toISOString()}`);
        assert.ok(Math.abs(minutes(times.set) - clock(set)) <= 2, `${day} set ${times.set.toISOString()}`);
    }
});

test('night by mode, by the sun, or by the browser setting', () => {
    const noon = new Date('2026-10-17T12:00:00Z');
    const late = new Date('2026-10-17T21:00:00Z');
    const here = {latitude: 52.37, longitude: 4.9};
    assert.equal(isNight('night', noon, here, false), true);
    assert.equal(isNight('day', late, here, true), false);
    assert.equal(isNight('auto', noon, here, true), false);
    assert.equal(isNight('auto', late, here, false), true);
    assert.equal(isNight('auto', noon, null, true), true);
    assert.equal(isNight('auto', late, null, false), false);
});

test('sun times for a height above the horizon come before sunset and after sunrise', () => {
    const day = new Date('2026-12-21T12:00:00Z');
    const plain = sunTimes(day, 52.1, 5.3);
    const high = sunTimes(day, 52.1, 5.3, 4);
    const before = (plain.set - high.set) / 60000;
    const after = (high.rise - plain.rise) / 60000;
    assert.ok(before > 38 && before < 48, `set ${before} min earlier`);
    assert.ok(after > 38 && after < 48, `rise ${after} min later`);
});

test('automatic night starts while the sun is still a little above the horizon', () => {
    // Sunset 15:29 UTC and sunrise 07:47 UTC in the middle of the country.
    const centre = {latitude: 52.1, longitude: 5.3};
    assert.equal(isNight('auto', new Date('2026-12-21T15:05:00Z'), centre, false), true);
    assert.equal(isNight('auto', new Date('2026-12-21T14:30:00Z'), centre, false), false);
    assert.equal(isNight('auto', new Date('2026-12-21T08:10:00Z'), centre, false), true);
    assert.equal(isNight('auto', new Date('2026-12-21T08:50:00Z'), centre, false), false);
});

test('a sun that never reaches the height means night all day, one that never sets day all night', () => {
    const north = {latitude: 78.2, longitude: 15.6};
    assert.equal(isNight('auto', new Date('2026-12-21T11:00:00Z'), north, false), true);
    assert.equal(isNight('auto', new Date('2026-06-21T23:00:00Z'), north, false), false);
});
