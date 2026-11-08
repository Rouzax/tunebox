import assert from 'node:assert/strict';
import {afterEach, beforeEach, test} from 'node:test';
import {createGpuStyles} from '../js/gpustyles.js';
import {STYLES} from '../js/vizcore.js';
import {fakeGl} from './fakegl.js';

const TUNNEL = STYLES.find((s) => s.id === 'tunnel');
const INK = STYLES.find((s) => s.id === 'ink');

let pending;
let canvases;
let contexts;
let drawn;

beforeEach(() => {
    pending = null;
    canvases = [];
    contexts = [];
    drawn = [];
    globalThis.window = {devicePixelRatio: 1};
    globalThis.requestAnimationFrame = (fn) => {
        pending = fn;
        return 1;
    };
    globalThis.cancelAnimationFrame = () => {
        pending = null;
    };
    globalThis.getComputedStyle = () => ({
        getPropertyValue: (name) => ({'--art-1': 'rgb(255, 0, 0)', '--art-2': 'rgb(0, 255, 0)', '--art-3': 'rgb(0, 0, 255)'})[name] || '',
    });
});

afterEach(() => {
    for (const name of ['document', 'window', 'requestAnimationFrame', 'cancelAnimationFrame', 'getComputedStyle']) {
        delete globalThis[name];
    }
});

// Runs the engine's frames from `from` to `to` ms at fps.
function frames(from, to, fps) {
    for (let t = from; t <= to && pending; t += 1000 / fps) {
        const fn = pending;
        pending = null;
        fn(t);
    }
}

function node() {
    const into = [];
    return {into, connect(to) {
        into.push(to);
        return to;
    }, disconnect() {}};
}

function world({context = true, buildFails = false, held = false} = {}) {
    const failures = [];
    globalThis.document = {createElement: () => {
        const listeners = {};
        const canvas = {
            width: 0, height: 0, clientWidth: 800, clientHeight: 450, dataset: {}, className: '', removed: false, listeners,
            getContext(type) {
                if (type === '2d') {
                    return {clearRect() {}, drawImage(image) {
                        if (image.broken) {
                            throw new Error('not decodable');
                        }
                        drawn.push(image);
                    }};
                }
                if (!context) {
                    return null;
                }
                const gl = fakeGl({buildFails});
                contexts.push(gl);
                return gl;
            },
            addEventListener(type, fn) {
                listeners[type] = fn;
            },
            removeEventListener(type) {
                delete listeners[type];
            },
            remove() {
                canvas.removed = true;
            },
        };
        canvases.push(canvas);
        return canvas;
    }};
    const source = node();
    const motion = {
        connectedSources: [source],
        audioCtx: {
            sampleRate: 48000,
            destination: {},
            createAnalyser: () => ({fftSize: 0, frequencyBinCount: 256, connect: (to) => to, disconnect() {}, getByteFrequencyData() {}}),
            createGain: () => ({gain: {value: 1}, connect: (to) => to}),
        },
    };
    const root = {querySelector: () => ({parentNode: {insertBefore() {}}})};
    const engine = createGpuStyles({motion, root, onLevel() {}, onFail: (why) => failures.push(why), held: () => held});
    const shown = () => canvases.filter((canvas) => canvas.className === 'car-fx car-gpu' && !canvas.removed);
    return {engine, failures, motion, source, shown};
}

test('a GPU style draws on one canvas at its own scale, and the next style keeps the context', () => {
    const {engine, shown} = world();
    engine.start(TUNNEL, false);
    frames(0, 100, 60);
    const [canvas] = shown();
    assert.deepEqual([canvas.width, canvas.height, canvas.dataset.gpuScale], [800, 450, '1']);
    assert.ok(Number(canvas.dataset.gpuFrames) > 0);
    engine.start(INK, false);
    frames(200, 300, 60);
    assert.deepEqual(shown(), [canvas]);
    assert.deepEqual([canvas.width, canvas.height, canvas.dataset.gpuScale], [400, 225, '2']);
    assert.equal(contexts.length, 1);
});

test('applying the same style again builds nothing new', () => {
    const {engine} = world();
    engine.start(TUNNEL, false);
    engine.start(TUNNEL, false);
    assert.equal(contexts[0].programs, 2);
});

test('stop gives the context back and takes the canvas away', () => {
    const {engine, shown} = world();
    engine.start(TUNNEL, false);
    frames(0, 100, 60);
    engine.stop();
    assert.equal(contexts[0].lost, true);
    assert.deepEqual(shown(), []);
    assert.equal(pending, null);
    engine.start(TUNNEL, false);
    assert.equal(contexts.length, 2);
    assert.equal(shown().length, 1);
});

test('low power starts one step smaller', () => {
    const {engine, shown} = world();
    engine.start(TUNNEL, true);
    frames(0, 50, 60);
    assert.equal(shown()[0].dataset.gpuScale, '2');
    engine.start(INK, true);
    frames(100, 150, 60);
    assert.equal(shown()[0].dataset.gpuScale, '3');
});

test('below 50 fps the scale steps down to a third and never back up', () => {
    const {engine, shown} = world();
    engine.start(TUNNEL, false);
    frames(0, 3100, 40);
    assert.equal(shown()[0].dataset.gpuScale, '2');
    frames(3150, 6300, 40);
    assert.equal(shown()[0].dataset.gpuScale, '3');
    frames(6350, 20000, 60);
    assert.equal(shown()[0].dataset.gpuScale, '3');
    engine.stop();
    engine.start(TUNNEL, false);
    frames(20100, 20200, 60);
    assert.equal(shown()[0].dataset.gpuScale, '3');
});

test('lab mode holds the scale', () => {
    const {engine, shown} = world({held: true});
    engine.start(TUNNEL, false);
    frames(0, 10000, 30);
    assert.equal(shown()[0].dataset.gpuScale, '1');
});

test('a style that does not build is reported and leaves no context', () => {
    const {engine, failures, shown} = world({buildFails: true});
    engine.start(TUNNEL, false);
    assert.deepEqual(failures, ['gpu']);
    assert.equal(contexts[0].lost, true);
    assert.deepEqual(shown(), []);
    assert.equal(pending, null);
});

test('no WebGL 2 context is reported', () => {
    const {engine, failures, shown} = world({context: false});
    engine.start(TUNNEL, false);
    assert.deepEqual(failures, ['gpu']);
    assert.deepEqual(shown(), []);
});

test('an unexpected context loss is reported', () => {
    const {engine, failures, shown} = world();
    engine.start(TUNNEL, false);
    frames(0, 100, 60);
    shown()[0].listeners.webglcontextlost({preventDefault() {}});
    assert.deepEqual(failures, ['gpu']);
    assert.equal(pending, null);
    assert.deepEqual(shown(), []);
});

test('the cover the player shows is the texture, and a new one replaces it', () => {
    const {engine} = world();
    const first = {name: 'first'};
    const second = {name: 'second'};
    engine.cover(first);
    engine.start(TUNNEL, false);
    assert.deepEqual(drawn, [first]);
    const uploads = () => [...contexts[0].uploads.values()].length;
    assert.equal(uploads(), 1);
    engine.cover(second);
    assert.deepEqual(drawn, [first, second]);
    assert.equal(uploads(), 2);
});

test('a cover that cannot be drawn keeps the one before', () => {
    const {engine} = world();
    engine.cover({name: 'first'});
    engine.start(TUNNEL, false);
    engine.cover({broken: true});
    assert.equal([...contexts[0].uploads.values()].length, 1);
});

test('the shaders get the cover palette from the page', () => {
    const {engine} = world();
    engine.start(TUNNEL, false);
    frames(0, 20, 60);
    assert.deepEqual(contexts[0].uniforms.uPal, [1, 0, 0, 0, 1, 0, 0, 0, 1]);
});

test('reconnect reads the new sources', () => {
    const {engine, motion} = world();
    engine.start(TUNNEL, false);
    const next = node();
    motion.connectedSources = [next];
    engine.reconnect();
    assert.equal(next.into.length, 1);
});
