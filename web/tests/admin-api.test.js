import assert from 'node:assert/strict';
import test from 'node:test';

import {call} from '../../web_admin/js/api.js';

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
