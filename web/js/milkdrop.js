// The player's MilkDrop: Butterchurn, loaded on the first MilkDrop pick
// only, drawing a preset at half size (a third in low power, or once the
// device cannot keep 50 fps) on its own canvas, fed from the sources audioMotion reads. Presets run as
// WebAssembly only: the page's policy refuses their JavaScript form.
import {bands} from './gpucore.js';
import {MAX_SCALE, createPace, renderSize, startScale} from './gpuframe.js';
import {BLEND_S, PRESETS, PRESET_DIR, SHUFFLE_S, createCycle, followBass} from './milkdropcore.js';

const BUTTERCHURN = '../vendor/butterchurn-3.0.0-beta.5/butterchurn.min.js';
const FFT_SIZE = 512;
// Half size: every shipped preset passed the car's GPU test there.
const BASE_SCALE = 2;

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

const importButterchurn = () => import(BUTTERCHURN).then((module) => module.default);
const fetchJson = (file) => window.fetch(PRESET_DIR + file).then((response) => response.json());

// onLevel(glow) drives the cover glow; onFail('butterchurn') whenever a
// MilkDrop style is asked for and the engine cannot start, onFail('preset')
// when a preset picked on its own cannot show. loadButterchurn and
// fetchPreset are the network, replaceable in tests. held() true keeps the
// size (lab mode measures the start scale).
export function createMilkdrop({motion, root, onLevel, onFail, held = () => false, loadButterchurn = importButterchurn, fetchPreset = fetchJson}) {
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
    let floor = 1;
    let pace = createPace();
    let size = {cssWidth: 0, cssHeight: 0};
    let current = null;
    // The style and scale asked for last, while MilkDrop is on; asking for
    // the same again (a new cover re-applies the style) changes nothing.
    let showing = null;
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
            // Shown once a start still wanted gets its engine.
            canvas.hidden = true;
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
            starting = loadButterchurn().then((butterchurn) => {
                const element = ensureCanvas();
                const {width, height} = renderSize(element.clientWidth, element.clientHeight, window.devicePixelRatio || 1, scale);
                element.width = width;
                element.height = height;
                visualizer = butterchurn.createVisualizer(motion.audioCtx, element, {
                    width, height, pixelRatio: 1, textureRatio: 1, onlyUseWASM: true,
                });
                connect();
                return visualizer;
            }).catch(() => {
                broken = true;
                visualizer = null;
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

    function loop(now) {
        frame = requestAnimationFrame(loop);
        if (pace.frame(now) && !held() && scale < MAX_SCALE) {
            scale += 1;
            floor = scale;
            showing.scale = scale;
        }
        resize();
        visualizer.render();
        frames += 1;
        canvas.dataset.mdFrames = String(frames);
        analyser.getByteFrequencyData(bins);
        const followed = followBass(bands(bins, motion.audioCtx.sampleRate, FFT_SIZE).bass, peak);
        peak = followed.peak;
        onLevel(followed.glow);
    }

    // Resolves 'shown', 'offline' (the file did not come; worth another
    // try later) or 'broken' (it would not compile: it leaves the cycle).
    function show(id, blendS, mine) {
        const preset = PRESETS.filter((candidate) => candidate.id === id)[0];
        return fetchPreset(preset.file).then((data) => {
            if (mine !== run) {
                return 'stale';
            }
            return Promise.resolve(visualizer.loadPreset(data, blendS)).then(() => {
                current = id;
                return 'shown';
            }, () => {
                cycle.drop(id);
                return 'broken';
            });
        }, () => 'offline');
    }

    function shuffle(mine, blendS) {
        const next = cycle.next(current);
        if (!next || mine !== run) {
            return;
        }
        show(next, blendS, mine).then((outcome) => {
            if (mine !== run) {
                return;
            }
            if (outcome === 'broken') {
                shuffle(mine, blendS);
                return;
            }
            // Shown, or offline: the next try comes after the usual wait,
            // so a dropped connection does not spin through the cycle.
            timer = window.setTimeout(() => shuffle(mine, BLEND_S), SHUFFLE_S * 1000);
        });
    }

    function halt() {
        run += 1;
        showing = null;
        cancelAnimationFrame(frame);
        frame = 0;
        window.clearTimeout(timer);
        timer = 0;
    }

    return {
        start(style, lowPower) {
            const wanted = startScale(BASE_SCALE, lowPower, floor);
            if (showing && showing.id === style.id && showing.scale === wanted) {
                return;
            }
            halt();
            const mine = run;
            scale = wanted;
            pace = createPace();
            showing = {id: style.id, scale: wanted};
            load().then((ready) => {
                if (mine !== run) {
                    return;
                }
                if (!ready) {
                    showing = null;
                    onFail('butterchurn');
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
                show(style.id, current ? BLEND_S : 0, mine).then((outcome) => {
                    if (outcome !== 'shown' && outcome !== 'stale' && mine === run) {
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
