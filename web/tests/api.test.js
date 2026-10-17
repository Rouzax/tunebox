import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createApi, createPoller} from '../js/api.js';

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return {promise, resolve, reject};
}

function harness() {
    const calls = [];
    const timers = [];
    const seen = {bodies: [], errors: 0, dates: []};
    const api = {
        now(channelId, bufferS, etag) {
            const call = {channelId, bufferS, etag, ...deferred()};
            calls.push(call);
            return call.promise;
        },
    };
    const poller = createPoller({
        api,
        getBufferS: () => 30,
        onBody: (body) => seen.bodies.push(body),
        onDate: (date) => seen.dates.push(date),
        onError: () => {
            seen.errors += 1;
        },
        setTimer: (fn, ms) => {
            const timer = {fn, ms};
            timers.push(timer);
            return timer;
        },
        clearTimer: (timer) => {
            timer.cleared = true;
        },
    });
    const settle = () => new Promise((resolve) => setImmediate(resolve));
    return {calls, timers, seen, poller, settle};
}

test('polls with the buffer and the last ETag, and a 304 changes nothing', async () => {
    const {calls, timers, seen, poller, settle} = harness();
    poller.start('owr');
    assert.deepEqual([calls[0].channelId, calls[0].bufferS, calls[0].etag], ['owr', 30, null]);
    calls[0].resolve({status: 200, body: {n: 1}, etag: '"a"', date: 'D1'});
    await settle();
    assert.deepEqual(seen.bodies, [{n: 1}]);
    assert.equal(timers.length, 1);
    assert.equal(timers[0].ms, 10000);
    timers[0].fn();
    assert.equal(calls[1].etag, '"a"');
    calls[1].resolve({status: 304, date: 'D2'});
    await settle();
    assert.deepEqual(seen.bodies, [{n: 1}]);
    assert.deepEqual(seen.dates, ['D1', 'D2']);
});

test('a late answer for the previous channel is dropped', async () => {
    const {calls, seen, poller, settle} = harness();
    poller.start('owr');
    poller.start('daybreak');
    calls[1].resolve({status: 200, body: {channel: 'daybreak'}, etag: '"b"'});
    calls[0].resolve({status: 200, body: {channel: 'owr'}, etag: '"a"'});
    await settle();
    assert.deepEqual(seen.bodies, [{channel: 'daybreak'}]);
    assert.equal(calls[1].etag, null);
});

test('errors are reported and polling goes on', async () => {
    const {calls, timers, seen, poller, settle} = harness();
    poller.start('owr');
    calls[0].reject(new Error('offline'));
    await settle();
    assert.equal(seen.errors, 1);
    assert.equal(timers.length, 1);
});

test('stop ends polling, refresh polls at once', async () => {
    const {calls, timers, poller, settle} = harness();
    poller.start('owr');
    calls[0].resolve({status: 200, body: {}, etag: '"a"'});
    await settle();
    poller.refresh();
    assert.equal(timers[0].cleared, true);
    assert.equal(calls.length, 2);
    poller.stop();
    calls[1].resolve({status: 200, body: {}, etag: '"b"'});
    await settle();
    assert.equal(timers.length, 1);
    poller.refresh();
    assert.equal(calls.length, 2);
});

test('the API client asks the right paths and reads the headers', async () => {
    const asked = [];
    const fetchFn = async (path, init) => {
        asked.push([path, init.headers]);
        const status = path.includes('now') && init.headers['If-None-Match'] ? 304 : 200;
        return {
            ok: status === 200,
            status,
            headers: {get: (name) => ({ETag: '"e"', Date: 'Sat, 17 Oct 2026 13:00:00 GMT'})[name] || null},
            json: async () => ({ok: true}),
        };
    };
    const api = createApi(fetchFn);
    assert.deepEqual(await api.channels(), {ok: true});
    const first = await api.now('radio-538', 31, null);
    assert.deepEqual(first, {status: 200, body: {ok: true}, etag: '"e"', date: 'Sat, 17 Oct 2026 13:00:00 GMT'});
    const second = await api.now('radio-538', 31, '"e"');
    assert.equal(second.status, 304);
    assert.equal(asked[1][0], '/api/channels/radio-538/now?buffer_s=31');
    assert.deepEqual(asked[2][1], {'If-None-Match': '"e"'});
});
