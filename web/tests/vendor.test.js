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

// Dependabot reads web/package.json; the browser loads web/vendor/. A bumped
// pin without the new files vendored fails here, so that PR cannot merge.
// Each pinned package names the vendored file whose header carries its version.
const VERSIONED_FILE = {'audiomotion-analyzer': 'audioMotion-analyzer.js'};

test('every pinned npm package is the version vendored and imported', () => {
    const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
    const pins = Object.entries(pkg.devDependencies || {});
    assert.ok(pins.length > 0);
    const sources = readdirSync(new URL('../js/', import.meta.url).pathname)
        .map((name) => readFileSync(new URL(`../js/${name}`, import.meta.url), 'utf8'))
        .join('\n');
    for (const [name, version] of pins) {
        assert.match(version, /^\d+\.\d+\.\d+$/, `${name} is pinned exactly`);
        const folder = `${name}-${version}`;
        assert.ok(statSync(join(VENDOR, folder)).isDirectory(), `${folder} is vendored`);
        assert.ok(VERSIONED_FILE[name], `${name} has a versioned file listed`);
        const header = readFileSync(join(VENDOR, folder, VERSIONED_FILE[name]), 'utf8').slice(0, 400);
        assert.match(header, new RegExp(`@version ${version.replace(/\./g, '\\.')}\\b`), `${folder} holds ${version}`);
        assert.ok(sources.includes(`vendor/${folder}/`), `the player imports ${folder}`);
    }
});
