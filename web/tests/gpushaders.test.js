import assert from 'node:assert/strict';
import {test} from 'node:test';
import {HEADER} from '../js/gpurender.js';
import {SHADERS, WARP_FIELD} from '../js/gpushaders.js';

const PASSES = {single: 1, feedback: 2, field: 2};

test('each GPU style has one shader per pass of its kind, on the shared header', () => {
    assert.deepEqual(Object.keys(SHADERS), ['tunnel', 'kaleido', 'synthwave', 'ink']);
    assert.equal(SHADERS.tunnel.kind, 'feedback');
    assert.equal(SHADERS.kaleido.kind, 'feedback');
    assert.equal(SHADERS.synthwave.kind, 'single');
    assert.equal(SHADERS.ink.kind, 'field');
    for (const [id, shader] of Object.entries(SHADERS)) {
        assert.equal(shader.sources.length, PASSES[shader.kind], id);
        for (const source of shader.sources) {
            assert.ok(source.startsWith(HEADER), id);
            assert.ok(/void main\(\)/.test(source), id);
            // Undefined in GLSL: write 1.0 - smoothstep(b, a, x) instead.
            for (const call of source.match(/smoothstep\(([-\d.]+), ([-\d.]+),/g) || []) {
                const [a, b] = call.slice('smoothstep('.length).split(',').map(parseFloat);
                assert.ok(a < b, `${id}: ${call}`);
            }
        }
    }
});

// The body after the shared header, which declares every input itself.
const body = (source) => source.slice(HEADER.length);

test('the cover styles read the cover, and every second pass reads the first', () => {
    for (const id of ['tunnel', 'kaleido']) {
        assert.ok(/cover\(/.test(body(SHADERS[id].sources[0])), id);
        assert.ok(/uInput/.test(body(SHADERS[id].sources[0])), `${id} draws over its frame before`);
    }
    for (const id of ['tunnel', 'kaleido', 'ink']) {
        assert.ok(/uInput/.test(body(SHADERS[id].sources[1])), id);
    }
    assert.equal(SHADERS.ink.sources[0], WARP_FIELD);
    for (const [id, shader] of Object.entries(SHADERS)) {
        assert.ok(shader.sources.some((source) => /uPal\[/.test(body(source))), `${id} takes the cover's colours`);
    }
});
