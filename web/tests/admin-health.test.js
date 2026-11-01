import assert from 'node:assert/strict';
import {test} from 'node:test';
import {itemLine, menuText, progressText, reasonText, retryText, runLine, shownReasons} from '../../web_admin/js/health.js';

test('station reasons read as sentences', () => {
    assert.equal(reasonText({code: 'streams_failing', item: null}, null), 'No stream works');
    assert.equal(reasonText({code: 'stream_failing', item: 'stream:MP3'}, {name: 'MP3'}), 'MP3 does not work');
    assert.equal(reasonText({code: 'stuck', item: null}, null), 'No new song for more than 2 hours');
    assert.equal(reasonText({code: 'something_new', item: null}, null), 'something_new');
});

test('not checked yet is left to the item lines', () => {
    const reasons = [
        {code: 'not_checked', item: 'stream:Default'},
        {code: 'feed_failing', item: 'feed'},
        {code: 'not_checked', item: 'stream:MP3'},
    ];
    assert.deepEqual(shownReasons(reasons), [{code: 'feed_failing', item: 'feed'}]);
});

test('an item line says what works or since when it fails', () => {
    assert.equal(itemLine({kind: 'stream', ok: true, codec: 'aac', kbps: 64, reason: null}, 0), 'AAC 64 kbps');
    assert.equal(itemLine({kind: 'stream', ok: true, codec: 'mp3', kbps: null, reason: null}, 0), 'MP3');
    assert.equal(itemLine({kind: 'stream', ok: null}, 0), 'Not checked yet');
    const failing = {kind: 'stream', ok: false, reason: 'http_404', failing_since: 0};
    assert.match(itemLine(failing, 3 * 3600 * 1000), /^Answered HTTP 404, failing for 3 h$/);
    assert.equal(itemLine({kind: 'feed', ok: true, reason: null}, 0), 'Answers');
});

test('progress, retry and the menu', () => {
    assert.equal(progressText({done: 12, total: 152}), 'Checking 12 of 152');
    assert.equal(retryText(61), 'You can check again in 2 min.');
    assert.equal(retryText(30), 'You can check again in 1 min.');
    assert.equal(menuText({broken: 2}), 'Health (2)');
    assert.equal(menuText({broken: 0}), 'Health');
    assert.equal(menuText({broken: null}), 'Health');
});

test('the last run line', () => {
    assert.equal(runLine({last_finished_at: null}, 0), 'Not checked yet. The first check runs 5 minutes after tunebox starts, then daily at 04:30.');
    assert.match(runLine({last_finished_at: 0, duration_ms: 600000, outcome: 'ok', items: 150}, 3600000), /^Last check 1 h ago: 150 items in 10 min\.$/);
    assert.match(runLine({last_finished_at: 0, duration_ms: 1000, outcome: 'network_down', items: 150}, 60000),
        /^Last check 1 min ago could not reach the internet; tunebox tries again later\.$/);
    const crashed = runLine({last_finished_at: 0, duration_ms: 1000, outcome: 'error', items: 150}, 60000);
    assert.equal(crashed, 'Last check 1 min ago stopped on an error in tunebox; see the log.');
    assert.doesNotMatch(crashed, /items in/);
});
