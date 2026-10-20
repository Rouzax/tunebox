// Browser check of the admin panel: log in with a password and a TOTP code,
// then switch a station off, reorder, edit, have a dangerous title rule
// refused, restore from the history, change the settings, lock the player
// with a PIN and pass it, hide a chosen station behind it, confirm account
// changes with a code, change the password, log out and log in with it,
// checking after each step
// that the player's /api/channels follows. Also checks that the pages have no
// sideways scroll at phone and laptop sizes, and no page or CSP errors.
//
// Needs Google Chrome or Chromium (or CHROME=<path>) and the repo's .venv.
// It starts its own tunebox on a free port with a temporary data folder, so
// no network is needed:
//
//   node --experimental-websocket web/tests/browser/admin.mjs [screenshot folder]
//
// Exits 1 and lists every failure, 2 when the check could not run.

import {spawn, spawnSync} from 'node:child_process';
import {createHmac} from 'node:crypto';
import {mkdirSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

import {connect, sleep} from './cdp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const PYTHON = join(ROOT, '.venv', 'bin', 'python');
const SHOTS = process.argv[2] || null;
const DEVTOOLS_PORT = 9800 + Math.floor(Math.random() * 500);
const PASSWORD = 'browser check password';
const SECRET = '3132333435363738393031323334353637383930';
const SIZES = [
    {name: 'phone', width: 390, height: 844, mobile: true},
    {name: 'laptop', width: 1280, height: 800},
];

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

// RFC 6238 with SHA-1, 30 s steps and 6 digits, as tunebox's crypto.totp.
function totp(step) {
    const counter = Buffer.alloc(8);
    counter.writeBigUInt64BE(BigInt(step));
    const mac = createHmac('sha1', Buffer.from(SECRET, 'hex')).update(counter).digest();
    const offset = mac[mac.length - 1] & 0x0f;
    return String((mac.readUInt32BE(offset) & 0x7fffffff) % 1000000).padStart(6, '0');
}

function station(id, name) {
    return [
        '[[channels]]',
        `id = "${id}"`,
        `name = "${name}"`,
        'source = { type = "triton", mount = "OWR_INTERNATIONAL" }',
        '[[channels.streams]]',
        'name = "Default"',
        `url = "https://example.invalid/${id}.aac"`,
        'codec = "aac"',
        '',
    ].join('\n');
}

async function startTunebox(folder, port) {
    writeFileSync(join(folder, 'tunebox.toml'), [
        '[server]',
        'host = "127.0.0.1"',
        `port = ${port}`,
        `data_dir = "${folder}"`,
        '',
        '[admin]',
        'enabled = true',
        `public_origin = "http://127.0.0.1:${port}"`,
        '',
        station('first', 'First Station'),
        station('second', 'Second Station'),
    ].join('\n'));
    const made = spawnSync(PYTHON, ['-c', [
        'import sys',
        'from pathlib import Path',
        'from tunebox.accounts import Accounts',
        'from tunebox.settings import Settings',
        'settings = Settings(Path(sys.argv[1]) / "tunebox.db")',
        'Accounts(settings).create(sys.argv[2], bytes.fromhex(sys.argv[3]), [], step=0)',
        'settings.close()',
    ].join('\n'), folder, PASSWORD, SECRET], {cwd: ROOT, encoding: 'utf8'});
    if (made.status !== 0) {
        throw new Error(`could not create the account: ${made.stderr}`);
    }
    const log = [];
    const child = spawn(PYTHON, ['-m', 'tunebox'], {
        cwd: ROOT,
        env: Object.assign({}, process.env, {TUNEBOX_CONFIG: join(folder, 'tunebox.toml'), LOG_LEVEL: 'info'}),
        stdio: ['ignore', 'pipe', 'pipe'],
    });
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
    child.kill();
    throw new Error(`tunebox did not start:\n${log.join('')}`);
}

async function channels(base) {
    const body = await (await fetch(`${base}/api/channels`)).json();
    return Array.isArray(body) ? body : body.channels;
}

async function run() {
    const folder = mkdtempSync(join(tmpdir(), 'tunebox-admin-'));
    const port = await freePort();
    const base = `http://127.0.0.1:${port}`;
    const failures = [];
    let tunebox = null;
    let browser = null;
    let steps = 0;
    try {
        tunebox = await startTunebox(folder, port);
        browser = await connect({port: DEVTOOLS_PORT});
        const {send, evaluate, on} = browser;
        await send('Page.enable');
        // Waits until the expression is true in the page; false after the
        // deadline.
        const until = async (expression, ms = 5000) => {
            const deadline = Date.now() + ms;
            while (Date.now() < deadline) {
                if (await evaluate(`Boolean(${expression})`)) {
                    return true;
                }
                await sleep(100);
            }
            return false;
        };
        const step = async (name, check) => {
            steps += 1;
            try {
                const problem = await check();
                if (problem) {
                    failures.push(`${name}: ${problem}`);
                }
            } catch (error) {
                failures.push(`${name}: ${error.message}`);
            }
        };
        const shoot = async (name) => {
            if (!SHOTS) {
                return;
            }
            mkdirSync(SHOTS, {recursive: true});
            const shot = await send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true});
            writeFileSync(join(SHOTS, `${name}.png`), Buffer.from(shot.result.data, 'base64'));
        };
        const noSideways = async (where) => {
            for (const size of SIZES) {
                await send('Emulation.setDeviceMetricsOverride', {
                    width: size.width, height: size.height, deviceScaleFactor: 1, mobile: Boolean(size.mobile),
                });
                await sleep(150);
                // Against the device's width, not innerWidth: a phone widens
                // its layout viewport to fit content that is too wide.
                const wide = await evaluate(`document.documentElement.scrollWidth - ${size.width}`);
                if (wide > 1) {
                    failures.push(`${where} at ${size.name}: ${wide} px sideways scroll`);
                }
                await shoot(`${where}-${size.name}`);
                if (SHOTS) {
                    await send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-color-scheme', value: 'dark'}]});
                    await shoot(`${where}-${size.name}-dark`);
                    await send('Emulation.setEmulatedMedia', {features: []});
                }
            }
        };
        const message = () => evaluate(`document.querySelector('.message').textContent`);
        // Clicks, then waits for the page's answer to that save.
        const saveBy = async (selector) => {
            await evaluate(`document.querySelector('.message').textContent = ''`);
            await click(selector);
            if (!(await until(`document.querySelector('.message').textContent !== ''`))) {
                throw new Error(`no answer after ${selector}`);
            }
            return message();
        };
        const click = (selector) => evaluate(`(() => {
            const node = document.querySelector(${JSON.stringify(selector)});
            if (!node) { throw new Error('no ${selector.replace(/'/g, '')}'); }
            node.click();
            return true;
        })()`);

        await send('Page.navigate', {url: `${base}/admin/`});
        await step('login form', async () => (await until(`!document.querySelector('#login').hidden`)) ? null : 'not shown');
        await noSideways('login');
        // Password managers take the field before the password as the
        // account name; without one they take the code field.
        await step('forms name the account for password managers', async () => {
            const wrong = await evaluate(`['#login', '#setup'].filter((id) => {
                const inputs = [...document.querySelectorAll(id + ' input')];
                const name = inputs.findIndex((i) => i.autocomplete === 'username' && i.type !== 'hidden');
                const password = inputs.findIndex((i) => i.type === 'password');
                return name < 0 || name > password;
            })`);
            return wrong.length ? `no account field before the password in ${wrong.join(', ')}` : null;
        });

        let usedStep = 0;
        // A code from a 30 s step later than the last one used; waits for
        // the next step when needed.
        const freshCode = async () => {
            while (Math.floor(Date.now() / 30000) <= usedStep) {
                await sleep(500);
            }
            usedStep = Math.floor(Date.now() / 30000);
            return totp(usedStep);
        };
        const adminCall = (method, path, body) => evaluate(`(async () => {
            const state = await (await fetch('/admin/api/state')).json();
            const response = await fetch('/admin/api/' + ${JSON.stringify(path)}, {
                method: ${JSON.stringify(method)},
                headers: {'Content-Type': 'application/json', 'X-Tunebox-CSRF': state.session.csrf},
                body: ${body === undefined ? 'undefined' : JSON.stringify(JSON.stringify(body))},
            });
            return response.status;
        })()`);
        // Safe while a page loads: the keypad's container may not be there yet.
        const pinShown = `(document.querySelector('.pin-screen') && !document.querySelector('.pin-screen').hidden)`;
        const typePin = (digits) => evaluate(`(() => {
            for (const key of ${JSON.stringify([...digits, 'ok'])}) {
                document.querySelector('.pin-keys [data-key="' + key + '"]').click();
            }
            return true;
        })()`);
        const buttonNamed = (text) => evaluate(`(() => {
            const found = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === ${JSON.stringify(text)});
            if (!found) { throw new Error('no button ' + ${JSON.stringify(text)}); }
            found.click();
            return true;
        })()`);

        // Authenticator autofill (1Password on Android) fills a code only in
        // a form of its own; with the password beside it, it copies the code.
        const toCode = async (password = PASSWORD) => {
            await evaluate(`(() => {
                const form = document.querySelector('#login');
                form.password.value = ${JSON.stringify(password)};
                form.requestSubmit();
            })()`);
            if (!(await until(`!document.querySelector('#code').hidden && document.querySelector('#login').hidden`))) {
                return 'the code form did not follow the password';
            }
            const inputs = await evaluate(`[...document.querySelectorAll('#code input')].map((i) => i.autocomplete)`);
            return inputs.join() === 'one-time-code' ? null : `the code form holds ${inputs.join() || 'nothing'}`;
        };
        await step('the code has a form of its own', toCode);
        await noSideways('code');

        await step('a failed login asks again from the password', async () => {
            await evaluate(`(() => {
                const form = document.querySelector('#code');
                form.code.value = '000000';
                form.requestSubmit();
            })()`);
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return 'the password form did not come back';
            }
            const kept = await evaluate(`document.querySelector('#login').password.value`);
            const said = await message();
            return said === 'Login failed.' && kept === PASSWORD ? null : `${said} / password kept: ${kept === PASSWORD}`;
        });

        await step('log in', async () => {
            // Logins are paced at one a second across all addresses.
            await sleep(1100);
            const problem = await toCode();
            if (problem) {
                return problem;
            }
            const code = await freshCode();
            await evaluate(`(() => {
                const form = document.querySelector('#code');
                form.code.value = ${JSON.stringify(code)};
                form.requestSubmit();
            })()`);
            if (!(await until(`document.querySelectorAll('.station').length === 2`))) {
                return `stations not listed (message: ${await message()})`;
            }
            return null;
        });
        await noSideways('stations');

        await step('switch the second station off', async () => {
            const said = await saveBy(`.station[data-id="second"] .switch`);
            if (!said.startsWith('Saved')) {
                return said;
            }
            for (let i = 0; i < 30; i++) {
                const ids = (await channels(base)).map((c) => c.id);
                if (ids.join() === 'first') {
                    return null;
                }
                await sleep(100);
            }
            return `player lists ${(await channels(base)).map((c) => c.id).join()}`;
        });

        await step('switch it on again and move it up', async () => {
            let said = await saveBy(`.station[data-id="second"] .switch`);
            if (!said.startsWith('Saved')) {
                return said;
            }
            said = await saveBy(`.station[data-id="second"] button[title="Move Second Station up"]`);
            if (!said.startsWith('Saved')) {
                return said;
            }
            for (let i = 0; i < 30; i++) {
                const ids = (await channels(base)).map((c) => c.id);
                if (ids.join() === 'second,first') {
                    return null;
                }
                await sleep(100);
            }
            return `player order ${(await channels(base)).map((c) => c.id).join()}`;
        });

        await step('a double click saves once', async () => {
            // Both clicks land before the first save answers.
            await evaluate(`(() => {
                const box = document.querySelector('.message');
                box.textContent = '';
                window.said = [];
                new MutationObserver(() => window.said.push(box.textContent)).observe(box, {childList: true, characterData: true, subtree: true});
                const down = document.querySelector('.station[data-id="second"] button[title="Move Second Station down"]');
                down.click();
                down.click();
            })()`);
            await until(`document.querySelector('.message').textContent !== ''`);
            await sleep(1000);
            const said = (await evaluate('window.said')).filter(Boolean);
            const ids = (await channels(base)).map((c) => c.id).join();
            return said.join() === 'Saved.' && ids === 'first,second' ? null : `said ${said.join(' | ')}; order ${ids}`;
        });

        await step('a save that cannot reach tunebox says so', async () => {
            await send('Network.enable');
            await send('Network.emulateNetworkConditions', {offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1});
            try {
                await saveBy(`.station[data-id="first"] .switch`);
            } finally {
                await send('Network.emulateNetworkConditions', {offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1});
            }
            const said = await message();
            if (said !== 'Could not reach tunebox.') {
                return `message: ${said}`;
            }
            const on = await evaluate(`document.querySelector('.station[data-id="first"] .switch').checked`);
            return on ? null : 'the switch shows off although nothing was saved';
        });

        // Requests to the config API pass through here while a step needs
        // to fail or replace one; decide(method) answers 'pass', 'fail' or
        // a status to answer with.
        const intercept = async (stage, decide) => {
            on('Fetch.requestPaused', (params) => {
                const verdict = decide(params.request.method);
                if (verdict === 'fail') {
                    send('Fetch.failRequest', {requestId: params.requestId, errorReason: 'ConnectionReset'});
                } else if (typeof verdict === 'number') {
                    send('Fetch.fulfillRequest', {
                        requestId: params.requestId,
                        responseCode: verdict,
                        responseHeaders: [{name: 'Content-Type', value: 'application/json'}],
                        body: Buffer.from(JSON.stringify({error: 'read_only'})).toString('base64'),
                    });
                } else {
                    send('Fetch.continueRequest', {requestId: params.requestId});
                }
            });
            await send('Fetch.enable', {patterns: [{urlPattern: '*/admin/api/config', requestStage: stage}]});
        };
        const stopIntercepting = async () => {
            await send('Fetch.disable');
            on('Fetch.requestPaused', null);
        };
        const order = () => evaluate(`[...document.querySelectorAll('.station')].map((s) => s.dataset.id).join()`);

        await step('a save whose answer is lost shows what is stored', async () => {
            // The server stores the move; its answer never arrives.
            await intercept('Response', (method) => (method === 'POST' ? 'fail' : 'pass'));
            try {
                await saveBy(`.station[data-id="first"] button[title="Move First Station down"]`);
                await until(`document.querySelector('.message').textContent.indexOf('may or may not') >= 0`);
            } finally {
                await stopIntercepting();
            }
            const said = await message();
            const shown = await order();
            return said.indexOf('may or may not') >= 0 && shown === 'second,first' ? null : `${said} / shows ${shown}`;
        });

        await step('a refused switch flips back', async () => {
            await intercept('Request', (method) => (method === 'POST' ? 503 : 'pass'));
            try {
                await saveBy(`.station[data-id="first"] .switch`);
            } finally {
                await stopIntercepting();
            }
            const said = await message();
            const on = await evaluate(`document.querySelector('.station[data-id="first"] .switch').checked`);
            return said.startsWith('tunebox cannot write') && on ? null : `${said} / switch on: ${on}`;
        });

        await step('a stored save whose reload fails is not called a failure', async () => {
            let posted = false;
            await intercept('Request', (method) => {
                if (method === 'POST') {
                    posted = true;
                    return 'pass';
                }
                return posted ? 'fail' : 'pass';
            });
            let said = '';
            try {
                said = await saveBy(`.station[data-id="first"] button[title="Move First Station up"]`);
            } finally {
                await stopIntercepting();
            }
            const ids = (await channels(base)).map((c) => c.id).join();
            await send('Page.reload');
            await until(`document.querySelectorAll('.station').length === 2`);
            return said.startsWith('Saved') && ids === 'first,second' ? null : `${said} / player ${ids}`;
        });

        const editing = () => evaluate(`Boolean(document.querySelector('form.edit')) && location.hash === '#edit/first'`);
        const asked = () => evaluate(`Boolean(document.querySelector('form.edit .confirm'))`);
        const answer = (label) => evaluate(`[...document.querySelectorAll('form.edit .confirm button')].find((b) => b.textContent.trim() === ${JSON.stringify(label)}).click()`);

        await step('an unchanged editor leaves without asking', async () => {
            await until(`document.querySelector('.station[data-id="first"] button[title="Edit First Station"]')`);
            await click(`.station[data-id="first"] button[title="Edit First Station"]`);
            await until(`document.querySelector('form.edit')`);
            // Typed and typed back is no change.
            await evaluate(`(() => {
                const name = document.querySelector('form.edit').elements.name;
                name.value = 'Something else';
                name.dispatchEvent(new Event('input', {bubbles: true}));
                name.value = 'First Station';
                name.dispatchEvent(new Event('input', {bubbles: true}));
            })()`);
            await evaluate(`[...document.querySelectorAll('form.edit button')].find((b) => b.textContent.trim() === 'Cancel').click()`);
            return (await until(`location.hash === '#stations' && document.querySelector('.station')`)) ? null : 'stayed on the form';
        });

        await step('leaving the editor with unsaved changes asks first', async () => {
            await until(`document.querySelector('.station[data-id="first"] button[title="Edit First Station"]')`);
            await click(`.station[data-id="first"] button[title="Edit First Station"]`);
            await until(`document.querySelector('form.edit')`);
            await evaluate(`document.querySelector('form.edit').elements.name.value = 'Not saved'`);
            await click(`#shell nav a[href="#history"]`);
            await sleep(300);
            if (!(await editing()) || !(await asked())) {
                return 'the menu link left without asking';
            }
            await answer('Keep editing');
            if (await asked()) {
                return 'the question stayed after Keep editing';
            }
            await evaluate('history.back()');
            await sleep(300);
            if (!(await editing()) || !(await asked())) {
                return 'the back button left without asking';
            }
            await answer('Keep editing');
            const kept = await evaluate(`document.querySelector('form.edit').elements.name.value`);
            if (kept !== 'Not saved') {
                return `the form lost what was typed (${kept})`;
            }
            const leaves = await evaluate(`(() => {
                const event = new Event('beforeunload', {cancelable: true});
                window.dispatchEvent(event);
                return event.defaultPrevented;
            })()`);
            if (!leaves) {
                return 'a reload would leave without asking';
            }
            await evaluate(`[...document.querySelectorAll('form.edit .actions button')].find((b) => b.textContent.trim() === 'Cancel').click()`);
            await sleep(300);
            if (!(await asked())) {
                return 'Cancel left without asking';
            }
            await answer('Leave without saving');
            if (!(await until(`location.hash === '#stations' && document.querySelector('.station')`))) {
                return 'Leave without saving stayed on the form';
            }
            const names = (await channels(base)).map((c) => c.name).join();
            return names.indexOf('Not saved') >= 0 ? 'the change was saved' : null;
        });

        await step('rename the first station', async () => {
            await until(`document.querySelector('.station[data-id="first"] button[title="Edit First Station"]')`);
            await click(`.station[data-id="first"] button[title="Edit First Station"]`);
            if (!(await until(`document.querySelector('form.edit')`))) {
                return 'no form';
            }
            await noSideways('edit');
            // A save from elsewhere in the meantime: the form must keep what
            // was typed, and a second Save must go through.
            await evaluate(`(async () => {
                const csrf = (await (await fetch('/admin/api/state')).json()).session.csrf;
                const config = await (await fetch('/admin/api/config')).json();
                await fetch('/admin/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-Tunebox-CSRF': csrf},
                    body: JSON.stringify({based_on: config.version, state: config.state}),
                });
            })()`);
            await evaluate(`(() => {
                const form = document.querySelector('form.edit');
                form.elements.name.value = 'Renamed Station';
                form.requestSubmit();
            })()`);
            if (!(await until(`document.querySelector('.message').textContent.indexOf('Changed elsewhere') >= 0`))) {
                return `no stale answer (message: ${await message()})`;
            }
            const kept = await evaluate(`document.querySelector('form.edit') && document.querySelector('form.edit').elements.name.value`);
            if (kept !== 'Renamed Station') {
                return `the form lost what was typed (${kept})`;
            }
            await evaluate(`document.querySelector('form.edit').requestSubmit()`);
            const deadline = Date.now() + 2000;
            while (Date.now() < deadline) {
                if ((await channels(base)).some((c) => c.id === 'first' && c.name === 'Renamed Station')) {
                    return null;
                }
                await sleep(100);
            }
            return `name not live within 2 s (message: ${await message()})`;
        });

        await step('a dangerous title rule is refused', async () => {
            await until(`location.hash === '#stations' && document.querySelector('.station[data-id="first"]')`);
            await click(`.station[data-id="first"] button[title="Edit Renamed Station"]`);
            await until(`document.querySelector('form.edit')`);
            await evaluate(`(() => {
                const form = document.querySelector('form.edit');
                form.elements['rules.played_by'].value = '(a+)+$';
                form.requestSubmit();
            })()`);
            if (!(await until(`document.querySelector('form.edit [role=alert]') && document.querySelector('form.edit [role=alert]').textContent.indexOf('nested repetition') >= 0`))) {
                return `no refusal shown (message: ${await message()})`;
            }
            const stored = await evaluate(`fetch('/admin/api/config').then((r) => r.json()).then((d) => JSON.stringify(d.state.channels.map((c) => c.doc.rules || null)))`);
            if (stored.indexOf('(a+)+') >= 0) {
                return 'the pattern was stored';
            }
            // The refused pattern is still in the form, unsaved.
            await evaluate(`[...document.querySelectorAll('form.edit .actions button')].find((b) => b.textContent.trim() === 'Cancel').click()`);
            await until(`document.querySelector('form.edit .confirm')`);
            await answer('Leave without saving');
            return (await until(`location.hash === '#stations'`)) ? null : 'could not leave the form';
        });

        await step('restore the version before the rename', async () => {
            await send('Page.navigate', {url: `${base}/admin/#history`});
            if (!(await until(`document.querySelectorAll('.history a').length >= 4`))) {
                return 'history not listed';
            }
            await noSideways('history');
            const ids = await evaluate(`[...document.querySelectorAll('.history a')].map((a) => a.getAttribute('href'))`);
            const sorted = [...ids].sort((x, y) => Number(y.split('/')[1]) - Number(x.split('/')[1]));
            if (ids.join() !== sorted.join()) {
                return `not newest first: ${ids.join()}`;
            }
            // The newest entry is the rename.
            await click('.history a');
            if (!(await until(`document.querySelector('.diff') && document.querySelector('.diff').textContent.indexOf('Renamed Station') >= 0`))) {
                return 'the rename is not in the newest entry';
            }
            await noSideways('change');
            await evaluate(`[...document.querySelectorAll('button')].find((b) => b.textContent.indexOf('Restore the version before') >= 0).click()`);
            if (!(await until(`[...document.querySelectorAll('.restore button')].some((b) => b.textContent.trim() === 'Restore')`))) {
                return 'no confirmation';
            }
            await evaluate(`[...document.querySelectorAll('.restore button')].find((b) => b.textContent.trim() === 'Restore').click()`);
            for (let i = 0; i < 30; i++) {
                if ((await channels(base)).some((c) => c.id === 'first' && c.name === 'First Station')) {
                    return null;
                }
                await sleep(100);
            }
            return `old name not back (message: ${await message()})`;
        });

        await step('settings save the defaults and the start station', async () => {
            await evaluate(`window.location.hash = '#settings'`);
            if (!(await until(`document.querySelector('form.settings [name=delay_s]')`))) {
                return 'settings page not shown';
            }
            await evaluate(`(() => {
                const form = document.querySelector('form.settings');
                form.delay_s.value = '9';
                form.default_channel.value = 'second';
            })()`);
            const said = await saveBy('form.settings button[type=submit]');
            const player = await evaluate(`fetch('/api/player').then((r) => r.json())`);
            return said === 'Settings saved.' && player.default_channel === 'second'
                ? null : `${said} / default ${player.default_channel}`;
        });
        // The PIN form loads after the page: measure with it in place.
        await until(`document.querySelector('form.pin [name=pin_mode]')`);
        await noSideways('settings');

        await step('half a location is refused on the page', async () => {
            await evaluate(`(() => {
                const form = document.querySelector('form.settings');
                form.latitude.value = '52.1';
                form.requestSubmit();
            })()`);
            const shown = await until(`!document.querySelector('form.settings .error').hidden`);
            await evaluate(`document.querySelector('form.settings').latitude.value = ''`);
            return shown ? null : 'no error for a latitude without a longitude';
        });

        const savePin = async (mode, digits) => {
            await send('Page.navigate', {url: `${base}/admin/#settings`});
            if (!(await until(`document.querySelector('form.pin [name=pin_mode]')`))) {
                throw new Error('PIN form not shown');
            }
            await evaluate(`(() => {
                const form = document.querySelector('form.pin');
                form.pin_mode.value = ${JSON.stringify(mode)};
                form.pin.value = ${JSON.stringify(digits || '')};
            })()`);
            return saveBy('form.pin button[type=submit]');
        };

        await step('lock the whole player', async () => {
            const said = await savePin('site', '123456');
            return said === 'PIN saved. The player asks for it now.' ? null : said;
        });

        await step('a locked site shows only the PIN page', async () => {
            await send('Page.navigate', {url: `${base}/`});
            if (!(await until(pinShown))) {
                return 'no PIN screen';
            }
            const player = await evaluate(`Boolean(document.querySelector('.main-container'))`);
            const status = await evaluate(`fetch('/api/channels').then((r) => r.status)`);
            return !player && status === 401 ? null : `player markup: ${player}, channels answered ${status}`;
        });

        await step('the PIN page fits the car', async () => {
            for (const size of [{width: 489, height: 518}, {width: 390, height: 844}, {width: 844, height: 390}]) {
                await send('Emulation.setDeviceMetricsOverride', {...size, deviceScaleFactor: 1, mobile: false});
                await sleep(150);
                const bad = await evaluate(`[...document.querySelectorAll('.pin-keys button, .pin-title')]
                    .map((node) => node.getBoundingClientRect())
                    .filter((r) => r.top < 0 || r.left < 0 || r.bottom > innerHeight || r.right > innerWidth).length`);
                const small = await evaluate(`Math.min(...[...document.querySelectorAll('.pin-keys button')]
                    .map((node) => node.getBoundingClientRect().height))`);
                if (bad || small < 44) {
                    return `${size.width}x${size.height}: ${bad} outside, smallest key ${small} px`;
                }
            }
            await send('Emulation.clearDeviceMetricsOverride');
            return null;
        });

        await step('a wrong PIN says so', async () => {
            await typePin('000000');
            const said = await until(`!document.querySelector('.pin-error').hidden`);
            const text = await evaluate(`document.querySelector('.pin-error').textContent`);
            return said && text.startsWith('Wrong PIN') ? null : `error shown: ${said}, "${text}"`;
        });

        await step('the right PIN opens the player', async () => {
            await typePin('123456');
            // The PIN page reloads into the player.
            if (!(await until(`document.querySelector('.main-container') && !(${pinShown}) && document.querySelector('.current-channel').textContent`, 8000))) {
                return 'the player did not open, or no station shown';
            }
            return null;
        });

        await step('a changed PIN asks again while listening', async () => {
            const changed = await adminCall('POST', 'pin', {mode: 'site', pin: '7654321'});
            if (changed !== 200) {
                return `the PIN change answered ${changed}`;
            }
            // The next poll of what is playing comes within 10 s.
            if (!(await until(pinShown, 15000))) {
                return 'no PIN keypad after the PIN changed';
            }
            await typePin('7654321');
            return (await until(`!(${pinShown})`)) ? null : 'the new PIN did not close the keypad';
        });

        await step('chosen stations hide the marked one', async () => {
            await send('Page.navigate', {url: `${base}/admin/#edit/second`});
            if (!(await until(`document.querySelector('form.edit [name=needs_pin]')`))) {
                return 'no "Needs the PIN" box';
            }
            await evaluate(`(() => {
                const form = document.querySelector('form.edit');
                form.elements.needs_pin.checked = true;
                form.requestSubmit();
            })()`);
            const mark = `document.querySelector('.station[data-id="second"] .id')`;
            if (!(await until(`${mark} && ${mark}.textContent.includes('PIN')`))) {
                return `not marked in the list (message: ${await message()})`;
            }
            const said = await savePin('stations');
            if (!said.startsWith('PIN saved. Stations marked')) {
                return said;
            }
            // A device without the grant.
            await send('Network.enable');
            await send('Network.deleteCookies', {name: 'tunebox_grant', url: `${base}/`});
            await send('Page.navigate', {url: `${base}/`});
            if (!(await until(`document.querySelector('.channel-groups .channel')`))) {
                return 'player not shown';
            }
            const listed = await evaluate(`[...document.querySelectorAll('.channel-groups .channel')].map((b) => b.dataset.id)`);
            const offered = await evaluate(`Boolean(document.querySelector('.more-stations'))`);
            if (listed.join() !== 'first' || !offered) {
                return `listed ${listed.join()}, "More stations" offered: ${offered}`;
            }
            await click('.more-stations');
            if (!(await until(pinShown))) {
                return 'no keypad for more stations';
            }
            await click('.pin-cancel');
            if (!(await until(`!(${pinShown})`))) {
                return 'Cancel did not close the keypad';
            }
            await click('.more-stations');
            await until(pinShown);
            await typePin('7654321');
            if (!(await until(`document.querySelectorAll('.channel-groups .channel').length === 2 && !document.querySelector('.more-stations')`))) {
                return 'the hidden station did not appear after the PIN';
            }
            return null;
        });

        await step('a playing hidden station asks again after a PIN change', async () => {
            await click('.channel-groups .channel[data-id="second"]');
            if (!(await until(`document.querySelector('.current-channel').textContent === 'Second Station'`))) {
                return 'could not pick the hidden station';
            }
            const changed = await adminCall('POST', 'pin', {mode: 'stations', pin: '24682468'});
            if (changed !== 200) {
                return `the PIN change answered ${changed}`;
            }
            // Its next poll finds it gone; the keypad offers it back.
            if (!(await until(pinShown, 15000))) {
                return 'no keypad after the PIN changed';
            }
            await typePin('24682468');
            if (!(await until(`!(${pinShown})`))) {
                return 'the new PIN did not close the keypad';
            }
            const polls = `performance.getEntriesByType('resource').filter((e) => e.name.includes('/api/channels/second/now')).length`;
            const before = await evaluate(polls);
            // Titles resume: the station is polled again within one interval.
            return (await until(`${polls} > ${before}`, 15000)) ? null : 'the hidden station is no longer polled after the PIN';
        });

        await step('switch the PIN off in the panel', async () => {
            const said = await savePin('off');
            const status = await evaluate(`fetch('/api/channels').then((r) => r.status)`);
            await evaluate(`window.location.hash = '#stations'`);
            return said === 'PIN saved. The player is open to everyone.' && status === 200 ? null : `${said} / ${status}`;
        });

        await step('the account page lists this browser', async () => {
            await evaluate(`window.location.hash = '#account'`);
            if (!(await until(`document.querySelectorAll('table.sessions tbody tr').length === 1`))) {
                return 'sessions not listed';
            }
            const text = await evaluate(`document.querySelector('table.sessions tbody').textContent`);
            return text.includes('This browser') ? null : text;
        });
        await noSideways('account');

        await step('new recovery codes ask for a code first', async () => {
            await buttonNamed('Make new recovery codes');
            if (!(await until(`document.querySelector('form.confirm-code')`))) {
                return 'no code asked';
            }
            const code = await freshCode();
            await evaluate(`(() => {
                const form = document.querySelector('form.confirm-code');
                form.code.value = ${JSON.stringify(code)};
                form.requestSubmit();
            })()`);
            if (!(await until(`document.querySelectorAll('ul.recovery li').length === 10`))) {
                return `no codes shown (message: ${await message()})`;
            }
            return null;
        });

        await step('change the password without a second code', async () => {
            await evaluate(`(() => {
                const inputs = document.querySelectorAll('[name="new-password"], [name="new-password-again"]');
                for (const input of inputs) {
                    input.value = 'a new browser check password';
                }
                inputs[0].form.requestSubmit();
            })()`);
            if (!(await until(`document.querySelector('.message').textContent.startsWith('Password changed')`))) {
                return `not changed (message: ${await message()})`;
            }
            return null;
        });
        await step('log out', async () => {
            await click('.logout');
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return 'login form not shown';
            }
            const status = await evaluate(`fetch('/admin/api/config').then((r) => r.status)`);
            return status === 401 ? null : `config answered ${status} after logout`;
        });

        await step('log in with the new password', async () => {
            await sleep(1100);
            const problem = await toCode('a new browser check password');
            if (problem) {
                return problem;
            }
            const code = await freshCode();
            await evaluate(`(() => {
                const form = document.querySelector('#code');
                form.code.value = ${JSON.stringify(code)};
                form.requestSubmit();
            })()`);
            return (await until(`!document.querySelector('#shell').hidden`)) ? null : `not logged in (message: ${await message()})`;
        });
    } finally {
        if (browser) {
            for (const error of browser.errors) {
                failures.push(`page error: ${error}`);
            }
            await browser.close();
        }
        if (tunebox) {
            const exited = new Promise((done) => tunebox.child.once('exit', done));
            tunebox.child.kill('SIGTERM');
            await Promise.race([exited, sleep(5000)]);
            const errors = tunebox.log.join('').split('\n').filter((line) => /"level": ?"(error|critical)"|Traceback/i.test(line));
            for (const line of errors) {
                failures.push(`tunebox: ${line}`);
            }
        }
        rmSync(folder, {recursive: true, force: true});
    }
    console.log(`${steps} steps checked`);
    if (failures.length) {
        console.log(`${failures.length} problem(s):`);
        for (const failure of failures) {
            console.log(`  ${failure}`);
        }
        process.exit(1);
    }
    console.log('the admin flow works');
}

run().catch((error) => {
    console.error(error);
    process.exit(2);
});
