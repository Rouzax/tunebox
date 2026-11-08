// The GPU test page (lab-gpu.html): what WebGL costs on this device. Off
// first, then the probe shaders of gpuprobe.js at three render scales,
// then each candidate Butterchurn preset at half size, each on the lab's
// silent test signal, with the setup cost of each and a verdict per
// preset. The results end in a table and one QR code for a photo. Nothing
// is stored and nothing is sent.
import qrcode from '../vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {PASS, PRESETS, SWITCH_WATCH_S, bands, capMs, describeSeconds, gpuPlan, planSeconds, presetVerdict, shrink, worstGapMs} from './gpucore.js';
import {bindFullscreen, isFullscreen} from './fullscreen.js';
import {createProbe} from './gpuprobe.js';
import {SPECTRUM_BINS} from './gpurender.js';
import {QR_LEVEL, gpuPayload} from './gpureport.js';
import {labSignal} from './labsignal.js';
import {frameStats, testWindowS} from './labstats.js';
import {qrMatrix, qrSvg} from './qr.js';

const BUTTERCHURN = '../vendor/butterchurn-3.0.0-beta.5/butterchurn.min.js';
const PRESET_DIR = 'vendor/butterchurn-presets-3.0.0-beta.4/';
const SEED = 1;
// As in lab mode: the first tenth of each window is left out, since a
// switch costs a few slow frames that say nothing about the cell itself.
const WARMUP_SHARE = 0.1;
const LIVE_MS = 500;
// 256 bins, folded into the probes' SPECTRUM_BINS.
const FFT_SIZE = 512;
const COVER_SIZE = 256;
const SCALE_LABELS = {1: '1', 2: '1/2', 3: '1/3'};

const root = document.querySelector('.gpu');
const probeCanvas = root.querySelector('.gpu-probe');
const presetCanvas = root.querySelector('.gpu-preset');
const name = root.querySelector('.gpu-test');
const fps = root.querySelector('.gpu-fps');
const note = root.querySelector('.gpu-note');
const start = root.querySelector('.gpu-start');
const show = root.querySelector('.gpu-show');
const back = root.querySelector('.gpu-back');
const results = root.querySelector('.gpu-results');
const environmentLine = root.querySelector('.gpu-env');
const table = root.querySelector('.gpu-table');
const qrBox = root.querySelector('.gpu-qr');
const close = root.querySelector('.gpu-close');
const full = root.querySelector('.gpu-full');
const summary = root.querySelector('.gpu-summary');

const params = new URLSearchParams(window.location.search);
const windowS = testWindowS(params.get('labsec'));
const plan = gpuPlan();
if (params.get('labsec') !== null) {
    back.href = `./?lab=1&labsec=${encodeURIComponent(params.get('labsec'))}`;
}

function element(tag, text) {
    const el = document.createElement(tag);
    if (text) {
        el.textContent = text;
    }
    return el;
}

function readEnvironment() {
    try {
        return window.tuneboxEnvironment ? window.tuneboxEnvironment() : null;
    } catch (error) {
        return null;
    }
}

// view: the window the run measured, and whether it was full screen.
function describeEnvironment(env, view) {
    if (!env) {
        return 'Environment: unknown';
    }
    const parts = [env.webgl2 ? 'WebGL 2' : env.webgl1 ? 'WebGL 1 only' : 'no WebGL'];
    if (env.renderer) {
        parts.push(env.renderer);
    }
    parts.push(`${env.cores || '?'} cores`, `screen ${env.screen.width} x ${env.screen.height} @${env.dpr}x`,
        `window ${view.width} x ${view.height}${view.fullscreen ? ' (full screen)' : ''}`);
    return parts.join(', ');
}

function currentView() {
    return {width: window.innerWidth, height: window.innerHeight, fullscreen: isFullscreen()};
}

const environment = readEnvironment();
environmentLine.textContent = describeEnvironment(environment, currentView());
// The run keeps the window it started in: leaving full screen during it
// does not change what the results say it measured.
let startView = currentView();
const labelFull = () => {
    full.textContent = isFullscreen() ? 'Leave full screen' : 'Full screen';
};
if (bindFullscreen(full)) {
    document.addEventListener('fullscreenchange', labelFull);
    document.addEventListener('webkitfullscreenchange', labelFull);
}
note.textContent = `Silent test signal, ${describeSeconds(planSeconds(plan, windowS))}. Press Start.`;
root.dataset.gpu = 'idle';

let state = 'idle';
let stopRequested = false;
let cutWait = null;
// Frame timestamps: the last second for the live rate, the measured window
// while one runs, and the frames around a preset switch.
let recent = [];
let recording = null;
let watching = null;
let lastFrame = 0;
let drawFrame = null;

function sync() {
    start.disabled = state === 'unavailable';
    start.textContent = state === 'testing' ? 'Stop' : state === 'done' ? 'Run again' : 'Start';
    show.disabled = !results.dataset.payload;
    root.dataset.gpu = state;
}

function wait(ms) {
    return new Promise((resolve) => {
        if (stopRequested) {
            resolve();
            return;
        }
        const timer = window.setTimeout(resolve, ms);
        cutWait = () => {
            window.clearTimeout(timer);
            resolve();
        };
    });
}

function tick(now) {
    window.requestAnimationFrame(tick);
    recent.push(now);
    while (recent.length > 1 && recent[0] < now - 1000) {
        recent.shift();
    }
    if (recording) {
        recording.push(now);
    }
    if (watching) {
        watching.push(now);
    }
    lastFrame = now;
    if (drawFrame) {
        drawFrame(now);
    }
}

function showLive() {
    fps.textContent = `${Math.round(frameStats(recent).avg_fps)} fps`;
}

// Runs inside the tap: the AudioContext must start from a user gesture.
// The output is muted: an analyser with no path to the destination may
// never be pulled.
function buildSignal() {
    const Context = window.AudioContext || window.webkitAudioContext;
    const ctx = new Context();
    const samples = labSignal(ctx.sampleRate, SEED);
    const buffer = ctx.createBuffer(2, samples.length, ctx.sampleRate);
    buffer.copyToChannel(samples, 0);
    buffer.copyToChannel(samples, 1);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.loop = true;
    const analyser = ctx.createAnalyser();
    analyser.fftSize = FFT_SIZE;
    const mute = ctx.createGain();
    mute.gain.value = 0;
    source.connect(analyser);
    analyser.connect(mute).connect(ctx.destination);
    source.start();
    if (ctx.state === 'suspended') {
        ctx.resume().catch(() => {});
    }
    return {ctx, source, analyser};
}

// The cover the player shows when a song has none, drawn into a canvas for
// the tunnel's walls; a gradient if the image cannot load.
function loadCover() {
    const canvas = document.createElement('canvas');
    canvas.width = COVER_SIZE;
    canvas.height = COVER_SIZE;
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createLinearGradient(0, 0, COVER_SIZE, COVER_SIZE);
    gradient.addColorStop(0, '#124c86');
    gradient.addColorStop(1, '#62a8ea');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, COVER_SIZE, COVER_SIZE);
    return new Promise((resolve) => {
        const image = new Image();
        image.addEventListener('load', () => {
            ctx.drawImage(image, 0, 0, COVER_SIZE, COVER_SIZE);
            resolve(canvas);
        });
        image.addEventListener('error', () => resolve(canvas));
        image.src = 'icons/cover.svg';
    });
}

function renderSize(scale, limit) {
    const dpr = window.devicePixelRatio || 1;
    return {
        width: Math.max(1, Math.min(limit, Math.round((window.innerWidth * dpr) / scale))),
        height: Math.max(1, Math.min(limit, Math.round((window.innerHeight * dpr) / scale))),
    };
}

function showCanvas(which) {
    probeCanvas.hidden = which !== probeCanvas;
    presetCanvas.hidden = which !== presetCanvas;
}

async function measure() {
    const warmupMs = windowS * 1000 * WARMUP_SHARE;
    await wait(warmupMs);
    recording = [];
    await wait(windowS * 1000 - warmupMs);
    const stats = frameStats(recording);
    recording = null;
    return stats;
}

async function runTest(signal) {
    const measured = [];
    const setup = {};
    const failed = {};
    const gl = probeCanvas.getContext('webgl2', {alpha: false, antialias: false, depth: false, stencil: false});
    if (!gl) {
        failed.gl2 = 'missing';
        note.textContent = 'This browser has no WebGL 2; the GPU test needs it.';
        return {measured, setup, failed};
    }
    probeCanvas.addEventListener('webglcontextlost', () => {
        failed.gl = 'lost';
    });
    const limit = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    const cover = await loadCover();
    const bins = new Uint8Array(signal.analyser.frequencyBinCount);
    const spectrum = new Uint8Array(SPECTRUM_BINS);
    const startedAt = performance.now();
    let probe = null;
    let visualizer = null;
    let presetReady = false;

    for (let n = 0; n < plan.length && !stopRequested; n++) {
        const cell = plan[n];
        if (failed[cell.id] || failed.gl) {
            continue;
        }
        name.textContent = `${cell.label}, scale ${SCALE_LABELS[cell.scale]}`;
        note.textContent = `test ${n + 1}/${plan.length}`;
        const size = renderSize(cell.scale, limit);
        // Nothing draws while the next cell is set up: the old cell's
        // resources may be gone, and an await may come first.
        drawFrame = null;
        if (cell.kind === 'off') {
            showCanvas(probeCanvas);
            gl.bindFramebuffer(gl.FRAMEBUFFER, null);
            gl.clearColor(0, 0, 0, 1);
            gl.clear(gl.COLOR_BUFFER_BIT);
        } else if (cell.kind === 'probe') {
            if (!probe || probe.id !== cell.id) {
                if (probe) {
                    probe.made.dispose();
                }
                try {
                    probe = {id: cell.id, made: createProbe(gl, cell.id, cover)};
                    setup[cell.id] = probe.made.setupMs;
                } catch (error) {
                    probe = null;
                    failed[cell.id] = 'compile';
                    continue;
                }
            }
            probeCanvas.width = size.width;
            probeCanvas.height = size.height;
            probe.made.resize(size.width, size.height);
            showCanvas(probeCanvas);
            const made = probe.made;
            drawFrame = (now) => {
                signal.analyser.getByteFrequencyData(bins);
                made.draw((now - startedAt) / 1000, bands(bins, signal.ctx.sampleRate, FFT_SIZE), shrink(bins, spectrum));
            };
        } else {
            if (probe) {
                probe.made.dispose();
                probe = null;
            }
            if (!visualizer) {
                try {
                    const butterchurn = (await import(BUTTERCHURN)).default;
                    presetCanvas.width = size.width;
                    presetCanvas.height = size.height;
                    // Equations only as WebAssembly: the page's policy refuses
                    // their JavaScript form anyway.
                    visualizer = butterchurn.createVisualizer(signal.ctx, presetCanvas, {
                        width: size.width, height: size.height, pixelRatio: 1, textureRatio: 1, onlyUseWASM: true,
                    });
                    visualizer.connectAudio(signal.source);
                } catch (error) {
                    failed.bc = 'butterchurn';
                    PRESETS.forEach((preset) => {
                        failed[preset.id] = 'butterchurn';
                    });
                    continue;
                }
            }
            presetCanvas.width = size.width;
            presetCanvas.height = size.height;
            visualizer.setRendererSize(size.width, size.height);
            showCanvas(presetCanvas);
            const shown = visualizer;
            drawFrame = () => {
                if (presetReady) {
                    shown.render();
                }
            };
            if (cell.load) {
                try {
                    const response = await window.fetch(PRESET_DIR + cell.file);
                    const preset = await response.json();
                    // From the last frame before the switch, so work done
                    // before loadPreset first yields counts too.
                    watching = [lastFrame];
                    const switched = performance.now();
                    await visualizer.loadPreset(preset, 0);
                    const load = performance.now() - switched;
                    presetReady = true;
                    await wait(SWITCH_WATCH_S * 1000);
                    setup[cell.id] = {load, worst: worstGapMs(watching)};
                    watching = null;
                } catch (error) {
                    watching = null;
                    failed[cell.id] = 'load';
                    continue;
                }
            }
        }
        const stats = await measure();
        // A window cut short by Stop is left out.
        if (!stopRequested) {
            measured.push(Object.assign({kind: cell.kind, id: cell.id, label: cell.label, scale: cell.scale}, stats));
        }
    }
    drawFrame = null;
    if (probe) {
        probe.made.dispose();
    }
    if (visualizer) {
        visualizer.loseGLContext();
    }
    const lose = gl.getExtension('WEBGL_lose_context');
    if (lose) {
        lose.loseContext();
    }
    return {measured, setup, failed};
}

// Milliseconds capped as in the payload: 999 reads "999 or more".
function setupText(id, setup) {
    const value = setup[id];
    if (typeof value === 'number') {
        return String(capMs(value));
    }
    return value ? `${capMs(value.load)} / ${capMs(value.worst)}` : '';
}

function verdictText(result, setup) {
    if (result.kind !== 'preset') {
        return '';
    }
    const timed = setup[result.id];
    const verdict = presetVerdict(result, timed ? timed.worst : null);
    return verdict.pass ? 'pass' : `fail: ${verdict.misses.join(', ')}`;
}

function showResults({measured, setup, failed}) {
    const payload = gpuPayload({
        userAgent: navigator.userAgent,
        screen: {width: window.screen.width, height: window.screen.height},
        view: startView,
        fullscreen: startView.fullscreen,
        dpr: window.devicePixelRatio || 1,
        windowS,
        env: environment || {},
        results: measured,
        setup,
        failed,
    });
    while (table.firstChild) {
        table.removeChild(table.firstChild);
    }
    environmentLine.textContent = describeEnvironment(environment, startView);
    table.appendChild(element('caption',
        `Frames per second; worst is the slowest second. Scale: the share of the window's device pixels drawn, stretched to fill it. Setup ms: for a probe, compiling and linking its shaders; for a Butterchurn preset, loading it / the longest frame from the switch to ${SWITCH_WATCH_S} s after it loaded (999: 999 or more). Verdict: a preset passes with an average of at least ${PASS.avg}, a worst second of at least ${PASS.worst} and no frame over ${PASS.stallMs} ms around its load.`));
    const head = element('tr');
    for (const title of ['Test', 'Scale', 'Average', 'Worst', 'Verdict', 'Setup ms']) {
        head.appendChild(element('th', title));
    }
    table.appendChild(head);
    const seen = {};
    for (const result of measured) {
        const row = element('tr');
        row.append(
            element('td', result.label),
            element('td', SCALE_LABELS[result.scale]),
            element('td', result.avg_fps.toFixed(1)),
            element('td', String(result.worst_fps)),
            element('td', verdictText(result, setup)),
            element('td', seen[result.id] ? '' : setupText(result.id, setup)),
        );
        seen[result.id] = true;
        table.appendChild(row);
    }
    Object.keys(failed).forEach((id) => {
        const preset = PRESETS.filter((candidate) => candidate.id === id)[0];
        const row = element('tr');
        row.append(element('td', preset ? preset.label : id), element('td', ''), element('td', 'failed'), element('td', ''),
            element('td', preset ? 'fail' : ''), element('td', failed[id]));
        table.appendChild(row);
    });
    // A preset that failed to load counts as judged and not passing.
    const judged = measured.filter((result) => result.kind === 'preset');
    const passing = judged.filter((result) => verdictText(result, setup) === 'pass').length;
    const broken = PRESETS.filter((preset) => failed[preset.id]).length;
    summary.textContent = `${passing} of ${judged.length + broken} presets pass`;
    while (qrBox.firstChild) {
        qrBox.removeChild(qrBox.firstChild);
    }
    qrBox.appendChild(qrSvg(document, qrMatrix(qrcode, payload, QR_LEVEL), 'QR code with the GPU test results'));
    results.dataset.payload = payload;
    openResults();
}

// Opens at the top, QR code first, for the photo.
function openResults() {
    results.hidden = false;
    results.scrollTop = 0;
    close.focus({preventScroll: true});
}

function closeResults() {
    results.hidden = true;
    show.focus();
}

let signal = null;

start.addEventListener('click', () => {
    if (state === 'testing') {
        stopRequested = true;
        if (cutWait) {
            cutWait();
        }
        return;
    }
    if (state === 'done') {
        // Every context of the run is given back; a fresh page starts clean.
        window.location.reload();
        return;
    }
    try {
        signal = buildSignal();
    } catch (error) {
        signal = null;
    }
    if (!signal) {
        note.textContent = 'Web Audio is not available here.';
        state = 'unavailable';
        sync();
        return;
    }
    startView = currentView();
    state = 'testing';
    stopRequested = false;
    results.hidden = true;
    sync();
    window.requestAnimationFrame(tick);
    window.setInterval(showLive, LIVE_MS);
    runTest(signal).then((outcome) => {
        signal.source.stop();
        signal.ctx.close().catch(() => {});
        showCanvas(null);
        name.textContent = 'GPU test';
        if (!outcome.failed.gl2) {
            note.textContent = stopRequested ? 'Stopped.' : 'Done.';
        }
        state = 'done';
        showResults(outcome);
        sync();
    });
});
show.addEventListener('click', () => {
    if (results.hidden) {
        openResults();
    } else {
        closeResults();
    }
});
close.addEventListener('click', closeResults);
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !results.hidden) {
        closeResults();
    }
});
sync();
