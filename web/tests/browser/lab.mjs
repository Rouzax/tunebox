// Browser check of the player's lab mode (?lab=1): the normal player loads
// none of it; Start feeds the silent test signal to the analysers; Next
// steps through every visualizer style; the cover glow's layer sits on
// the cover and follows the bass, and Still glow holds it;
// a shortened test measures every style, Off first, in both modes;
// the results fit a phone's width; the QR code holds exactly the results JSON;
// Stop test stops at once; MilkDrop draws and stops drawing when left;
// each GPU style draws at its scale, a step smaller in low power, and
// gives its WebGL context back when left; nothing is stored and no stream
// plays; exactly one AudioContext is made, and only after the Start tap;
// every WebGL context made is given back but MilkDrop's; Start without
// Web Audio says so; /check.html lists the environment rows. Fails on any
// page or CSP error.
//
// Needs Google Chrome or Chromium (or CHROME=<path>) and the repo's .venv.
// It starts its own tunebox on a free port with a temporary data folder and
// needs no network:
//
//   node --experimental-websocket web/tests/browser/lab.mjs [screenshot folder]
//
// For the car's engine:
//
//   CHROME=$(.venv/bin/python scripts/chrome_120.py) CHROME_NO_SANDBOX=1 \
//       node --experimental-websocket web/tests/browser/lab.mjs
//
// Exits 1 and lists every failure, 2 when the check could not run.

import {spawn} from 'node:child_process';
import {mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from 'node:fs';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

import qrcode from '../../vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {QR_LEVEL} from '../../js/labreport.js';
import {SHUFFLE_ID} from '../../js/milkdropcore.js';
import {STYLES as ALL_STYLES} from '../../js/vizcore.js';
import {qrMatrix} from '../../js/qr.js';
import {alive, connect, killGroup, sleep} from './cdp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const PYTHON = join(ROOT, '.venv', 'bin', 'python');
const SHOTS = process.argv[2] || null;
const DEVTOOLS_PORT = 9300 + Math.floor(Math.random() * 500);
const FOLDER_PREFIX = 'tunebox-lab-check-';
// Every style, as Next steps through them (headless Chrome has WebGL 2).
const STYLES = ALL_STYLES.map((style) => style.id);
// Styles audioMotion does not draw have no draw count.
const UNCOUNTED = ALL_STYLES.filter((style) => style.engine !== 'motion').map((style) => style.id);
// Run test measures MilkDrop as Shuffle only.
const TESTED = STYLES.filter((id) => id.indexOf('md-') !== 0 || id === SHUFFLE_ID);

if (typeof WebSocket === 'undefined') {
    console.error('This Node has no WebSocket client: use Node 22+, or run Node 20 with --experimental-websocket.');
    process.exit(2);
}

// Runs before any page script: counts AudioContexts, WebGL contexts made
// and given back and WebAssembly compiles, records policy violations, and takes Web
// Audio away for ?nowebaudio=1.
const INSTRUMENT = `(() => {
    const counts = {contexts: 0, gl: 0, losses: 0, wasm: 0, violations: 0};
    window.labCheckCounts = counts;
    for (const name of ['compile', 'instantiate']) {
        const real = WebAssembly[name];
        WebAssembly[name] = function (...args) {
            if (name === 'compile' || !(args[0] instanceof WebAssembly.Module)) {
                counts.wasm += 1;
            }
            return real.apply(this, args);
        };
    }
    document.addEventListener('securitypolicyviolation', () => {
        counts.violations += 1;
    });
    if (location.search.indexOf('nowebaudio=1') >= 0) {
        window.AudioContext = undefined;
        window.webkitAudioContext = undefined;
    } else if (window.AudioContext) {
        const Real = window.AudioContext;
        window.AudioContext = class extends Real {
            constructor(...args) {
                super(...args);
                counts.contexts += 1;
            }
        };
    }
    // Every WebGL context made, counted once however often it is asked for.
    // Butterchurn draws on an OffscreenCanvas where there is one.
    const glMade = new WeakSet();
    for (const type of [window.HTMLCanvasElement, window.OffscreenCanvas]) {
        if (!type) {
            continue;
        }
        const getContext = type.prototype.getContext;
        type.prototype.getContext = function (kind, ...rest) {
            const context = getContext.call(this, kind, ...rest);
            if (context && /webgl/.test(kind) && !glMade.has(context)) {
                glMade.add(context);
                counts.gl += 1;
            }
            return context;
        };
    }
    for (const type of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
        if (!type) {
            continue;
        }
        const getExtension = type.prototype.getExtension;
        type.prototype.getExtension = function (name) {
            const extension = getExtension.call(this, name);
            if (extension && name === 'WEBGL_lose_context' && !extension.labCounted) {
                const lose = extension.loseContext.bind(extension);
                extension.loseContext = () => {
                    counts.losses += 1;
                    lose();
                };
                extension.labCounted = true;
            }
            return extension;
        };
    }
})();`;

// A failure of lab mode itself (exit 1), as opposed to a check that could
// not run (exit 2).
class LabFailure extends Error {}

// A check killed outright (SIGKILL, a timeout) runs no exit handler; the
// next run stops the tunebox it left and removes its folder. The tunebox
// pid is killed only while its environment still names that folder's
// config, so a reused pid is never hit.
function sweepStaleFolders() {
    for (const name of readdirSync(tmpdir())) {
        if (!name.startsWith(FOLDER_PREFIX)) {
            continue;
        }
        const folder = join(tmpdir(), name);
        let owner = null;
        try {
            owner = JSON.parse(readFileSync(join(folder, 'owner.json'), 'utf8'));
        } catch (error) {
            continue;
        }
        if (alive(owner.node)) {
            continue;
        }
        try {
            const environment = readFileSync(`/proc/${owner.tunebox}/environ`, 'utf8').split('\0');
            if (environment.includes(`TUNEBOX_CONFIG=${join(folder, 'tunebox.toml')}`)) {
                killGroup(owner.tunebox, 'SIGKILL');
            }
        } catch (error) {
            // tunebox is gone too.
        }
        try {
            rmSync(folder, {recursive: true, force: true, maxRetries: 5, retryDelay: 200});
        } catch (error) {
            // Left for the next sweep.
        }
    }
}

function freePort() {
    return new Promise((done, fail) => {
        const server = createServer();
        server.once('error', fail);
        server.listen(0, '127.0.0.1', () => {
            const {port} = server.address();
            server.close(() => done(port));
        });
    });
}

async function startTunebox(folder, port) {
    writeFileSync(join(folder, 'tunebox.toml'), [
        '[server]',
        'host = "127.0.0.1"',
        `port = ${port}`,
        `data_dir = "${folder}"`,
        '',
        '[[channels]]',
        'id = "first"',
        'name = "First Station"',
        'source = { type = "triton", mount = "OWR_INTERNATIONAL" }',
        '[[channels.streams]]',
        'name = "Default"',
        'url = "https://example.invalid/first.aac"',
        '',
    ].join('\n'));
    const log = [];
    const child = spawn(PYTHON, ['-m', 'tunebox'], {
        cwd: ROOT,
        env: Object.assign({}, process.env, {TUNEBOX_CONFIG: join(folder, 'tunebox.toml')}),
        stdio: ['ignore', 'pipe', 'pipe'],
        detached: true,
    });
    writeFileSync(join(folder, 'owner.json'), JSON.stringify({node: process.pid, tunebox: child.pid}));
    const onExit = () => killGroup(child.pid, 'SIGKILL');
    process.prependOnceListener('exit', onExit);
    child.once('exit', () => process.removeListener('exit', onExit));
    child.stdout.on('data', (chunk) => log.push(String(chunk)));
    child.stderr.on('data', (chunk) => log.push(String(chunk)));
    for (let i = 0; i < 100; i++) {
        try {
            if ((await fetch(`http://127.0.0.1:${port}/healthz`)).ok) {
                return {child, log};
            }
        } catch (error) {
            // Still starting.
        }
        await sleep(100);
    }
    killGroup(child.pid, 'SIGTERM');
    throw new Error(`tunebox did not start:\n${log.join('')}`);
}

async function run() {
    sweepStaleFolders();
    const folder = mkdtempSync(join(tmpdir(), FOLDER_PREFIX));
    const failures = [];
    let tunebox = null;
    let browser = null;
    try {
        const port = await freePort();
        tunebox = await startTunebox(folder, port);
        const base = `http://127.0.0.1:${port}`;
        browser = await connect({port: DEVTOOLS_PORT});
        const {send, evaluate} = browser;
        await send('Page.enable');
        await send('Page.addScriptToEvaluateOnNewDocument', {source: INSTRUMENT});
        // The car's browser at two thirds of its screen.
        await send('Emulation.setDeviceMetricsOverride', {width: 960, height: 560, deviceScaleFactor: 1, mobile: false});

        const until = async (expression, ms = 5000) => {
            const end = Date.now() + ms;
            while (Date.now() < end) {
                if (await evaluate(`Boolean(${expression})`)) {
                    return true;
                }
                await sleep(100);
            }
            return false;
        };
        const open = async (path) => {
            await send('Page.navigate', {url: `${base}${path}`});
            await sleep(300);
        };
        // A real mouse press, so the page gets a user gesture.
        const tap = async (selector) => {
            const box = await evaluate(`(() => {
                const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
                return {x: r.left + r.width / 2, y: r.top + r.height / 2};
            })()`);
            for (const type of ['mousePressed', 'mouseReleased']) {
                await send('Input.dispatchMouseEvent', {type, x: box.x, y: box.y, button: 'left', clickCount: 1});
            }
        };
        const shoot = async (name) => {
            if (SHOTS) {
                mkdirSync(SHOTS, {recursive: true});
                const shot = await send('Page.captureScreenshot', {format: 'png'});
                writeFileSync(join(SHOTS, `${name}.png`), Buffer.from(shot.result.data, 'base64'));
            }
        };
        const check = (problem) => {
            if (problem) {
                failures.push(problem);
            }
        };
        const counts = () => evaluate(`({...window.labCheckCounts})`);
        const lab = () => evaluate(`document.querySelector('.tunebox').dataset.lab`);

        await open('/');
        if (!(await until(`document.querySelector('.viz-groups .tile')`))) {
            throw new LabFailure('the player did not start');
        }
        await sleep(500);
        check(await evaluate(`(() => {
            const loaded = performance.getEntriesByType('resource').map((e) => e.name)
                .filter((name) => /\\/(lab|labsignal|labstats|labreport|qr)\\.js|lab\\.css|envinfo\\.js/.test(name));
            if (document.querySelector('.lab')) {
                return 'the normal player shows the lab';
            }
            return loaded.length ? 'the normal player loaded ' + loaded.join(', ') : null;
        })()`));
        check((await counts()).contexts === 0 ? null : 'the normal player made an AudioContext without a gesture');

        // A listener's stored choice of Off: the lab starts on it, and it
        // is still the choice afterwards.
        await evaluate(`localStorage.setItem('tunebox.visualizer', 'off')`);
        await open('/?lab=1&labsec=1');
        if (!(await until(`document.querySelector('.lab-start')`))) {
            throw new LabFailure('no lab overlay');
        }
        const before = await evaluate(`localStorage.length`);
        check(await evaluate(`document.querySelector('.play').disabled`) ? null : 'the play button works in lab mode');
        await tap('.play');
        await sleep(300);
        check(await evaluate(`document.querySelector('.tunebox').dataset.state`) === 'stopped' ? null : 'a stream started in lab mode');
        check((await counts()).contexts === 0 ? null : 'an AudioContext before Start');
        await tap('.lab-start');
        if (!(await until(`document.querySelector('.tunebox').dataset.lab === 'ready'`))) {
            throw new LabFailure(`Start did not start the lab (${await evaluate(`document.querySelector('.lab-note').textContent`)})`);
        }
        check((await until(`document.querySelector('.tunebox').dataset.labSignal === 'yes'`, 4000))
            ? null : 'the analysers see no signal');
        check((await counts()).contexts === 1 ? null : `${(await counts()).contexts} AudioContexts after Start`);

        await sleep(1000);
        await shoot('lab-wave');
        check(await evaluate(`document.querySelector('.tunebox').dataset.viz`) === 'off' ? null : 'the lab did not start on the stored style');
        // Every style once, from the stored one round to it again.
        const seen = [];
        for (let i = 0; i < STYLES.length; i++) {
            await tap('.lab-next');
            await sleep(400);
            seen.push(await evaluate(`document.querySelector('.tunebox').dataset.viz`));
            await shoot(`lab-${seen[i]}`);
        }
        check([...seen].sort().join() === [...STYLES].sort().join() ? null : `Next showed ${seen.join(', ')}`);
        const labels = await evaluate(`document.querySelector('.lab-style').textContent`);
        check(labels ? null : 'no style label');

        // A style picked from the player's own menu shows in the lab bar.
        await tap('.viz-toggle');
        await sleep(600);
        if (await evaluate(`!document.querySelector('.btn-viz-tiles').classList.contains('hidden')`)) {
            await tap('.btn-viz-tiles');
            await sleep(300);
        }
        await tap('.viz-groups .tile[data-group="bars"]');
        await sleep(300);
        await tap('.viz-list li:nth-child(4) button');
        await sleep(600);
        check(await evaluate(`document.querySelector('.lab-style').textContent`) === 'Ring'
            ? null : `the lab bar says ${await evaluate(`document.querySelector('.lab-style').textContent`)} after picking Ring from the menu`);

        await tap('.lab-power');
        await sleep(200);
        check(await evaluate(`document.querySelector('.lab-power').getAttribute('aria-pressed') === 'true'
            && document.querySelector('.tunebox').dataset.power === 'low' ? null : 'Low power did not switch on'`));
        await tap('.lab-power');
        await sleep(200);

        // The bass swell sits on the cover's box and moves with the signal.
        const glowOver = await evaluate(`(() => {
            const glow = document.querySelector('.car-glow');
            const art = document.querySelector('.artwork-img');
            return ['offsetLeft', 'offsetTop', 'offsetWidth', 'offsetHeight']
                .every((key) => Math.abs(glow[key] - art[key]) <= 1) && art.offsetWidth > 0;
        })()`);
        check(glowOver ? null : 'the glow layer is not over the cover');
        const swell = [];
        for (let i = 0; i < 8; i++) {
            swell.push(await evaluate(`document.querySelector('.car-glow').style.opacity`));
            await sleep(100);
        }
        check(new Set(swell).size > 1 && swell.some((value) => Number(value) > 0)
            ? null : `the glow did not follow the bass: ${swell.join(', ')}`);

        // Still glow holds the cover glow while a style draws.
        await tap('.lab-still');
        await sleep(600);
        const glow = await evaluate(`[document.querySelector('.car-glow').style.opacity,
            document.querySelector('.lab-still').getAttribute('aria-pressed')].join()`);
        check(glow === '0,true' ? null : `Still glow left the glow at ${glow}`);
        await tap('.lab-still');
        await sleep(600);
        check(await evaluate(`document.querySelector('.lab-still').getAttribute('aria-pressed')`) === 'false'
            ? null : 'Still glow did not switch off');

        await tap('.lab-run');
        if (!(await until(`document.querySelector('.tunebox').dataset.lab === 'done'`, 90000))) {
            throw new LabFailure('the test did not finish');
        }
        const payload = await evaluate(`document.querySelector('.lab-results').dataset.payload`);
        const data = JSON.parse(payload);
        check(data.v === 5 ? null : `format ${data.v}`);
        check(Array.isArray(data.vw) && data.vw[0] === 960 && data.vw[1] === 560 ? null : `window ${JSON.stringify(data.vw)}`);
        check(data.w === 1 ? null : `window ${data.w} s`);
        check(typeof data.ua === 'string' && data.ua.length ? null : 'no user agent');
        check(data.e && typeof data.e.g1 === 'number' && typeof data.e.g2 === 'number' ? null : `environment ${JSON.stringify(data.e)}`);
        // Off first, as the baseline.
        const tested = ['off'].concat(TESTED.filter((id) => id !== 'off'));
        check(Object.keys(data.r).join() === tested.join() ? null : `results for ${Object.keys(data.r).join()}`);
        for (const id of tested) {
            const row = data.r[id] || [];
            const counted = !UNCOUNTED.includes(id);
            const pages = counted ? [row[0], row[1], row[3], row[4]] : row;
            // audioMotion draws at most 30 times a second in low power.
            const drawsOk = !counted || ([row[2], row[5]].every((value) => typeof value === 'number' && value > 0) && row[5] <= 33);
            check(row.length === (counted ? 6 : 4) && pages.every((value) => typeof value === 'number' && value > 0) && drawsOk
                ? null : `${id}: ${JSON.stringify(row)}`);
        }
        const rows = await evaluate(`document.querySelectorAll('.lab-table tr').length`);
        check(rows === tested.length * 2 + 1 ? null : `${rows} table rows`);

        // The code drawn is the code of exactly this payload.
        const drawn = await evaluate(`(() => {
            const svg = document.querySelector('.lab-qr svg');
            const size = Number(svg.getAttribute('viewBox').split(' ')[2]);
            const cells = [...svg.querySelectorAll('rect[width="1"]')].map((r) => [Number(r.getAttribute('y')), Number(r.getAttribute('x'))]);
            const box = svg.getBoundingClientRect();
            return {size, cells, width: box.width, visible: box.width > 0 && !document.querySelector('.lab-results').hidden};
        })()`);
        const matrix = qrMatrix(qrcode, payload, QR_LEVEL);
        const expected = [];
        matrix.forEach((cells, row) => cells.forEach((dark, col) => {
            if (dark) {
                expected.push(`${row + 4},${col + 4}`);
            }
        }));
        const got = drawn.cells.map(([row, col]) => `${row},${col}`);
        check(drawn.size === matrix.length + 8 && got.join(';') === expected.join(';')
            ? null : 'the QR code does not hold the results payload');
        check(drawn.visible && drawn.width >= 200 ? null : `QR code shown at ${drawn.width} px`);
        await shoot('lab-results');

        // On a phone the results fit the width: nothing past either edge.
        await send('Emulation.setDeviceMetricsOverride', {width: 360, height: 780, deviceScaleFactor: 3, mobile: true});
        await sleep(400);
        const outside = await evaluate(`(() => {
            const width = window.innerWidth;
            return ['.lab-qr svg', '.lab-env', '.lab-table', '.lab-close'].filter((selector) => {
                const box = document.querySelector(selector).getBoundingClientRect();
                return box.left < 0 || box.right > width;
            }).join(', ');
        })()`);
        check(outside === '' ? null : `past the edge of a phone: ${outside}`);
        await shoot('lab-results-phone');
        await send('Emulation.setDeviceMetricsOverride', {width: 960, height: 560, deviceScaleFactor: 1, mobile: false});
        await sleep(400);
        check(await evaluate(`document.activeElement === document.querySelector('.lab-close')`) ? null : 'the results did not take the focus');
        await send('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
        await send('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
        await sleep(200);
        check(await evaluate(`document.querySelector('.lab-results').hidden && document.activeElement === document.querySelector('.lab-show')`)
            ? null : 'Escape did not close the results back to the Results button');

        // Stop test ends the test at once, keeping the finished windows.
        await tap('.lab-run');
        await sleep(1700);
        const stopAt = Date.now();
        await tap('.lab-run');
        check(await until(`document.querySelector('.tunebox').dataset.lab === 'done'`, 1000)
            ? null : `Stop test took over a second (${await lab()})`);
        const stopped = JSON.parse(await evaluate(`document.querySelector('.lab-results').dataset.payload`));
        check(Object.keys(stopped.r).length === 1 && stopped.r.off && stopped.r.off.length === 4 && stopped.r.off[2] === null
            ? null : `after Stop test: ${JSON.stringify(stopped.r)} in ${Date.now() - stopAt} ms`);
        check((await counts()).contexts === 1 ? null : `${(await counts()).contexts} AudioContexts at the end`);
        // Every WebGL context made was given back but MilkDrop's, which
        // Butterchurn keeps once loaded: envinfo's, the player's WebGL 2
        // check and the GPU styles'.
        const afterTest = await counts();
        check(afterTest.gl - afterTest.losses === 1 ? null : `${afterTest.gl} WebGL contexts made, ${afterTest.losses} given back`);
        console.log(`results payload: ${payload.length} bytes, QR code ${matrix.length} modules`);
        console.log(payload);

        // MilkDrop: Shuffle draws, every preset compiles as WebAssembly with
        // no policy violation, and leaving it stops its drawing.
        const stepTo = async (id) => {
            for (let i = 0; i < STYLES.length && (await evaluate(`document.querySelector('.tunebox').dataset.viz`)) !== id; i++) {
                await tap('.lab-next');
                await sleep(250);
            }
            return (await evaluate(`document.querySelector('.tunebox').dataset.viz`)) === id;
        };
        if (!(await evaluate(`document.querySelector('.lab-results').hidden`))) {
            await tap('.lab-close');
            await sleep(300);
        }
        const mdFrames = () => evaluate(`(() => { const c = document.querySelector('.car-md'); return c ? Number(c.dataset.mdFrames || 0) : -1; })()`);
        check(await stepTo(SHUFFLE_ID) ? null : 'Next never reached MilkDrop Shuffle');
        await sleep(3000);
        const shuffling = await mdFrames();
        await sleep(1000);
        const box = await evaluate(`(() => { const c = document.querySelector('.car-md'); return c ? [c.width, c.height, c.hidden] : null; })()`);
        check(box && box[0] > 0 && box[1] > 0 && !box[2] && (await mdFrames()) > shuffling && shuffling > 0
            ? null : `MilkDrop does not draw (${JSON.stringify(box)}, frames ${shuffling})`);
        await shoot('lab-md-shuffle');
        check(await stepTo('wave') ? null : 'Next never came round to Wave');
        await sleep(300);
        const left = await mdFrames();
        await sleep(1000);
        check((await mdFrames()) === left && await evaluate(`document.querySelector('.car-md').hidden`)
            ? null : 'MilkDrop still draws after leaving it');
        const after = await counts();
        check(after.wasm > 0 ? null : 'MilkDrop compiled no WebAssembly');
        check(after.violations === 0 ? null : `${after.violations} policy violation(s)`);

        // GPU styles: each draws on one canvas at its start scale, low power
        // draws a step smaller, and leaving them gives the context back.
        const gpu = () => evaluate(`(() => { const c = document.querySelector('.car-gpu');
            return c ? {frames: Number(c.dataset.gpuFrames || 0), scale: c.dataset.gpuScale, width: c.width} : null; })()`);
        for (const [id, scale] of [['tunnel', '1'], ['kaleido', '1'], ['synthwave', '1'], ['ink', '2']]) {
            check(await stepTo(id) ? null : `Next never reached ${id}`);
            await sleep(1500);
            const first = await gpu();
            await sleep(1000);
            const later = await gpu();
            check(first && later && later.frames > first.frames && later.scale === scale && later.width > 0
                ? null : `${id} does not draw at scale ${scale} (${JSON.stringify(later)})`);
            await shoot(`lab-${id}`);
        }
        check(await evaluate(`document.querySelectorAll('.car-gpu').length === 1`) ? null : 'not exactly one GPU style canvas');
        check(!(await evaluate(`/could not start/.test(document.body.textContent)`)) ? null : 'a GPU style could not start');
        await tap('.lab-power');
        await sleep(800);
        const lowInk = await gpu();
        check(lowInk && lowInk.scale === '3' ? null : `Liquid ink in low power at ${JSON.stringify(lowInk)}`);
        await tap('.lab-power');
        check(await stepTo('wave') ? null : 'Next never came round to Wave from the GPU styles');
        await sleep(500);
        check(await evaluate(`document.querySelector('.car-gpu') === null`) ? null : 'the GPU style canvas stays after leaving');
        const afterGpu = await counts();
        check(afterGpu.gl - afterGpu.losses === 1 ? null : `after the GPU styles: ${afterGpu.gl} WebGL contexts made, ${afterGpu.losses} given back`);

        check(await evaluate(`localStorage.length === ${before} && localStorage.getItem('tunebox.visualizer') === 'off'`)
            ? null : 'lab mode stored something');

        await open('/check.html');
        await until(`document.querySelectorAll('#report tr').length > 10`);
        const env = await evaluate(`window.tuneboxEnvironment()`);
        // The page read the environment once, the call above once more.
        check((await counts()).losses === 2 * (Number(env.webgl1) + Number(env.webgl2)) ? null : `${(await counts()).losses} WebGL contexts given back on check.html`);
        const names = await evaluate(`[...document.querySelectorAll('#report th')].map((th) => th.textContent)`);
        for (const row of ['WebGL 1', 'WebGL 2', 'GPU renderer', 'GPU vendor', 'Largest WebGL texture', 'OffscreenCanvas', 'CPU cores', 'Device memory', 'Screen']) {
            check(names.includes(row) ? null : `check.html has no ${row} row`);
        }
        console.log(`environment: ${JSON.stringify(env)}`);

        await open('/?lab=1&labsec=1&nowebaudio=1');
        if (!(await until(`document.querySelector('.lab-start')`))) {
            throw new LabFailure('no lab overlay without Web Audio');
        }
        await tap('.lab-start');
        check(await until(`document.querySelector('.tunebox').dataset.lab === 'unavailable'
            && /Web Audio/.test(document.querySelector('.lab-note').textContent)`, 2000)
            ? null : 'Start without Web Audio did not say so');
    } catch (error) {
        if (!(error instanceof LabFailure)) {
            throw error;
        }
        failures.push(error.message);
    } finally {
        if (browser) {
            for (const error of browser.errors) {
                failures.push(`page error: ${error}`);
            }
            await browser.close();
        }
        if (tunebox) {
            const exited = new Promise((done) => tunebox.child.once('exit', done));
            killGroup(tunebox.child.pid, 'SIGTERM');
            await Promise.race([exited, sleep(5000)]);
        }
        rmSync(folder, {recursive: true, force: true});
    }
    if (failures.length) {
        console.log(`${failures.length} problem(s):`);
        for (const failure of failures) {
            console.log(`  ${failure}`);
        }
        process.exit(1);
    }
    console.log('lab mode works');
}

run().catch((error) => {
    console.error(error);
    process.exit(2);
});
