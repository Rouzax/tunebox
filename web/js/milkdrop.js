// The player's MilkDrop: Butterchurn, loaded on the first MilkDrop pick
// only, drawing a preset at half size (a third in low power) on its own
// canvas, fed from the sources audioMotion reads. Presets run as
// WebAssembly only: the page's policy refuses their JavaScript form.
import {bands} from './gpucore.js';
import {BLEND_S, PRESETS, PRESET_DIR, SHUFFLE_S, createCycle, followBass, renderSize} from './milkdropcore.js';

const BUTTERCHURN = '../vendor/butterchurn-3.0.0-beta.5/butterchurn.min.js';
const FFT_SIZE = 512;

// Asked once per page view: a context made and given back at once.
export function webgl2Available() {
    try {
        const gl = document.createElement('canvas').getContext('webgl2');
        if (!gl) {
            return false;
        }
        const lose = gl.getExtension('WEBGL_lose_context');
        if (lose) {
            lose.loseContext();
        }
        return true;
    } catch (error) {
        return false;
    }
}

// onLevel(glow) drives the cover glow; onFail('butterchurn') when the
// engine cannot start, onFail('preset') when a preset picked on its own
// cannot load.
export function createMilkdrop({motion, root, onLevel, onFail}) {
    let canvas = null;
    let visualizer = null;
    let starting = null;
    let broken = false;
    let connected = [];
    let analyser = null;
    let bins = null;
    let peak = 0.2;
    let frame = 0;
    let frames = 0;
    let timer = 0;
    let scale = 2;
    let size = {cssWidth: 0, cssHeight: 0};
    let current = null;
    // Bumped by every start and stop, so a preset still loading for a
    // style that is gone is not shown.
    let run = 0;
    const cycle = createCycle(PRESETS.map((preset) => preset.id), Math.random);

    function ensureCanvas() {
        if (!canvas) {
            const host = root.querySelector('#analyzer');
            canvas = document.createElement('canvas');
            // car-fx: placed, faded and dimmed like the 2D effects.
            canvas.className = 'car-fx car-md';
            host.parentNode.insertBefore(canvas, host);
        }
        return canvas;
    }

    function connect() {
        connected = motion.connectedSources.slice();
        const audioCtx = motion.audioCtx;
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = FFT_SIZE;
        bins = new Uint8Array(analyser.frequencyBinCount);
        const mute = audioCtx.createGain();
        mute.gain.value = 0;
        analyser.connect(mute).connect(audioCtx.destination);
        for (const source of connected) {
            visualizer.connectAudio(source);
            source.connect(analyser);
        }
    }

    function disconnect() {
        for (const source of connected) {
            try {
                visualizer.disconnectAudio(source);
                source.disconnect(analyser);
            } catch (error) {
                // A source already gone with its audio element.
            }
        }
        if (analyser) {
            analyser.disconnect();
            analyser = null;
        }
        connected = [];
    }

    function load() {
        if (visualizer || broken) {
            return Promise.resolve(visualizer);
        }
        if (!starting) {
            starting = import(BUTTERCHURN).then((module) => {
                const element = ensureCanvas();
                const {width, height} = renderSize(element.clientWidth, element.clientHeight, window.devicePixelRatio || 1, scale);
                element.width = width;
                element.height = height;
                visualizer = module.default.createVisualizer(motion.audioCtx, element, {
                    width, height, pixelRatio: 1, textureRatio: 1, onlyUseWASM: true,
                });
                connect();
                return visualizer;
            }).catch(() => {
                broken = true;
                visualizer = null;
                onFail('butterchurn');
                return null;
            });
        }
        return starting;
    }

    function resize() {
        const cssWidth = canvas.clientWidth;
        const cssHeight = canvas.clientHeight;
        if (cssWidth === size.cssWidth && cssHeight === size.cssHeight && size.scale === scale) {
            return;
        }
        size = {cssWidth, cssHeight, scale};
        const {width, height} = renderSize(cssWidth, cssHeight, window.devicePixelRatio || 1, scale);
        canvas.width = width;
        canvas.height = height;
        visualizer.setRendererSize(width, height);
    }

    function loop() {
        frame = requestAnimationFrame(loop);
        resize();
        visualizer.render();
        frames += 1;
        canvas.dataset.mdFrames = String(frames);
        analyser.getByteFrequencyData(bins);
        const followed = followBass(bands(bins, motion.audioCtx.sampleRate, FFT_SIZE).bass, peak);
        peak = followed.peak;
        onLevel(followed.glow);
    }

    // Resolves true once the preset shows; a failed one leaves the cycle.
    function show(id, blendS, mine) {
        const preset = PRESETS.filter((candidate) => candidate.id === id)[0];
        return window.fetch(PRESET_DIR + preset.file)
            .then((response) => response.json())
            .then((data) => {
                if (mine !== run) {
                    return false;
                }
                return Promise.resolve(visualizer.loadPreset(data, blendS)).then(() => {
                    current = id;
                    return true;
                });
            })
            .catch(() => {
                cycle.drop(id);
                return false;
            });
    }

    function shuffle(mine, blendS) {
        const next = cycle.next(current);
        if (!next || mine !== run) {
            return;
        }
        show(next, blendS, mine).then((shown) => {
            if (mine !== run) {
                return;
            }
            if (!shown) {
                shuffle(mine, blendS);
                return;
            }
            timer = window.setTimeout(() => shuffle(mine, BLEND_S), SHUFFLE_S * 1000);
        });
    }

    function halt() {
        run += 1;
        cancelAnimationFrame(frame);
        frame = 0;
        window.clearTimeout(timer);
        timer = 0;
    }

    return {
        start(style, lowPower) {
            halt();
            const mine = run;
            scale = lowPower ? 3 : 2;
            load().then((ready) => {
                if (!ready || mine !== run) {
                    return;
                }
                canvas.hidden = false;
                if (!frame) {
                    frame = requestAnimationFrame(loop);
                }
                if (style.shuffle) {
                    shuffle(mine, current ? BLEND_S : 0);
                    return;
                }
                show(style.id, current ? BLEND_S : 0, mine).then((shown) => {
                    if (!shown && mine === run) {
                        onFail('preset');
                    }
                });
            });
        },
        stop() {
            halt();
            if (canvas) {
                // Its last frame would stay on screen under the next style.
                canvas.hidden = true;
            }
        },
        // A new audio element came in; read from it from now on.
        reconnect() {
            if (visualizer) {
                disconnect();
                connect();
            }
        },
    };
}
