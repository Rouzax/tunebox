import assert from 'node:assert/strict';
import test from 'node:test';

import {call, send, setCsrf} from '../../web_admin/js/api.js';

test('a call that never answers fails at its deadline', async () => {
    const never = (_url, init) => new Promise((_resolve, reject) => {
        init.signal.addEventListener('abort', () => reject(new Error('aborted')));
    });
    await assert.rejects(call('GET', 'config', undefined, {fetchFn: never, deadlineMs: 20}));
});

test('an answer before the deadline is read as usual', async () => {
    const quick = async () => ({ok: true, status: 200, json: async () => ({a: 1})});
    assert.deepEqual(await call('GET', 'config', undefined, {fetchFn: quick, deadlineMs: 1000}),
        {ok: true, status: 200, data: {a: 1}});
});

test('send passes a raw body with its own type and the token', async () => {
    let seen = null;
    setCsrf('t0k');
    const fetchFn = async (url, init) => { seen = {url, init}; return {ok: true, status: 200, json: async () => ({logo: 'blob:x'})}; };
    const answer = await send('POST', 'logos', new Uint8Array([1, 2]), 'image/png', {fetchFn});
    assert.equal(answer.data.logo, 'blob:x');
    assert.equal(seen.init.headers['Content-Type'], 'image/png');
    assert.equal(seen.init.headers['X-Tunebox-CSRF'], 't0k');
    assert.deepEqual(seen.init.body, new Uint8Array([1, 2]));
    setCsrf(null);
});

test('call sends JSON under its content type and the token', async () => {
    let seen = null;
    setCsrf('t0k');
    const fetchFn = async (url, init) => { seen = {url, init}; return {ok: true, status: 200, json: async () => ({})}; };
    await call('POST', 'config', {a: 1}, {fetchFn});
    assert.equal(seen.url, '/admin/api/config');
    assert.equal(seen.init.headers['Content-Type'], 'application/json');
    assert.equal(seen.init.headers['X-Tunebox-CSRF'], 't0k');
    assert.equal(seen.init.body, JSON.stringify({a: 1}));
    setCsrf(null);
});

test('asText answers the body as text with the disposition, errors stay JSON', async () => {
    const headers = {get: (name) => (name === 'Content-Disposition' ? 'attachment; filename="x.toml"' : null)};
    const good = async () => ({ok: true, status: 200, headers, text: async () => 'a = 1\n'});
    assert.deepEqual(await call('GET', 'export', undefined, {fetchFn: good, asText: true}),
        {ok: true, status: 200, data: 'a = 1\n', disposition: 'attachment; filename="x.toml"'});
    const bad = async () => ({ok: false, status: 401, headers, json: async () => ({error: 'login_required'})});
    assert.deepEqual(await call('GET', 'export', undefined, {fetchFn: bad, asText: true}),
        {ok: false, status: 401, data: {error: 'login_required'}});
});
