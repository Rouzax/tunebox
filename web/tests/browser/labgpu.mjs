// Browser check of the GPU test page (lab-gpu.html): lab mode's GPU test
// button opens it with labsec carried; only this page's policy allows
// WebAssembly; no AudioContext before the Start tap and exactly one after;
// a shortened full run measures Off, every probe shader at every scale and
// every Butterchurn preset at both scales, with every setup time; the
// presets ran as WebAssembly, never as JavaScript; the QR code holds
// exactly the results JSON; the results fit a phone's width; nothing is
// stored and every request stays on tunebox; a browser without WebGL 2 is
// told so. Fails on any page error or CSP violation.
//
// Needs Google Chrome or Chromium (or CHROME=<path>) and the repo's .venv.
// It starts its own tunebox on a free port with a temporary data folder and
// needs no network. Headless Chrome draws WebGL in software (SwiftShader),
// so its frame rates say nothing about a real GPU; this checks that the
// page works, not how fast.
//
//   node --experimental-websocket web/tests/browser/labgpu.mjs [screenshot folder]
//
// For the car's engine:
//
//   CHROME=$(.venv/bin/python scripts/chrome_120.py) CHROME_NO_SANDBOX=1 \
//       node --experimental-websocket web/tests/browser/labgpu.mjs
//
// Exits 1 and lists every failure, 2 when the check could not run.

import {spawn} from 'node:child_process';
import {mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from 'node:fs';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

import qrcode from '../../vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {PRESETS, PROBES} from '../../js/gpucore.js';
import {QR_LEVEL} from '../../js/gpureport.js';
import {qrMatrix} from '../../js/qr.js';
import {alive, connect, killGroup, sleep} from './cdp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const PYTHON = join(ROOT, '.venv', 'bin', 'python');
const SHOTS = process.argv[2] || null;
const DEVTOOLS_PORT = 9300 + Math.floor(Math.random() * 500);
const FOLDER_PREFIX = 'tunebox-labgpu-check-';
// Software WebGL draws the heaviest cells at a frame or two a second.
const RUN_TIMEOUT_MS = 600000;

if (typeof WebSocket === 'undefined') {
    console.error('This Node has no WebSocket client: use Node 22+, or run Node 20 with --experimental-websocket.');
    process.exit(2);
}

// Runs before any page script: counts AudioContexts and WebAssembly
// compiles, keeps every CSP violation and console warning, and takes
// WebGL 2 away for ?nowebgl2=1.
const INSTRUMENT = `(() => {
    const counts = {contexts: 0, wasm: 0, violations: [], warnings: []};
    window.gpuCheck = counts;
    if (window.AudioContext) {
        const Real = window.AudioContext;
        window.AudioContext = class extends Real {
            constructor(...args) {
                super(...args);
                counts.contexts += 1;
            }
        };
    }
    for (const name of ['compile', 'instantiate']) {
        const real = WebAssembly[name];
        WebAssembly[name] = function (...args) {
            if (name === 'compile' || !(args[0] instanceof WebAssembly.Module)) {
                counts.wasm += 1;
            }
            return real.apply(this, args);
        };
    }
    document.addEventListener('securitypolicyviolation', (event) => {
        counts.violations.push(event.violatedDirective + ' ' + event.blockedURI);
    });
    const warn = console.warn;
    console.warn = function (...args) {
        counts.warnings.push(args.map(String).join(' '));
        return warn.apply(this, args);
    };
    if (location.search.indexOf('nowebgl2=1') >= 0) {
        const getContext = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function (kind, ...rest) {
            return kind === 'webgl2' ? null : getContext.call(this, kind, ...rest);
        };
    }
})();`;

class CheckFailure extends Error {}

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
        const check = (problem) => {
            if (problem) {
                failures.push(problem);
            }
        };

        // The policy, as served.
        const policyOf = async (path) => (await fetch(`${base}${path}`)).headers.get('content-security-policy') || '';
        const gpuPolicy = await policyOf('/lab-gpu.html');
        check(/script-src 'self' 'wasm-unsafe-eval';/.test(gpuPolicy) && !/'unsafe-eval'/.test(gpuPolicy)
            ? null : `lab-gpu.html policy: ${gpuPolicy}`);
        for (const path of ['/', '/check.html']) {
            const policy = await policyOf(path);
            check(/script-src 'self';/.test(policy) && !/wasm/.test(policy) ? null : `${path} policy: ${policy}`);
        }

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
                await sleep(200);
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
        const counts = () => evaluate(`JSON.parse(JSON.stringify(window.gpuCheck))`);
        const gpuState = () => evaluate(`document.querySelector('.gpu').dataset.gpu`);

        // Lab mode's button opens the page and carries labsec.
        await open('/?lab=1&labsec=1');
        if (!(await until(`document.querySelector('.lab-gpu')`))) {
            throw new CheckFailure('lab mode has no GPU test button');
        }
        await tap('.lab-gpu');
        check(await until(`location.pathname === '/lab-gpu.html' && location.search === '?labsec=1'
            && document.querySelector('.gpu-start')`)
            ? null : `the GPU test button opened ${await evaluate('location.href')}`);
        await sleep(500);
        check((await counts()).contexts === 0 ? null : 'an AudioContext before Start');
        check(/about \d+ s\. Press Start\./.test(await evaluate(`document.querySelector('.gpu-note').textContent`))
            ? null : `the start note says ${await evaluate(`document.querySelector('.gpu-note').textContent`)}`);
        check(await evaluate(`document.querySelector('.gpu-back').getAttribute('href')`) === './?lab=1&labsec=1'
            ? null : 'Back to lab drops labsec');
        const env = await evaluate(`window.tuneboxEnvironment()`);
        if (!env.webgl2) {
            throw new CheckFailure(`this browser has no WebGL 2 (${JSON.stringify(env)}); the run cannot be checked here`);
        }

        const started = Date.now();
        await tap('.gpu-start');
        check(await until(`document.querySelector('.gpu').dataset.gpu === 'testing'`, 2000) ? null : `Start left the page ${await gpuState()}`);
        check(await evaluate(`document.querySelector('.gpu-start').textContent`) === 'Stop' ? null : 'no Stop while testing');
        await sleep(400);
        await shoot('gpu-off');
        // A frame of a probe and of a preset, for the eye.
        if (SHOTS) {
            await until(`/Plasma/.test(document.querySelector('.gpu-test').textContent)`, 30000);
            await sleep(600);
            await shoot('gpu-light');
            await until(`/Tunnel/.test(document.querySelector('.gpu-test').textContent)`, 30000);
            await sleep(800);
            await shoot('gpu-cover');
            await until(`/Noise/.test(document.querySelector('.gpu-test').textContent)`, 30000);
            await sleep(800);
            await shoot('gpu-heavy');
            await until(`/Hurricane/.test(document.querySelector('.gpu-test').textContent)`, 120000);
            await sleep(2500);
            await shoot('gpu-hurricane');
        }
        if (!(await until(`document.querySelector('.gpu').dataset.gpu === 'done'`, RUN_TIMEOUT_MS))) {
            throw new CheckFailure(`the run did not finish (at ${await evaluate(`document.querySelector('.gpu-test').textContent`)})`);
        }
        console.log(`full run with labsec=1: ${Math.round((Date.now() - started) / 1000)} s`);
        const payload = await evaluate(`document.querySelector('.gpu-results').dataset.payload`);
        const data = JSON.parse(payload);
        check(data.k === 'gpu' && data.v === 1 ? null : `marker ${data.k} ${data.v}`);
        check(data.w === 1 ? null : `window ${data.w} s`);
        check(Array.isArray(data.vw) && data.vw[0] === 960 && data.vw[1] === 560 ? null : `window ${JSON.stringify(data.vw)}`);
        check(typeof data.ua === 'string' && data.ua.length ? null : 'no user agent');
        check(data.e && data.e.g2 === 1 ? null : `environment ${JSON.stringify(data.e)}`);
        check(data.x === undefined ? null : `failed: ${JSON.stringify(data.x)}`);
        const rows = {off: 2};
        PROBES.forEach((probe) => {
            rows[probe.id] = 6;
        });
        PRESETS.forEach((preset) => {
            rows[preset.id] = 4;
        });
        check(Object.keys(data.r).join() === Object.keys(rows).join() ? null : `results for ${Object.keys(data.r).join()}`);
        for (const id of Object.keys(rows)) {
            const row = data.r[id] || [];
            check(row.length === rows[id] && row.every((value) => typeof value === 'number' && value > 0)
                ? null : `${id}: ${JSON.stringify(row)}`);
        }
        for (const probe of PROBES) {
            const ms = data.su[probe.id];
            check(typeof ms === 'number' && ms >= 0 ? null : `${probe.id} setup ${JSON.stringify(ms)}`);
        }
        for (const preset of PRESETS) {
            const pair = data.su[preset.id];
            check(Array.isArray(pair) && pair.length === 2 && pair[0] > 0 && pair[1] > 0
                ? null : `${preset.id} setup ${JSON.stringify(pair)}`);
        }
        const tableRows = await evaluate(`document.querySelectorAll('.gpu-table tr').length`);
        check(tableRows === 1 + 1 + PROBES.length * 3 + PRESETS.length * 2 ? null : `${tableRows} table rows`);

        // Butterchurn ran every preset as WebAssembly: a compile each, and
        // none refused, and it never fell back to JavaScript (which the
        // policy refuses and reports as a violation).
        const after = await counts();
        check(after.wasm >= PRESETS.length ? null : `${after.wasm} WebAssembly compiles for ${PRESETS.length} presets`);
        check(after.violations.length === 0 ? null : `CSP violations: ${after.violations.join('; ')}`);
        const refused = after.warnings.filter((text) => /WASM|JS preset/.test(text));
        check(refused.length === 0 ? null : `Butterchurn warned: ${refused.join('; ')}`);
        check(after.contexts === 1 ? null : `${after.contexts} AudioContexts after the run`);
        console.log(`WebAssembly compiles: ${after.wasm}; console warnings: ${after.warnings.length}`);

        // The code drawn is the code of exactly this payload.
        const drawn = await evaluate(`(() => {
            const svg = document.querySelector('.gpu-qr svg');
            const size = Number(svg.getAttribute('viewBox').split(' ')[2]);
            const cells = [...svg.querySelectorAll('rect[width="1"]')].map((r) => [Number(r.getAttribute('y')), Number(r.getAttribute('x'))]);
            const box = svg.getBoundingClientRect();
            return {size, cells, width: box.width, visible: box.width > 0 && !document.querySelector('.gpu-results').hidden};
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
        check(matrix.length <= 97 ? null : `QR code of ${matrix.length} modules`);
        await shoot('gpu-results');

        // On a phone the results fit the width: nothing past either edge.
        await send('Emulation.setDeviceMetricsOverride', {width: 360, height: 780, deviceScaleFactor: 3, mobile: true});
        await sleep(400);
        const outside = await evaluate(`(() => {
            const width = window.innerWidth;
            return ['.gpu-qr svg', '.gpu-env', '.gpu-table-box', '.gpu-close'].filter((selector) => {
                const box = document.querySelector(selector).getBoundingClientRect();
                return box.left < 0 || box.right > width;
            }).join(', ');
        })()`);
        check(outside === '' ? null : `past the edge of a phone: ${outside}`);
        await shoot('gpu-results-phone');
        await send('Emulation.setDeviceMetricsOverride', {width: 960, height: 560, deviceScaleFactor: 1, mobile: false});
        await sleep(300);
        check(await evaluate(`document.activeElement === document.querySelector('.gpu-close')`) ? null : 'the results did not take the focus');
        await send('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
        await send('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
        await sleep(200);
        check(await evaluate(`document.querySelector('.gpu-results').hidden && document.activeElement === document.querySelector('.gpu-show')`)
            ? null : 'Escape did not close the results back to the Results button');

        // Nothing stored, nothing fetched from anywhere but tunebox.
        check(await evaluate(`localStorage.length === 0 && sessionStorage.length === 0`) ? null : 'the GPU test stored something');
        const foreign = await evaluate(`performance.getEntriesByType('resource').map((e) => e.name)
            .filter((name) => !name.startsWith(location.origin + '/'))`);
        check(foreign.length === 0 ? null : `requests elsewhere: ${foreign.join(', ')}`);
        console.log(`results payload: ${payload.length} bytes, QR code ${matrix.length} modules`);
        console.log(payload);

        // Stop ends a run at once and keeps what it measured.
        await open('/lab-gpu.html?labsec=1');
        await until(`document.querySelector('.gpu-start')`);
        await tap('.gpu-start');
        await sleep(1700);
        await tap('.gpu-start');
        check(await until(`document.querySelector('.gpu').dataset.gpu === 'done'`, 2000) ? null : `Stop took over 2 s (${await gpuState()})`);
        const stopped = JSON.parse(await evaluate(`document.querySelector('.gpu-results').dataset.payload`));
        check(Object.keys(stopped.r).join() === 'off' ? null : `after Stop: ${JSON.stringify(stopped.r)}`);

        // Without WebGL 2 the page says so, and the code says so too.
        await open('/lab-gpu.html?labsec=1&nowebgl2=1');
        await until(`document.querySelector('.gpu-start')`);
        await tap('.gpu-start');
        check(await until(`document.querySelector('.gpu').dataset.gpu === 'done'`, 3000)
            && /no WebGL 2/.test(await evaluate(`document.querySelector('.gpu-note').textContent`))
            ? null : 'no WebGL 2 was not reported');
        const missing = JSON.parse(await evaluate(`document.querySelector('.gpu-results').dataset.payload || '{}'`));
        check(missing.x && missing.x.gl2 === 'missing' ? null : `without WebGL 2: ${JSON.stringify(missing)}`);
        check((await counts()).violations.length === 0 ? null : 'CSP violation without WebGL 2');
    } catch (error) {
        if (!(error instanceof CheckFailure)) {
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
    console.log('the GPU test page works');
}

run().catch((error) => {
    console.error(error);
    process.exit(2);
});
