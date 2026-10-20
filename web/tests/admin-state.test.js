import assert from 'node:assert/strict';
import {test} from 'node:test';
import {
    addGroup, addStation, decodeHashPart, deleteGroup, deleteStation, diffStates, duplicateStation, moveGroup,
    moveStation, newStation, renameGroup, replaceStation, saveMessage, setEnabled, setSettings, stationsByGroup, validId,
} from '../../web_admin/js/state.js';

function base() {
    return {
        defaults: {},
        player: {default_channel: 'b'},
        groups: ['G', 'H'],
        channels: [
            newStation('a', 'A', 'G'),
            newStation('b', 'B', 'G'),
            newStation('c', 'C', null),
            newStation('d', 'D', 'H'),
        ],
    };
}

test('stationsByGroup lists ungrouped first, then groups in order', () => {
    const groups = stationsByGroup(base());
    assert.deepEqual(groups.map((g) => [g.name, g.rows.map((r) => r.id)]), [
        [null, ['c']], ['G', ['a', 'b']], ['H', ['d']],
    ]);
});

test('functions never change their input', () => {
    const state = base();
    const copy = JSON.parse(JSON.stringify(state));
    moveStation(state, 'b', -1);
    deleteStation(state, 'a');
    renameGroup(state, 'G', 'X');
    assert.deepEqual(state, copy);
});

test('moveStation moves within the group only', () => {
    let state = moveStation(base(), 'b', -1);
    assert.deepEqual(stationsByGroup(state)[1].rows.map((r) => r.id), ['b', 'a']);
    state = moveStation(state, 'b', -1);
    assert.deepEqual(stationsByGroup(state)[1].rows.map((r) => r.id), ['b', 'a']);
});

test('deleteStation clears the default', () => {
    const {state, clearedDefault} = deleteStation(base(), 'b');
    assert.equal(clearedDefault, true);
    assert.equal(state.player.default_channel, undefined);
    assert.equal(state.channels.some((r) => r.id === 'b'), false);
    assert.equal(deleteStation(base(), 'a').clearedDefault, false);
});

test('disabling the default clears it too', () => {
    const {state, clearedDefault} = setEnabled(base(), 'b', false);
    assert.equal(clearedDefault, true);
    assert.equal(state.channels.find((r) => r.id === 'b').enabled, false);
});

test('duplicateStation picks a free id and keeps the place', () => {
    let result = duplicateStation(base(), 'a');
    assert.equal(result.id, 'a-copy');
    assert.equal(result.state.channels[1].id, 'a-copy');
    assert.equal(result.state.channels[1].doc.id, 'a-copy');
    assert.equal(result.state.channels[1].doc.name, 'A (copy)');
    result = duplicateStation(result.state, 'a');
    assert.equal(result.id, 'a-copy-2');
});

test('groups: add, rename (rows follow), move, delete only when empty', () => {
    let state = addGroup(base(), 'New');
    assert.deepEqual(state.groups, ['G', 'H', 'New']);
    state = renameGroup(state, 'G', 'Gee');
    assert.deepEqual(state.groups, ['Gee', 'H', 'New']);
    assert.equal(state.channels[0].group, 'Gee');
    state = moveGroup(state, 'New', -1);
    assert.deepEqual(state.groups, ['Gee', 'New', 'H']);
    assert.deepEqual(deleteGroup(state, 'H').groups, state.groups);
    assert.deepEqual(deleteGroup(state, 'New').groups, ['Gee', 'H']);
});

test('addStation puts a station at the end of its group', () => {
    const state = addStation(base(), newStation('e', 'E', 'G'));
    assert.deepEqual(stationsByGroup(state)[1].rows.map((r) => r.id), ['a', 'b', 'e']);
});

test('validId', () => {
    assert.equal(validId('radio-538'), true);
    assert.equal(validId('Radio'), false);
    assert.equal(validId('-x'), false);
    assert.equal(validId(''), false);
});

test('diffStates lists changed leaves by station id', () => {
    const before = base();
    const after = JSON.parse(JSON.stringify(before));
    after.channels[0].doc.name = 'Renamed';
    after.channels.pop();
    after.player.default_channel = 'a';
    const paths = diffStates(before, after).map((d) => d.path);
    assert.ok(paths.includes('channels.a.doc.name'));
    assert.ok(paths.includes('channels.d'));
    assert.ok(paths.includes('player.default_channel'));
});

test('diffStates reports a new order', () => {
    const before = base();
    const after = moveStation(before, 'b', -1);
    assert.deepEqual(diffStates(before, after).map((d) => d.path), ['order']);
    assert.deepEqual(diffStates(before, before), []);
});

test('replaceStation keeps the place and the group order', () => {
    const row = newStation('a', 'Renamed', 'G');
    const state = replaceStation(base(), 'a', row);
    assert.equal(state.channels.find((r) => r.id === 'a').doc.name, 'Renamed');
    assert.deepEqual(stationsByGroup(state)[1].rows.map((r) => r.id), ['a', 'b']);
    const moved = replaceStation(base(), 'c', newStation('c', 'C', 'H'));
    assert.deepEqual(stationsByGroup(moved).map((g) => g.name), ['G', 'H']);
    assert.deepEqual(stationsByGroup(moved)[1].rows.map((r) => r.id), ['d', 'c']);
});

test('setSettings keeps the numbers given and drops the empty ones', () => {
    const state = base();
    const next = setSettings(state, {
        defaults: {delay_s: 9, pawtunes_delay_s: undefined, poll_interval_s: 12, history_size: undefined},
        latitude: undefined,
        longitude: undefined,
        defaultChannel: '',
    });
    assert.deepEqual(next.defaults, {delay_s: 9, poll_interval_s: 12});
    assert.deepEqual(next.player, {});
    assert.equal(next.channels, state.channels);
    assert.deepEqual(state.player, {default_channel: 'b'});
});

test('setSettings takes the location only as a pair, and the default station', () => {
    const next = setSettings(base(), {defaults: {}, latitude: 52.1, longitude: 5.1, defaultChannel: 'c'});
    assert.deepEqual(next.player, {latitude: 52.1, longitude: 5.1, default_channel: 'c'});
    const half = setSettings(base(), {defaults: {}, latitude: 52.1, longitude: undefined, defaultChannel: ''});
    assert.deepEqual(half.player, {});
});

test('a malformed hash part is not a station id', () => {
    assert.equal(decodeHashPart('owr'), 'owr');
    assert.equal(decodeHashPart('a%20b'), 'a b');
    assert.equal(decodeHashPart('%'), null);
});

test('the save message says what happened', () => {
    assert.equal(saveMessage({changed: true, applied: true}, 'Saved.'), 'Saved.');
    assert.equal(saveMessage({changed: false, applied: true}, 'Saved.'), 'No changes to save.');
    assert.equal(saveMessage({changed: true, applied: false}, 'Saved.'),
        'Saved, but not live yet: tunebox could not start the change. The log says why.');
    assert.equal(saveMessage({changed: false, applied: false}, 'Saved.'),
        'Nothing new to save, but the stored change is still not live. The log says why.');
});
