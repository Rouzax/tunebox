import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createDebugLog, describeError, describeMedia} from '../js/debuglog.js';

function clock() {
    let now = 1000;
    return {now: () => now, advance: (ms) => { now += ms; }};
}

test('lines carry the seconds since play was pressed', () => {
    const time = clock();
    const log = createDebugLog(time.now);
    log.add('page');
    time.advance(5000);
    log.mark();
    log.add('loadstart');
    time.advance(2340);
    log.add('playing');
    assert.equal(log.text(), '0.0 page\n0.0 loadstart\n2.3 playing');
});

test('only the most recent lines are kept', () => {
    const log = createDebugLog(clock().now, 3);
    for (const word of ['a', 'b', 'c', 'd']) {
        log.add(word);
    }
    assert.equal(log.text(), '0.0 b\n0.0 c\n0.0 d');
});

test('listeners hear each new line', () => {
    const log = createDebugLog(clock().now);
    const heard = [];
    log.onLine((line) => heard.push(line));
    log.add('stalled');
    assert.deepEqual(heard, ['0.0 stalled']);
});

test('clear empties the list', () => {
    const log = createDebugLog(clock().now);
    log.add('x');
    log.clear();
    assert.equal(log.text(), '');
});

test('a media element is described by position, buffer and states', () => {
    const element = {
        currentTime: 12.345,
        readyState: 4,
        networkState: 2,
        paused: false,
        buffered: {length: 1, end: () => 20.04},
    };
    assert.equal(describeMedia(element), 't=12.3 buffered=20.0 ready=4 network=2 playing');
    const empty = {currentTime: 0, readyState: 0, networkState: 3, paused: true, buffered: {length: 0}};
    assert.equal(describeMedia(empty), 't=0.0 buffered=0.0 ready=0 network=3 paused');
});

test('a media error keeps the browser message', () => {
    assert.equal(describeError({code: 4, message: 'NS_ERROR_DOM_MEDIA_METADATA_ERR (0x806e0006)'}),
        'code=4 NS_ERROR_DOM_MEDIA_METADATA_ERR (0x806e0006)');
    assert.equal(describeError({code: 2}), 'code=2');
    assert.equal(describeError(null), 'no error object');
});
