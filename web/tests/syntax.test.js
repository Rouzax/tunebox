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
    // Newer DOM and library calls the car may not have (Chrome 86+, 92+, 98+).
    [/\.replaceChildren\(/, 'replaceChildren()'],
    [/\.at\(/, 'Array.prototype.at()'],
    [/structuredClone\(/, 'structuredClone()'],
    [/\.replaceAll\(/, 'String.prototype.replaceAll()'],
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

test('player code stays within the ES2019 syntax and API floor', () => {
    const files = readdirSync(JS).filter((name) => name.endsWith('.js')).map((name) => join(JS, name));
    // The status and check pages run in the car too.
    files.push(new URL('../status.js', import.meta.url).pathname, new URL('../check.js', import.meta.url).pathname);
    for (const path of files) {
        const name = path.split('/').pop();
        const source = stripped(readFileSync(path, 'utf8'));
        for (const [pattern, what] of FORBIDDEN) {
            assert.ok(!pattern.test(source), `${name}: ${what}`);
        }
    }
});
