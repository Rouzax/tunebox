import assert from 'node:assert/strict';
import {test} from 'node:test';
import {canPlay, groupChannels, nextChannel, pickChannel, pickStream, streamLabel} from '../js/channels.js';

const HIGH = {name: 'High', url: 'https://example.invalid/h', codec: 'aac', kbps: 256};
const MP3 = {name: 'Compatible', url: 'https://example.invalid/m', codec: 'mp3', kbps: null};
const CHANNELS = [
    {id: 'owr', group: 'Tomorrowland', streams: [HIGH, MP3]},
    {id: 'radio-538', group: 'Radio 538', streams: [MP3]},
    {id: 'solo', group: null, streams: [MP3]},
    {id: 'daybreak', group: 'Tomorrowland', streams: [HIGH]},
];

test('groups in first-seen order, ungrouped first', () => {
    assert.deepEqual(
        groupChannels(CHANNELS).map((g) => [g.name, g.channels.map((c) => c.id)]),
        [[null, ['solo']], ['Tomorrowland', ['owr', 'daybreak']], ['Radio 538', ['radio-538']]],
    );
});

test('next and previous follow config order and wrap', () => {
    assert.equal(nextChannel(CHANNELS, 'owr', 1), 'radio-538');
    assert.equal(nextChannel(CHANNELS, 'daybreak', 1), 'owr');
    assert.equal(nextChannel(CHANNELS, 'owr', -1), 'daybreak');
    assert.equal(nextChannel(CHANNELS, 'gone', 1), 'owr');
});

test('stream labels', () => {
    assert.equal(streamLabel(HIGH), 'High (AAC 256)');
    assert.equal(streamLabel(MP3), 'Compatible (MP3)');
    assert.equal(streamLabel({name: 'Default', codec: null, kbps: null}), 'Default');
});

test('a remembered stream that no longer exists falls back to the first', () => {
    assert.equal(pickStream(CHANNELS[0], 'Compatible'), MP3);
    assert.equal(pickStream(CHANNELS[0], 'Removed'), HIGH);
    assert.equal(pickStream(CHANNELS[0], null), HIGH);
});

test('channel choice: link, then remembered, then default, then first', () => {
    assert.equal(pickChannel(CHANNELS, 'daybreak', 'owr', 'solo'), 'daybreak');
    assert.equal(pickChannel(CHANNELS, 'nope', 'radio-538', 'solo'), 'radio-538');
    assert.equal(pickChannel(CHANNELS, null, 'removed', 'solo'), 'solo');
    assert.equal(pickChannel(CHANNELS, null, null, 'removed'), 'owr');
});

test('a stream plays unless the browser says it cannot play its codec', () => {
    const says = (answers) => (type) => answers[type] || '';
    assert.equal(canPlay(HIGH, says({'audio/aac': 'probably'})), true);
    assert.equal(canPlay(HIGH, says({'audio/mpeg': 'maybe'})), false);
    assert.equal(canPlay(MP3, says({'audio/mpeg': 'maybe'})), true);
    assert.equal(canPlay({name: 'Default', codec: null}, says({})), true);
});

test('the stream choice prefers one this browser can play', () => {
    const mp3Only = (stream) => stream.codec === 'mp3';
    assert.equal(pickStream(CHANNELS[0], 'High', mp3Only), MP3);
    assert.equal(pickStream(CHANNELS[0], null, mp3Only), MP3);
    assert.equal(pickStream(CHANNELS[3], null, mp3Only), HIGH);
    assert.equal(pickStream(CHANNELS[0], 'High', () => true), HIGH);
});
