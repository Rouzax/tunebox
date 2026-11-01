// Browser check of the player's lab mode (?lab=1): the normal player loads
// none of it; Start feeds the silent test signal to the analysers; Next
// steps through every visualizer style; a shortened test measures every
// style in both qualities; the QR code holds exactly the results JSON;
// nothing is stored; /check.html lists the environment rows. Fails on any
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
import {mkdirSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

import qrcode from '../../vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {QR_LEVEL} from '../../js/labreport.js';
import {qrMatrix} from '../../js/qr.js';
import {connect, killGroup, sleep} from './cdp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const PYTHON = join(ROOT, '.venv', 'bin', 'python');
const SHOTS = process.argv[2] || null;
const DEVTOOLS_PORT = 9300 + Math.floor(Math.random() * 500);
const STYLES = ['wave', 'bars', 'led', 'butterfly', 'ring', 'neon', 'ambient', 'scope', 'lava', 'milkdrop', 'aurora', 'off'];

if (typeof WebSocket === 'undefined') {
    console.error('This Node has no WebSocket client: use Node 22+, or run Node 20 with --experimental-websocket.');
    process.exit(2);
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
    const folder = mkdtempSync(join(tmpdir(), 'tunebox-lab-check-'));
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

        await open('/');
        if (!(await until(`document.querySelector('.viz-list li')`))) {
            throw new Error('the player did not start');
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

        // A listener's stored choice of Off: the lab starts on it, and it
        // is still the choice afterwards.
        await evaluate(`localStorage.setItem('tunebox.visualizer', 'off')`);
        await open('/?lab=1&labsec=1');
        if (!(await until(`document.querySelector('.lab-start')`))) {
            throw new Error('no lab overlay');
        }
        const before = await evaluate(`localStorage.length`);
        await tap('.lab-start');
        if (!(await until(`document.querySelector('.tunebox').dataset.lab === 'ready'`))) {
            throw new Error(`Start did not start the lab (${await evaluate(`document.querySelector('.lab-note').textContent`)})`);
        }
        check((await until(`document.querySelector('.tunebox').dataset.labSignal === 'yes'`, 4000))
            ? null : 'the analysers see no signal');

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

        await tap('.lab-power');
        await sleep(200);
        check(await evaluate(`document.querySelector('.lab-power').getAttribute('aria-pressed') === 'true'
            && document.querySelector('.tunebox').dataset.power === 'low' ? null : 'Low power did not switch on'`));
        await tap('.lab-power');
        await sleep(200);

        await tap('.lab-run');
        if (!(await until(`document.querySelector('.tunebox').dataset.lab === 'done'`, 90000))) {
            throw new Error('the test did not finish');
        }
        const payload = await evaluate(`document.querySelector('.lab-results').dataset.payload`);
        const data = JSON.parse(payload);
        check(data.v === 1 ? null : `format ${data.v}`);
        check(data.w === 1 ? null : `window ${data.w} s`);
        check(typeof data.ua === 'string' && data.ua.length ? null : 'no user agent');
        check(data.e && typeof data.e.g1 === 'number' && typeof data.e.g2 === 'number' ? null : `environment ${JSON.stringify(data.e)}`);
        const tested = STYLES.filter((id) => id !== 'off');
        check(Object.keys(data.r).join() === tested.join() ? null : `results for ${Object.keys(data.r).join()}`);
        for (const id of tested) {
            const row = data.r[id] || [];
            check(row.length === 4 && row.every((value) => typeof value === 'number' && value > 0)
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
        console.log(`results payload: ${payload.length} bytes, QR code ${matrix.length} modules`);

        check(await evaluate(`localStorage.length === ${before} && localStorage.getItem('tunebox.visualizer') === 'off'`)
            ? null : 'lab mode stored something');

        await open('/check.html');
        await until(`document.querySelectorAll('#report tr').length > 10`);
        const names = await evaluate(`[...document.querySelectorAll('#report th')].map((th) => th.textContent)`);
        for (const row of ['WebGL 1', 'WebGL 2', 'GPU renderer', 'GPU vendor', 'Largest WebGL texture', 'OffscreenCanvas', 'CPU cores', 'Device memory', 'Screen']) {
            check(names.includes(row) ? null : `check.html has no ${row} row`);
        }
        const environment = await evaluate(`JSON.stringify(window.tuneboxEnvironment())`);
        console.log(`environment: ${environment}`);
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
