import assert from 'node:assert/strict';
import {afterEach, beforeEach, test} from 'node:test';
import {SHUFFLE_S} from '../js/milkdropcore.js';
import {createMilkdrop} from '../js/milkdrop.js';

// A page, a clock and Butterchurn made of plain objects: frames run when
// the test says, timers when the test moves the clock.
let timers;
let now;
let canvases;

function fakeCanvas() {
    return {hidden: false, width: 0, height: 0, clientWidth: 800, clientHeight: 450, dataset: {}, className: ''};
}

beforeEach(() => {
    timers = [];
    now = 0;
    canvases = [];
    globalThis.document = {createElement: () => {
        const canvas = fakeCanvas();
        canvases.push(canvas);
        return canvas;
    }};
    globalThis.window = {
        devicePixelRatio: 1,
        setTimeout(fn, ms) {
            timers.push({at: now + ms, fn});
            return timers.length;
        },
        clearTimeout(id) {
            if (timers[id - 1]) {
                timers[id - 1].fn = null;
            }
        },
    };
    globalThis.requestAnimationFrame = () => 1;
    globalThis.cancelAnimationFrame = () => {};
});

afterEach(() => {
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.requestAnimationFrame;
    delete globalThis.cancelAnimationFrame;
});

async function advance(ms) {
    now += ms;
    for (const timer of timers) {
        if (timer.fn && timer.at <= now) {
            const fn = timer.fn;
            timer.fn = null;
            fn();
        }
    }
    await settle();
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

function world(options = {}) {
    const loads = [];
    const failures = [];
    const node = {connect: (to) => to, disconnect() {}};
    const visualizer = {
        connectAudio() {},
        disconnectAudio() {},
        setRendererSize() {},
        render() {},
        loadPreset(preset, blend) {
            if (options.broken && options.broken.includes(preset.file)) {
                return Promise.reject(new Error('shader'));
            }
            loads.push({file: preset.file, blend});
            return Promise.resolve();
        },
    };
    const motion = {
        connectedSources: [node],
        audioCtx: {
            sampleRate: 48000,
            destination: {},
            createAnalyser: () => ({fftSize: 0, frequencyBinCount: 256, connect: (to) => to, disconnect() {}, getByteFrequencyData() {}}),
            createGain: () => ({gain: {value: 1}, connect: (to) => to}),
        },
    };
    const root = {querySelector: () => ({parentNode: {insertBefore() {}}})};
    let release = null;
    const gate = options.slowStart ? new Promise((resolve) => {
        release = resolve;
    }) : Promise.resolve();
    const engine = createMilkdrop({
        motion, root, onLevel() {}, onFail: (why) => failures.push(why),
        loadButterchurn: () => gate.then(() => {
            if (options.noButterchurn) {
                throw new Error('no WebGL');
            }
            return {createVisualizer: () => visualizer};
        }),
        fetchPreset: (file) => (options.offline && options.offline(file)
            ? Promise.reject(new Error('offline')) : Promise.resolve({file})),
    });
    return {engine, loads, failures, release: () => release()};
}

const SHUFFLE = {id: 'md-shuffle', shuffle: true};
const HURRICANE = {id: 'md-geiss-hurricane'};

test('applying the same style again leaves the preset showing', async () => {
    const {engine, loads} = world();
    engine.start(HURRICANE, false);
    await settle();
    engine.start(HURRICANE, false);
    engine.start(HURRICANE, false);
    await settle();
    assert.equal(loads.length, 1);
    engine.start(SHUFFLE, false);
    await settle();
    engine.start(SHUFFLE, false);
    await advance(10000);
    // A new cover is no reason to move on: still one Shuffle preset.
    assert.equal(loads.length, 2);
    await advance(SHUFFLE_S * 1000);
    assert.equal(loads.length, 3);
});

test('a style left while Butterchurn still loads leaves no canvas showing', async () => {
    const {engine, release, loads} = world({slowStart: true});
    engine.start(HURRICANE, false);
    engine.stop();
    release();
    await settle();
    await settle();
    assert.equal(loads.length, 0);
    assert.ok(canvases.every((canvas) => canvas.hidden), 'a canvas shows');
});

test('Butterchurn that failed once fails again on the next pick, so the player can fall back', async () => {
    const {engine, failures} = world({noButterchurn: true});
    engine.start(SHUFFLE, false);
    await settle();
    engine.stop();
    engine.start(HURRICANE, false);
    await settle();
    assert.deepEqual(failures, ['butterchurn', 'butterchurn']);
    assert.ok(canvases.every((canvas) => canvas.hidden));
});

test('a preset that cannot be fetched stays in Shuffle and is tried again later', async () => {
    let down = true;
    const {engine, loads} = world({offline: () => down});
    engine.start(SHUFFLE, false);
    await settle();
    await settle();
    assert.equal(loads.length, 0);
    down = false;
    await advance(SHUFFLE_S * 1000);
    await settle();
    assert.equal(loads.length, 1);
});

test('a preset that will not compile leaves Shuffle for good', async () => {
    const {engine, loads} = world({broken: ['geiss-hurricane.json']});
    engine.start(SHUFFLE, false);
    for (let i = 0; i < 40; i++) {
        await advance(SHUFFLE_S * 1000);
    }
    assert.ok(loads.length > 17);
    assert.ok(loads.every((load) => load.file !== 'geiss-hurricane.json'));
});
