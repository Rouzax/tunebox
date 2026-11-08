import assert from 'node:assert/strict';
import {test} from 'node:test';
import {HEADER, SPECTRUM_BINS, createRenderer} from '../js/gpurender.js';
import {fakeGl} from './fakegl.js';

const QUIET = {bass: 0, mid: 0, high: 0};
const SPECTRUM = new Uint8Array(SPECTRUM_BINS);
const source = () => `${HEADER}void main() { outColor = vec4(1.0); }`;
const PASSES = {single: 1, feedback: 2, field: 2};

function drawn(kind, frameCount) {
    const gl = fakeGl();
    const renderer = createRenderer(gl, kind, Array.from({length: PASSES[kind]}, source));
    renderer.resize(64, 32);
    for (let i = 0; i < frameCount; i++) {
        renderer.draw(i / 60, QUIET, SPECTRUM);
    }
    return gl;
}

test('no pass reads the texture it draws into, and every frame ends on the screen', () => {
    for (const kind of Object.keys(PASSES)) {
        const gl = drawn(kind, 4);
        assert.equal(gl.draws.length, 4 * PASSES[kind], kind);
        for (const draw of gl.draws) {
            if (draw.writing !== null) {
                assert.ok(!Object.values(draw.units).includes(draw.writing), `${kind} reads what it draws into`);
            }
        }
        for (let i = PASSES[kind] - 1; i < gl.draws.length; i += PASSES[kind]) {
            assert.equal(gl.draws[i].writing, null, kind);
        }
    }
});

test('the feedback pass reads the frame before and the screen shows the frame just drawn', () => {
    const [warp1, show1, warp2, show2] = drawn('feedback', 2).draws;
    assert.equal(show1.units[3], warp1.writing);
    assert.equal(warp2.units[3], warp1.writing);
    assert.equal(show2.units[3], warp2.writing);
    assert.notEqual(warp2.writing, warp1.writing);
});

test('the field pass is shaded by the second pass', () => {
    const [fill, shade] = drawn('field', 1).draws;
    assert.ok(fill.writing);
    assert.equal(fill.units[3], null);
    assert.equal(shade.units[3], fill.writing);
});

test('a new cover fades in over the one before', () => {
    const gl = fakeGl();
    const renderer = createRenderer(gl, 'single', [source()]);
    renderer.resize(8, 8);
    const first = {name: 'first'};
    const second = {name: 'second'};
    renderer.cover(first);
    renderer.cover(second);
    const palette = new Float32Array([1, 0, 0, 0, 1, 0, 0, 0, 1]);
    renderer.draw(0, QUIET, SPECTRUM, {palette, fade: 0.25});
    const [draw] = gl.draws;
    assert.equal(gl.uploads.get(draw.units[1]), second);
    assert.equal(gl.uploads.get(draw.units[2]), first);
    assert.equal(gl.uniforms.uFade, 0.25);
    assert.deepEqual(gl.uniforms.uPal, Array.from(palette));
    assert.deepEqual(gl.uniforms.uRes, [8, 8]);
});

test('a program that does not build throws with its log', () => {
    assert.throws(() => createRenderer(fakeGl({buildFails: true}), 'single', [source()]), /fake compile error/);
});
