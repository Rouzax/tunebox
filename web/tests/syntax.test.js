import assert from 'node:assert/strict';
import {readFileSync, readdirSync} from 'node:fs';
import {join} from 'node:path';
import {test} from 'node:test';

const JS = new URL('../js/', import.meta.url).pathname;
// The car's browser engine is unknown; stay within what the car theme
// already ran there (ES2019).
const FORBIDDEN = [
    [/\?\.(?!\d)/, 'optional chaining'],
    [/\?\?/, 'nullish coalescing'],
    [/(\|\||&&)=/, 'logical assignment'],
    [/this\.#|^\s*(static\s+)?#[A-Za-z_$]/m, 'private class member'],
    [/^\s*static\s*\{/m, 'static block'],
    [/^await\s/m, 'top-level await'],
];

function stripped(source) {
    // Comments, strings and regex-free template text would give false hits.
    return source
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/(^|[^:\\])\/\/.*$/gm, '$1')
        .replace(/'(?:[^'\\\n]|\\.)*'/g, "''")
        .replace(/"(?:[^"\\\n]|\\.)*"/g, '""')
        .replace(/`(?:[^`\\]|\\.)*`/g, '``');
}

test('player code stays within the ES2019 syntax floor', () => {
    const files = readdirSync(JS).filter((name) => name.endsWith('.js'));
    for (const name of files) {
        const source = stripped(readFileSync(join(JS, name), 'utf8'));
        for (const [pattern, what] of FORBIDDEN) {
            assert.ok(!pattern.test(source), `${name}: ${what}`);
        }
    }
});
