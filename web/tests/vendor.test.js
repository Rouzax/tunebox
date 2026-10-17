import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync, readdirSync, statSync} from 'node:fs';
import {join} from 'node:path';
import {test} from 'node:test';

const VENDOR = new URL('../vendor/', import.meta.url).pathname;

function listed() {
    return readFileSync(join(VENDOR, 'SHA256SUMS'), 'utf8')
        .trim()
        .split('\n')
        .map((line) => line.split(/\s+/))
        .map(([hash, path]) => ({hash, path}));
}

function filesUnder(dir, prefix = '') {
    return readdirSync(dir).flatMap((name) => {
        const full = join(dir, name);
        return statSync(full).isDirectory() ? filesUnder(full, `${prefix}${name}/`) : [`${prefix}${name}`];
    });
}

test('every vendored file matches its recorded hash', () => {
    for (const {hash, path} of listed()) {
        const actual = createHash('sha256').update(readFileSync(join(VENDOR, path))).digest('hex');
        assert.equal(actual, hash, path);
    }
});

test('every vendored file is recorded', () => {
    const recorded = new Set(listed().map((entry) => entry.path));
    const present = filesUnder(VENDOR).filter((path) => !['SHA256SUMS', 'README.md'].includes(path));
    assert.deepEqual(present.filter((path) => !recorded.has(path)), []);
});
