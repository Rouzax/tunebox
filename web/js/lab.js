import qrcode from '../vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {QR_LEVEL, labPayload} from './labreport.js';
import {labSignal} from './labsignal.js';
import {frameStats, testWindowS} from './labstats.js';
import {qrMatrix, qrSvg} from './qr.js';

const SEED = 1;
// The first tenth of each test window (1 s of 10) is left out: a style
// switch costs a few slow frames that say nothing about the style itself.
const WARMUP_SHARE = 0.1;
const LIVE_MS = 500;
// The test's modes per style, in order.
const MODES = ['normal', 'low', 'still'];
const MODE_LABELS = {normal: 'normal', low: 'low power', still: 'still glow'};
// RMS of the meter's samples above this means the signal reaches Web Audio.
const SIGNAL_RMS = 0.01;

function element(tag, className, text) {
    const el = document.createElement(tag);
    el.className = className;
    if (text) {
        el.textContent = text;
    }
    return el;
}

function button(className, text) {
    const el = element('button', `lab-button ${className}`, text);
    el.type = 'button';
    return el;
}

// envinfo.js is a classic script, shared with check.html.
function loadEnvironment() {
    return new Promise((resolve) => {
        const read = () => {
            try {
                resolve(window.tuneboxEnvironment ? window.tuneboxEnvironment() : null);
            } catch (error) {
                resolve(null);
            }
        };
        if (window.tuneboxEnvironment) {
            read();
            return;
        }
        const script = document.createElement('script');
        script.src = 'envinfo.js';
        script.addEventListener('load', read);
        script.addEventListener('error', read);
        document.head.appendChild(script);
    });
}

function describeEnvironment(env) {
    if (!env) {
        return 'Environment: unknown';
    }
    const gl = env.webgl2 ? 'WebGL 2' : env.webgl1 ? 'WebGL 1 only' : 'no WebGL';
    const parts = [gl];
    if (env.renderer) {
        parts.push(env.renderer);
    }
    parts.push(`${env.cores || '?'} cores`, `screen ${env.screen.width} x ${env.screen.height} @${env.dpr}x`,
        `window ${window.innerWidth} x ${window.innerHeight}`);
    return parts.join(', ');
}

// The ?lab=1 overlay: every visualizer style on a silent test signal, with
// the frame rate the browser manages, an automatic test of every style in
// both qualities, and the results in one QR code for a photo. Nothing is
// stored and nothing is sent.
export function bindLab(app, viz) {
    const {root} = app;
    const styles = viz.styles();
    const stylesheet = document.createElement('link');
    stylesheet.rel = 'stylesheet';
    stylesheet.href = 'css/lab.css';
    document.head.appendChild(stylesheet);

    const panel = element('div', 'lab');
    panel.setAttribute('role', 'region');
    panel.setAttribute('aria-label', 'Visualizer lab');
    const status = element('div', 'lab-status');
    const name = element('span', 'lab-style');
    const fps = element('span', 'lab-fps', '-- fps');
    const note = element('span', 'lab-note', 'Silent test signal. Press Start.');
    status.append(name, fps, note);
    const start = button('lab-start', 'Start');
    const previous = button('lab-previous', 'Previous');
    const next = button('lab-next', 'Next');
    const power = button('lab-power', 'Low power');
    const still = button('lab-still', 'Still glow');
    const run = button('lab-run', 'Run test');
    const show = button('lab-show', 'Results');
    const gpu = button('lab-gpu', 'GPU test');
    const buttons = element('div', 'lab-buttons');
    buttons.append(start, previous, next, power, still, run, gpu, show);
    panel.append(status, buttons);

    const results = element('div', 'lab-results');
    results.hidden = true;
    results.setAttribute('role', 'dialog');
    results.setAttribute('aria-label', 'Lab results');
    const resultsText = element('div', 'lab-results-text');
    const environmentLine = element('p', 'lab-env');
    const table = element('table', 'lab-table');
    // Scrolls sideways on its own where a narrow window cannot fit it.
    const tableBox = element('div', 'lab-table-box');
    tableBox.appendChild(table);
    const close = button('lab-close', 'Close');
    resultsText.append(environmentLine, tableBox, close);
    const qrBox = element('div', 'lab-qr');
    results.append(qrBox, resultsText);
    root.append(panel, results);

    let environment = null;
    loadEnvironment().then((env) => {
        environment = env;
        environmentLine.textContent = describeEnvironment(env);
    });

    let started = false;
    let stillGlow = false;
    let testing = false;
    let stopRequested = false;
    // Ends the test's current wait at once when Stop test is pressed.
    let cutWait = null;
    let meter = null;
    let meterData = null;
    // Timestamps of the last second for the live rate, and of the test
    // window while one is measured.
    let recent = [];
    let recording = null;
    root.dataset.lab = 'idle';

    // Resolves after ms, or at once on Stop test.
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

    function sync() {
        const style = styles[viz.index()];
        name.textContent = style.label;
        power.setAttribute('aria-pressed', String(viz.lowPower()));
        still.setAttribute('aria-pressed', String(stillGlow));
        for (const control of [previous, next, power, still]) {
            control.disabled = !started || testing;
        }
        start.disabled = started;
        run.disabled = !started;
        run.textContent = testing ? 'Stop test' : 'Run test';
        gpu.disabled = testing;
        show.disabled = !results.dataset.payload;
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
    }

    function hasSignal() {
        if (!meter) {
            return false;
        }
        meter.getFloatTimeDomainData(meterData);
        let sum = 0;
        for (let i = 0; i < meterData.length; i++) {
            sum += meterData[i] * meterData[i];
        }
        return Math.sqrt(sum / meterData.length) > SIGNAL_RMS;
    }

    function showLive() {
        const live = frameStats(recent).avg_fps;
        const signal = hasSignal();
        fps.textContent = `${Math.round(live)} fps`;
        if (!testing) {
            note.textContent = signal ? 'signal ok' : 'no signal';
        }
        root.dataset.labSignal = signal ? 'yes' : 'no';
    }

    // Runs inside the tap: the AudioContext must start from a user gesture.
    function buildSignal(ctx) {
        const samples = labSignal(ctx.sampleRate, SEED);
        const buffer = ctx.createBuffer(2, samples.length, ctx.sampleRate);
        buffer.copyToChannel(samples, 0);
        buffer.copyToChannel(samples, 1);
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.loop = true;
        meter = ctx.createAnalyser();
        meter.fftSize = 2048;
        meterData = new Float32Array(meter.fftSize);
        // An analyser with no path to the destination may never be pulled.
        const mute = ctx.createGain();
        mute.gain.value = 0;
        source.connect(meter);
        meter.connect(mute).connect(ctx.destination);
        source.start();
        return source;
    }

    start.addEventListener('click', () => {
        viz.holdPower();
        let connected = false;
        try {
            connected = viz.connectLab(buildSignal);
        } catch (error) {
            // No Web Audio, or an AudioContext the browser refused.
            connected = false;
        }
        if (!connected) {
            note.textContent = 'Web Audio is not available here.';
            root.dataset.lab = 'unavailable';
            return;
        }
        started = true;
        root.dataset.lab = 'ready';
        window.requestAnimationFrame(tick);
        window.setInterval(showLive, LIVE_MS);
        sync();
    });

    const step = (delta) => () => {
        viz.select((viz.index() + delta + styles.length) % styles.length);
        sync();
    };
    previous.addEventListener('click', step(-1));
    next.addEventListener('click', step(1));
    power.addEventListener('click', () => {
        viz.setLowPower(!viz.lowPower());
        sync();
    });
    function holdGlow(on) {
        stillGlow = on;
        if (app.holdBass) {
            app.holdBass(on);
        }
    }
    still.addEventListener('click', () => {
        holdGlow(!stillGlow);
        sync();
    });

    function showResults(measured, windowS) {
        const payload = labPayload({
            userAgent: navigator.userAgent,
            screen: {width: window.screen.width, height: window.screen.height},
            view: {width: window.innerWidth, height: window.innerHeight},
            dpr: window.devicePixelRatio || 1,
            windowS,
            env: environment || {},
            results: measured,
        });
        while (table.firstChild) {
            table.removeChild(table.firstChild);
        }
        const caption = element('caption', 'lab-caption',
            'Frames per second. Page: what the browser managed; Off is the page with no visualizer. Draws: how often the visualizer itself drew (low power draws the spectrum styles at most 30 times a second); a dash means the style keeps no count. Still glow: normal quality with the cover glow not following the bass.');
        table.appendChild(caption);
        const head = element('tr', '');
        for (const title of ['Style', 'Mode', 'Page average', 'Page worst', 'Draws']) {
            head.appendChild(element('th', '', title));
        }
        table.appendChild(head);
        for (const result of measured) {
            const row = element('tr', '');
            row.append(
                element('td', '', result.label),
                element('td', '', MODE_LABELS[result.mode]),
                element('td', '', `${result.avg_fps.toFixed(1)}`),
                element('td', '', `${result.worst_fps.toFixed(1)}`),
                element('td', '', result.draw_fps === null ? '-' : `${Math.round(result.draw_fps)}`),
            );
            table.appendChild(row);
        }
        while (qrBox.firstChild) {
            qrBox.removeChild(qrBox.firstChild);
        }
        qrBox.appendChild(qrSvg(document, qrMatrix(qrcode, payload, QR_LEVEL), 'QR code with the lab results'));
        results.dataset.payload = payload;
        openResults();
    }

    // Focus moves into the results and back, so a keyboard does not wander
    // through the player hidden behind them.
    function openResults() {
        results.hidden = false;
        close.focus();
    }

    function closeResults() {
        results.hidden = true;
        show.focus();
    }

    async function runTest() {
        const windowS = testWindowS(new URLSearchParams(window.location.search).get('labsec'));
        const warmupMs = windowS * 1000 * WARMUP_SHARE;
        const measureMs = windowS * 1000 - warmupMs;
        const kept = {index: viz.index(), low: viz.lowPower(), still: stillGlow};
        // Off first: the page with no visualizer is what every style is read
        // against.
        const order = styles.map((style, i) => ({i, style}));
        const plan = [];
        order.filter((s) => s.style.id === 'off').concat(order.filter((s) => s.style.id !== 'off')).forEach(({i, style}) => {
            for (const mode of MODES) {
                plan.push({i, style, mode});
            }
        });
        const measured = [];
        testing = true;
        viz.lockMenu(true);
        stopRequested = false;
        results.hidden = true;
        root.dataset.lab = 'testing';
        sync();
        for (let n = 0; n < plan.length && !stopRequested; n++) {
            const {i, style, mode} = plan[n];
            viz.setLowPower(mode === 'low');
            holdGlow(mode === 'still');
            viz.select(i);
            sync();
            note.textContent = `test ${n + 1}/${plan.length}${mode === 'normal' ? '' : `, ${MODE_LABELS[mode]}`}`;
            await wait(warmupMs);
            recording = [];
            const drawsFrom = viz.draws();
            const from = performance.now();
            await wait(measureMs);
            const drawsTo = viz.draws();
            const elapsed = performance.now() - from;
            const stats = frameStats(recording);
            recording = null;
            // A window cut short by Stop test is left out.
            if (!stopRequested) {
                const drawFps = drawsFrom === null || drawsTo === null ? null : ((drawsTo - drawsFrom) * 1000) / elapsed;
                measured.push(Object.assign({style: style.id, label: style.label, mode, draw_fps: drawFps}, stats));
            }
        }
        viz.setLowPower(kept.low);
        holdGlow(kept.still);
        viz.select(kept.index);
        testing = false;
        viz.lockMenu(false);
        root.dataset.lab = measured.length ? 'done' : 'ready';
        if (measured.length) {
            showResults(measured, windowS);
        }
        sync();
    }

    run.addEventListener('click', () => {
        if (testing) {
            stopRequested = true;
            if (cutWait) {
                cutWait();
            }
        } else {
            runTest();
        }
    });
    // A page of its own (lab-gpu.html): it measures WebGL apart from the
    // player, with its own signal and results.
    gpu.addEventListener('click', () => {
        const seconds = new URLSearchParams(window.location.search).get('labsec');
        window.location.href = seconds === null ? 'lab-gpu.html' : `lab-gpu.html?labsec=${encodeURIComponent(seconds)}`;
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
    // A style picked from the player's own visualizer menu.
    viz.onMenuPick(sync);
    sync();
}
