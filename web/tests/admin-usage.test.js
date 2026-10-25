import assert from 'node:assert/strict';
import {test} from 'node:test';
import {DELETE_QUESTION, bars, duration, endedLine, stationName} from '../../web_admin/js/usage.js';

test('a deleted station shows by its id', () => {
    const channels = [{id: 'owr', doc: {name: 'OWR'}}, {id: 'bare', doc: {}}];
    assert.equal(stationName(channels, 'owr'), 'OWR');
    assert.equal(stationName(channels, 'gone'), 'gone');
    assert.equal(stationName(channels, 'bare'), 'bare');
});

test('durations read naturally', () => {
    assert.equal(duration(45), '45 s');
    assert.equal(duration(720), '12 min');
    assert.equal(duration(7500), '2 h 5 min');
    assert.equal(duration(7200), '2 h');
    assert.equal(duration(0), '0 s');
    // Rounded to the minute before the hours are split off.
    assert.equal(duration(3599), '1 h');
});

test('bars are shares of the largest bucket', () => {
    const out = bars([{label: 'a', seconds: 50, visits: 1}, {label: 'b', seconds: 100, visits: 0}]);
    assert.deepEqual(out.map((b) => b.share), [0.5, 1]);
    assert.deepEqual(out.map((b) => b.label), ['a', 'b']);
    assert.deepEqual(bars([{label: 'a', seconds: 0, visits: 0}]).map((b) => b.share), [0]);
    assert.deepEqual(bars([]), []);
});

test('bars can measure visits instead', () => {
    const out = bars([{label: 'a', seconds: 50, visits: 1}, {label: 'b', seconds: 100, visits: 4}], 'visits');
    assert.deepEqual(out.map((b) => b.share), [0.25, 1]);
});

test('the last ended session names the station and how long ago', () => {
    const channels = [{id: 'q', doc: {name: 'Qmusic'}}];
    const last = {label: 'Car', channel: 'q', stream: 'AAC', seconds: 600, ended_at: 1000};
    assert.equal(endedLine(last, channels, 1000 + 14 * 60), 'Car · Qmusic · ended 14 min ago');
    // A clock behind the server's never says "in the future".
    assert.equal(endedLine(last, channels, 990), 'Car · Qmusic · ended 0 s ago');
});

test('the delete question says today loses the device', () => {
    assert.equal(DELETE_QUESTION,
        "Delete this device and its listening history? Totals of finished days stay; today's figures lose this device.");
});
