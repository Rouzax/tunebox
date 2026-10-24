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
    const seen = {bodies: [], errors: 0, dates: [], reached: 0, missing: 0};
    const api = {
        now(channelId, bufferS, etag, stream) {
            const call = {channelId, bufferS, etag, stream, ...deferred()};
            calls.push(call);
            return call.promise;
        },
    };
    const poller = createPoller({
        api,
        getBufferS: () => 30,
        getStream: () => 1,
        onBody: (body) => seen.bodies.push(body),
        onMissing: () => {
            seen.missing += 1;
        },
        onReached: () => {
            seen.reached += 1;
        },
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
    assert.deepEqual([calls[0].channelId, calls[0].bufferS, calls[0].etag, calls[0].stream], ['owr', 30, null, 1]);
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
    assert.deepEqual(await api.channels(), {channels: {ok: true}, version: 0, more: false, pin: '', images: ''});
    const first = await api.now('radio-538', 31, null);
    assert.deepEqual(first, {status: 200, body: {ok: true}, etag: '"e"', date: 'Sat, 17 Oct 2026 13:00:00 GMT'});
    const second = await api.now('radio-538', 31, '"e"');
    assert.equal(second.status, 304);
    assert.equal(asked[1][0], '/api/channels/radio-538/now?buffer_s=31');
    assert.deepEqual(asked[2][1], {'If-None-Match': '"e"'});
    await api.now('radio-538', 3, null, 0);
    assert.equal(asked[3][0], '/api/channels/radio-538/now?buffer_s=3&stream=0');
});

test('every answer, 304 included, reports tunebox reached', async () => {
    const {calls, timers, seen, poller, settle} = harness();
    poller.start('owr');
    calls[0].resolve({status: 200, body: {n: 1}, etag: '"a"', date: 'D1'});
    await settle();
    timers[0].fn();
    calls[1].resolve({status: 304, date: 'D2'});
    await settle();
    assert.equal(seen.reached, 2);
});

test('a 404 or 400 from /now reports a missing channel or stream, not lost contact', async () => {
    const {calls, seen, poller, settle} = harness();
    poller.start('owr');
    calls[0].reject(Object.assign(new Error('gone'), {status: 404}));
    await settle();
    assert.equal(seen.missing, 1);
    assert.equal(seen.errors, 0);
    assert.equal(seen.reached, 1);
});

test('channels() returns the list with the configuration version', async () => {
    const api = createApi(async () => ({
        ok: true,
        status: 200,
        headers: {get: (name) => (name === 'X-Tunebox-Version' ? '3' : null)},
        json: async () => [{id: 'owr'}],
    }));
    assert.deepEqual(await api.channels(), {channels: [{id: 'owr'}], version: 3, more: false, pin: '', images: ''});
});

test('now() sends the stream by name, encoded', async () => {
    const paths = [];
    const api = createApi(async (path) => {
        paths.push(path);
        return {ok: true, status: 200, headers: {get: () => null}, json: async () => ({})};
    });
    await api.now('owr', 30, null, 'High & Low');
    assert.match(paths[0], /stream=High%20%26%20Low$/);
});

test('a failed request carries its status', async () => {
    const api = createApi(async () => ({ok: false, status: 400, headers: {get: () => null}}));
    await assert.rejects(api.now('owr', 0, null, null), (error) => error.status === 400);
});

test('pin posts the PIN as JSON and gives the status back', async () => {
    const sent = [];
    const api = createApi(async (path, init) => {
        sent.push({path, init});
        return {ok: false, status: 401, headers: new Map(), json: async () => ({error: 'pin_wrong'})};
    });
    assert.deepEqual(await api.pin('123456'), {status: 401, error: 'pin_wrong'});
    assert.equal(sent[0].path, '/api/pin');
    assert.equal(sent[0].init.method, 'POST');
    assert.equal(sent[0].init.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(sent[0].init.body), {pin: '123456'});
});

test('a 401 on the channel list is an error that says 401', async () => {
    const api = createApi(async () => ({ok: false, status: 401, headers: new Map()}));
    await assert.rejects(api.channels(), (error) => error.status === 401);
});

test('the channel list says when stations wait behind the PIN', async () => {
    const answer = (more) => async () => ({
        ok: true,
        status: 200,
        headers: new Map([['X-Tunebox-Version', '3'], ...(more ? [['X-Tunebox-Pin', 'more']] : [])]),
        json: async () => [],
    });
    assert.equal((await createApi(answer(true)).channels()).more, true);
    assert.equal((await createApi(answer(false)).channels()).more, false);
});

test('channels carries the pin revision and pin() the error', async () => {
    const api = createApi(async (path) => (path === '/api/pin'
        ? {ok: false, status: 429, json: async () => ({error: 'busy'})}
        : {ok: true, status: 200, json: async () => [], headers: new Map([
            ['X-Tunebox-Version', '3'], ['X-Tunebox-Pin-Revision', 'stations.2'],
            ['X-Tunebox-Images', 'ab.4']])}));
    const listed = await api.channels();
    assert.equal(listed.pin, 'stations.2');
    assert.equal(listed.images, 'ab.4');
    assert.deepEqual(await api.pin('123456'), {status: 429, error: 'busy'});
});
