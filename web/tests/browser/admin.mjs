// Browser check of the admin panel: log in with a password and a TOTP code,
// then switch a station off, reorder, edit, have a dangerous title rule
// refused, restore from the history, change the settings, lock the player
// with a PIN and pass it, hide a chosen station behind it, ask for a code
// before a PIN change or before showing that station again, confirm account
// changes with a code, see a listener on the Usage page, rename and delete
// its device, change the usage settings, export and import the stations,
// upload a logo, give a group an image, find a song on the Covers page, fix
// its cover from a search and an upload, and undo it, change the password,
// log out and log in with it,
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
//
// For a failure that comes and goes, DEBUG_TUNEBOX_LOG=<file> runs tunebox
// at debug level, writes its whole log there when a step fails, and adds the
// page's hash, open section and view to each failure.

import {spawn, spawnSync} from 'node:child_process';
import {createHmac} from 'node:crypto';
import {mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from 'node:fs';
import {createServer as createHttpServer} from 'node:http';
import {createServer as createHttpsServer} from 'node:https';
import {createServer} from 'node:net';
import os, {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {deflateSync} from 'node:zlib';

import {alive, connect, killGroup, sleep} from './cdp.mjs';

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
function totp(step, key = Buffer.from(SECRET, 'hex')) {
    const counter = Buffer.alloc(8);
    counter.writeBigUInt64BE(BigInt(step));
    const mac = createHmac('sha1', key).update(counter).digest();
    const offset = mac[mac.length - 1] & 0x0f;
    return String((mac.readUInt32BE(offset) & 0x7fffffff) % 1000000).padStart(6, '0');
}

// The bytes of an RFC 4648 base32 text, as the setup page shows its secret.
function base32(text) {
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
    let bits = '';
    for (const letter of text.replace(/[\s=]/g, '').toUpperCase()) {
        bits += alphabet.indexOf(letter).toString(2).padStart(5, '0');
    }
    const bytes = [];
    for (let i = 0; i + 8 <= bits.length; i += 8) {
        bytes.push(parseInt(bits.slice(i, i + 8), 2));
    }
    return Buffer.from(bytes);
}

function station(id, name, url = `https://example.invalid/${id}.aac`) {
    return [
        '[[channels]]',
        `id = "${id}"`,
        `name = "${name}"`,
        'source = { type = "triton", mount = "OWR_INTERNATIONAL" }',
        '[[channels.streams]]',
        'name = "Default"',
        `url = "${url}"`,
        '',
    ].join('\n');
}

// A local HTTPS stream of silent MP3 frames, sent at about real time after a
// short burst, so a page can really play something without the network.
// tunebox accepts only https stream URLs; the page is told to accept this
// one's throwaway certificate.
function silentStream(folder) {
    const key = join(folder, 'stream.key');
    const cert = join(folder, 'stream.crt');
    const made = spawnSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-subj', '/CN=localhost', '-keyout', key, '-out', cert], {encoding: 'utf8'});
    if (made.error) {
        throw new Error(`openssl is needed to make the throwaway certificate for the local stream (${made.error.code === 'ENOENT' ? 'not found on PATH' : made.error.message})`);
    }
    if (made.status !== 0) {
        throw new Error(`could not make a certificate for the local stream: ${made.stderr}`);
    }
    const frame = Buffer.alloc(417);
    frame.set([0xff, 0xfb, 0x90, 0x00]);
    const sockets = new Set();
    const server = createHttpsServer({key: readFileSync(key), cert: readFileSync(cert)}, (_request, response) => {
        response.writeHead(200, {'Content-Type': 'audio/mpeg', 'Access-Control-Allow-Origin': '*'});
        sockets.add(response);
        const send = (frames) => response.write(Buffer.concat(Array(frames).fill(frame)));
        send(80);
        const timer = setInterval(() => send(38), 1000);
        response.on('close', () => {
            clearInterval(timer);
            sockets.delete(response);
        });
    });
    return new Promise((done) => server.listen(0, '127.0.0.1', () => done({
        url: `https://127.0.0.1:${server.address().port}/live.mp3`,
        close: () => {
            for (const response of sockets) {
                response.destroy();
            }
            server.close();
        },
    })));
}

// The Deezer and iTunes APIs as tunebox's Covers search reads them
// (TUNEBOX_CATALOGS), with one album and two artist profiles. The images
// they name are dzcdn links, which the page's requests answer locally.
function localCatalogue() {
    const image = (kind, id, size) => `https://cdn-images.dzcdn.net/images/${kind}/${id.repeat(32)}/${size}x${size}-000000-80-0-0.jpg`;
    const album = {
        id: 655089251,
        title: 'Collide',
        cover_xl: image('cover', '1', 1000),
        cover_medium: image('cover', '1', 250),
        artist: {name: 'AGENTS OF TIME'},
    };
    const answer = (path) => {
        if (path === '/deezer/search') {
            return {data: [{title: 'Hearts Collide', artist: album.artist, album}]};
        }
        if (path === '/deezer/search/artist') {
            return {data: [
                {id: 11, name: 'Jessica', nb_fan: 12345, picture_xl: image('artist', '2', 1000), picture_medium: image('artist', '2', 250)},
                {id: 12, name: 'Jessica Mauboy', nb_fan: 678, picture_xl: image('artist', '3', 1000), picture_medium: image('artist', '3', 250)},
            ]};
        }
        if (/^\/deezer\/artist\/[0-9]+\/albums$/.test(path)) {
            return {data: []};
        }
        if (/^\/deezer\/album\/[0-9]+$/.test(path)) {
            return album;
        }
        if (path === '/itunes/search') {
            return {results: []};
        }
        return null;
    };
    const server = createHttpServer((request, response) => {
        const found = answer(new URL(request.url, 'http://catalogue').pathname);
        response.writeHead(found ? 200 : 404, {'Content-Type': 'application/json'});
        response.end(JSON.stringify(found || {error: {code: 800}}));
    });
    return new Promise((done) => server.listen(0, '127.0.0.1', () => done({
        url: `http://127.0.0.1:${server.address().port}`,
        close: () => server.close(),
    })));
}

// Data folders made by this check; each holds owner.json naming the node
// process and, once started, its tunebox.
const FOLDER_PREFIXES = ['tunebox-admin-', 'tunebox-setup-', 'tunebox-transfer-'];

function ownedFolder(prefix) {
    const folder = mkdtempSync(join(tmpdir(), prefix));
    writeFileSync(join(folder, 'owner.json'), JSON.stringify({node: process.pid}));
    return folder;
}

// A check killed outright (SIGKILL, a timeout) runs no exit handler; the
// next run stops the tunebox it left and removes its folders. The tunebox
// pid is killed only while its environment still names that folder's
// config (its command line does not name the folder), so a reused pid is
// never hit.
function sweepStaleFolders() {
    for (const name of readdirSync(tmpdir())) {
        if (!FOLDER_PREFIXES.some((prefix) => name.startsWith(prefix))) {
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
        if (owner.tunebox) {
            try {
                const environment = readFileSync(`/proc/${owner.tunebox}/environ`, 'utf8').split('\0');
                if (environment.includes(`TUNEBOX_CONFIG=${join(folder, 'tunebox.toml')}`)) {
                    killGroup(owner.tunebox, 'SIGKILL');
                }
            } catch (error) {
                // tunebox is gone too.
            }
        }
        try {
            rmSync(folder, {recursive: true, force: true, maxRetries: 5, retryDelay: 200});
        } catch (error) {
            // Left for the next sweep.
        }
    }
}

// setup: no account yet and the setup page on, as a first start would be.
// catalogueUrl: where the Covers search finds Deezer and iTunes.
async function startTunebox(folder, port, streamUrl, setup = false, catalogueUrl = null) {
    writeFileSync(join(folder, 'tunebox.toml'), [
        '[server]',
        'host = "127.0.0.1"',
        `port = ${port}`,
        `data_dir = "${folder}"`,
        '',
        '[admin]',
        'enabled = true',
        `setup_page = ${setup}`,
        `public_origin = "http://127.0.0.1:${port}"`,
        '',
        station('first', 'First Station', streamUrl),
        station('second', 'Second Station'),
    ].join('\n'));
    const made = setup ? {status: 0} : spawnSync(PYTHON, ['-c', [
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
    // Two songs for the Covers page: one first heard over a day ago with
    // no cover found yet, and one showing an artist photo.
    const seeded = setup ? {status: 0} : spawnSync(PYTHON, ['-c', [
        'import sys, time',
        'from pathlib import Path',
        'from tunebox.store import CoverDecision, Store',
        'from tunebox.tracks import RawEntry, Rules, clean',
        'now = int(time.time() * 1000)',
        'store = Store(Path(sys.argv[1]) / "tunebox.db")',
        'songs = [clean(RawEntry("AGENTS OF TIME", "Hearts Collide", now - 90000000), Rules(), now),',
        '         clean(RawEntry("JESSICA", "Summer Song", now - 60000), Rules(), now)]',
        'store.record_heard("first", songs)',
        'store.put_cover(CoverDecision(songs[1].key, "artist", "https://cdn-images.dzcdn.net/images/artist/' + '0'.repeat(32) + '/1000x1000-000000-80-0-0.jpg", "deezer artist: Jessica", time.time()))',
        'store.close()',
    ].join('\n'), folder], {cwd: ROOT, encoding: 'utf8'});
    if (seeded.status !== 0) {
        throw new Error(`could not seed the song list: ${seeded.stderr}`);
    }
    const log = [];
    // Its own process group, so one signal also reaches the title-rule
    // checks it runs in child processes.
    const child = spawn(PYTHON, ['-m', 'tunebox'], {
        cwd: ROOT,
        // Usage is written every second, so the Usage page's devices show
        // within the check's waits.
        env: Object.assign({}, process.env, {
            TUNEBOX_CONFIG: join(folder, 'tunebox.toml'),
            LOG_LEVEL: process.env.DEBUG_TUNEBOX_LOG ? 'debug' : 'info',
            TUNEBOX_USAGE_FLUSH_S: '1',
        }, catalogueUrl ? {TUNEBOX_CATALOGS: catalogueUrl} : {}),
        stdio: ['ignore', 'pipe', 'pipe'],
        detached: true,
    });
    writeFileSync(join(folder, 'owner.json'), JSON.stringify({node: process.pid, tunebox: child.pid}));
    // A check that exits from anywhere takes its tunebox along, stopped
    // before any exit handler removes its folder.
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

// Error lines from tunebox's log, and each traceback whole: a traceback is
// printed outside the JSON log (by socketserver, or at exit), and its first
// line alone says nothing about what failed.
function logProblems(text) {
    const problems = [];
    let traceback = null;
    for (const line of text.split('\n')) {
        if (traceback !== null) {
            if (line.startsWith(' ') || line.startsWith('Traceback') || (line && !line.startsWith('{'))) {
                traceback.push(line);
                continue;
            }
            problems.push(traceback.join('\n'));
            traceback = null;
        }
        if (/Traceback/.test(line)) {
            traceback = [line];
        } else if (/"level": ?"(error|critical)"/i.test(line)) {
            problems.push(line);
        }
    }
    if (traceback !== null) {
        problems.push(traceback.join('\n'));
    }
    return problems;
}

async function channels(base) {
    const body = await (await fetch(`${base}/api/channels`)).json();
    return Array.isArray(body) ? body : body.channels;
}

async function run() {
    sweepStaleFolders();
    // An interrupt runs the exit handlers: tunebox is in its own process
    // group, out of reach of the terminal's signal.
    for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
        if (process.listenerCount(signal) === 0) {
            process.once(signal, () => process.exit(128 + os.constants.signals[signal]));
        }
    }
    const folder = ownedFolder('tunebox-admin-');
    const port = await freePort();
    const base = `http://127.0.0.1:${port}`;
    const failures = [];
    let tunebox = null;
    let browser = null;
    let stream = null;
    let catalogue = null;
    let steps = 0;
    // A check interrupted mid-run skips the finally below; its data folder
    // goes with the process all the same (startTunebox stops tunebox).
    process.once('exit', () => {
        rmSync(folder, {recursive: true, force: true});
    });
    try {
        stream = await silentStream(folder);
        catalogue = await localCatalogue();
        tunebox = await startTunebox(folder, port, stream.url, false, catalogue.url);
        browser = await connect({port: DEVTOOLS_PORT});
        const {send, evaluate, on} = browser;
        await send('Page.enable');
        await send('Security.setIgnoreCertificateErrors', {ignore: true});
        // Waits until the expression is true in the page; false after the
        // deadline.
        // An expression that throws is not true yet: right after a
        // navigation the old or a blank document can still be in place.
        const until = async (expression, ms = 5000) => {
            const deadline = Date.now() + ms;
            while (Date.now() < deadline) {
                try {
                    if (await evaluate(`Boolean(${expression})`)) {
                        return true;
                    }
                } catch (error) {
                    // Not there yet.
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
            if (process.env.DEBUG_TUNEBOX_LOG && failures.length && failures[failures.length - 1].startsWith(name)) {
                try {
                    failures.push(`  page: ${await evaluate(`JSON.stringify({hash: location.hash, shown: [...document.querySelectorAll('main > form, main > section')].filter((n) => !n.hidden).map((n) => n.id), view: (document.querySelector('#shell .view') || {}).innerHTML ? document.querySelector('#shell .view').innerHTML.slice(0, 400) : null, message: (document.querySelector('.message') || {}).textContent, at: Date.now()})`)}`);
                } catch (error) {
                    failures.push(`  page: ${error.message}`);
                }
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
            let body = ${body === undefined ? 'undefined' : JSON.stringify(JSON.stringify(body))};
            if (${JSON.stringify(path)} === 'pin' && body !== undefined) {
                // A PIN change names the revision it is based on.
                const current = await (await fetch('/admin/api/pin')).json();
                body = JSON.stringify({...JSON.parse(body), based_on: current.revision});
            }
            const response = await fetch('/admin/api/' + ${JSON.stringify(path)}, {
                method: ${JSON.stringify(method)},
                headers: {'Content-Type': 'application/json', 'X-Tunebox-CSRF': state.session.csrf},
                body,
            });
            return response.status;
        })()`);
        // A PIN change through the API on the page's session; it confirms a
        // fresh code first when the session has none from the last 5 minutes.
        const pinCall = async (body) => {
            const status = await adminCall('POST', 'pin', body);
            if (status !== 403) {
                return status;
            }
            const confirmed = await adminCall('POST', 'account/reauth', {code: await freshCode()});
            return confirmed === 204 ? adminCall('POST', 'pin', body) : `reauth ${confirmed}`;
        };
        // Types a fresh code into the code form the page shows.
        const typeCode = async () => {
            const code = await freshCode();
            await evaluate(`(() => {
                const form = document.querySelector('form.confirm-code');
                form.code.value = ${JSON.stringify(code)};
                form.requestSubmit();
            })()`);
        };
        // Clicks, gives a fresh code when the page asks for one, then waits
        // for the page's answer to that save.
        const saveCoded = async (selector) => {
            await evaluate(`document.querySelector('.message').textContent = ''`);
            await click(selector);
            if (!(await until(`document.querySelector('form.confirm-code') || document.querySelector('.message').textContent !== ''`))) {
                throw new Error(`no answer after ${selector}`);
            }
            if (await evaluate(`Boolean(document.querySelector('form.confirm-code'))`)) {
                await typeCode();
                if (!(await until(`document.querySelector('.message').textContent !== ''`))) {
                    throw new Error(`no answer after the code for ${selector}`);
                }
            }
            return message();
        };
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
        // Logs the page out and in again: a new login holds no fresh code,
        // so the next change that needs one asks for it.
        const newLogin = async (password) => {
            await click('.logout');
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return 'log out did not show the login';
            }
            await sleep(1100);
            const problem = await toCode(password);
            if (problem) {
                return problem;
            }
            const code = await freshCode();
            await evaluate(`(() => {
                const form = document.querySelector('#code');
                form.code.value = ${JSON.stringify(code)};
                form.requestSubmit();
            })()`);
            return (await until(`!document.querySelector('#shell').hidden && document.querySelector('#shell .view').children.length`))
                ? null : `not logged in (message: ${await message()})`;
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
                    body: JSON.stringify({
                        based_on: config.version,
                        // A state equal to the stored one stores nothing, so change it.
                        state: {...config.state, defaults: {...config.state.defaults, delay_s: 8}},
                    }),
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

        await step('the station form folds what is rarely changed', async () => {
            await until(`location.hash === '#stations' && document.querySelector('.station[data-id="first"]')`);
            await click(`.station[data-id="first"] button[title="Edit Renamed Station"]`);
            await until(`document.querySelector('form.edit details.section')`);
            const open = await evaluate(`JSON.stringify([...document.querySelectorAll('form.edit details.section')].map((d) => [d.querySelector('h3').textContent, d.open, d.querySelector('.status').textContent]))`);
            const sections = JSON.parse(open);
            const shape = sections.map(([title, isOpen]) => `${title}:${isOpen ? 'open' : 'folded'}`).join(' ');
            if (shape !== 'Station:open Streams:open Now playing:folded Title rules:folded Timing:folded Options:folded') {
                return `sections ${shape}`;
            }
            if (sections.some(([title, isOpen, status]) => !isOpen && !status)) {
                return `a folded section says nothing: ${open}`;
            }
            await click(`form.edit details.section:nth-of-type(3) > summary`);
            if (!(await evaluate(`document.querySelectorAll('form.edit details.section')[2].open`))) {
                return 'Now playing did not open on a tap';
            }
            const helpButton = `form.edit details.section[open] button.help[title="About Mount"]`;
            if (!(await until(`document.querySelector('${helpButton}')`))) {
                return 'no ? beside Mount';
            }
            await click(helpButton);
            const card = `document.getElementById(document.querySelector('${helpButton}').getAttribute('aria-controls'))`;
            if (!(await evaluate(`!${card}.hidden && ${card}.textContent.indexOf('np.tritondigital.com') >= 0 && document.querySelector('${helpButton}').getAttribute('aria-expanded') === 'true'`))) {
                return 'the Mount card did not open';
            }
            await click(helpButton);
            if (!(await evaluate(`${card}.hidden`))) {
                return 'the Mount card did not close';
            }
            await evaluate(`[...document.querySelectorAll('form.edit .actions button')].find((b) => b.textContent.trim() === 'Cancel').click()`);
            return (await until(`location.hash === '#stations'`)) ? null : 'could not leave the form';
        });

        await step('a new station opens its feed settings', async () => {
            await evaluate(`window.location.hash = '#new'`);
            if (!(await until(`document.querySelector('form.edit details.section')`))) {
                return 'no new-station form';
            }
            const open = await evaluate(`[...document.querySelectorAll('form.edit details.section')].filter((d) => d.open).map((d) => d.querySelector('h3').textContent).join(', ')`);
            await evaluate(`window.location.hash = '#stations'`);
            await until(`location.hash === '#stations' && document.querySelector('.station[data-id="first"]')`);
            return open === 'Station, Streams, Now playing' ? null : `open: ${open}`;
        });

        await step('a new station fills in its feed and checks its stream', async () => {
            await evaluate(`window.location.hash = '#new'`);
            if (!(await until(`document.querySelector('form.edit fieldset.stream input[type=url]')`))) {
                return 'no new-station form';
            }
            const codecBefore = await evaluate(`document.querySelector('form.edit fieldset.stream select').value`);
            if (codecBefore !== '') {
                return `a new stream starts with codec ${codecBefore}, so a check cannot fill it`;
            }
            // Check feed with nothing filled in yet: a check asks for a fresh
            // code first, then the server names the missing setting.
            await evaluate(`[...document.querySelectorAll('form.edit button')].find((b) => b.textContent.trim() === 'Check feed').click()`);
            if (!(await until(`document.querySelector('form.edit details.section:nth-of-type(3) .check-result form.confirm-code')`))) {
                return `Check feed asked for no code (it said: ${await evaluate(`document.querySelector('form.edit details.section:nth-of-type(3) .check-result').textContent`)})`;
            }
            await typeCode();
            if (!(await until(`(document.querySelector('form.edit details.section:nth-of-type(3) .check-result') || {}).textContent.indexOf('source.mount') >= 0`))) {
                return `Check feed with no mount said: ${await evaluate(`document.querySelector('form.edit details.section:nth-of-type(3) .check-result').textContent`)}`;
            }
            await evaluate(`(() => {
                const url = document.querySelector('form.edit fieldset.stream input[type=url]');
                url.value = 'https://playerservices.streamtheworld.com/api/livestream-redirect/OWR_INTERNATIONAL_ADP.aac';
                url.dispatchEvent(new Event('input', {bubbles: true}));
                url.dispatchEvent(new Event('change', {bubbles: true}));
            })()`);
            const mount = await evaluate(`(document.querySelector('form.edit [name="source.mount"]') || {}).value`);
            if (mount !== 'OWR_INTERNATIONAL') {
                return `mount filled in as ${mount}`;
            }
            if (!(await evaluate(`document.querySelectorAll('form.edit details.section')[2].open`))) {
                return 'Now playing did not open when it was filled in';
            }
            // The test's own stream is on this machine: the guard refuses it.
            await evaluate(`(() => {
                const url = document.querySelector('form.edit fieldset.stream input[type=url]');
                url.value = ${JSON.stringify(stream.url)};
                url.dispatchEvent(new Event('input', {bubbles: true}));
                [...document.querySelectorAll('form.edit fieldset.stream button')].find((b) => b.textContent.trim() === 'Check stream').click();
            })()`);
            const said = `(document.querySelector('form.edit fieldset.stream .check-result') || {}).textContent || ''`;
            if (!(await until(`${said}.indexOf('not a public address') >= 0`, 8000))) {
                return `Check stream on a local address said: ${await evaluate(said)}`;
            }
            // With the minute's checks used up, the form says when to try again.
            let status = 0;
            for (let i = 0; i < 20 && status !== 429; i++) {
                status = await adminCall('POST', 'stream/check', {url: stream.url});
            }
            await evaluate(`[...document.querySelectorAll('form.edit fieldset.stream button')].find((b) => b.textContent.trim() === 'Check stream').click()`);
            if (!(await until(`/used up; try again in [0-9]+ s/.test(${said})`, 8000))) {
                return `Check stream past the minute's checks said: ${await evaluate(said)}`;
            }
            await evaluate(`window.location.hash = '#stations'`);
            await until(`document.querySelector('form.edit .confirm')`);
            await answer('Leave without saving');
            if (!(await until(`location.hash === '#stations' && document.querySelector('.station[data-id="first"]')`))) {
                return 'could not leave the form';
            }
            // The code typed for the checks would stay fresh into the steps
            // that expect to be asked for one.
            return newLogin(PASSWORD);
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
            if (!(await until(`document.querySelector('form.edit p.message') && document.querySelector('form.edit p.message').textContent.indexOf('nested repetition') >= 0`))) {
                return `no refusal shown (message: ${await message()})`;
            }
            const stored = await evaluate(`fetch('/admin/api/config').then((r) => r.json()).then((d) => JSON.stringify(d.state.channels.map((c) => c.doc.rules || null)))`);
            if (stored.indexOf('(a+)+') >= 0) {
                return 'the pattern was stored';
            }
            if (!(await evaluate(`document.querySelector('form.edit [name="rules.played_by"]').closest('details').open`))) {
                return 'the folded Title rules section stayed shut on the refusal';
            }
            if ((await evaluate(`document.activeElement && document.activeElement.name`)) !== 'rules.played_by') {
                return `the refused pattern did not get focus (focus on ${await evaluate(`document.activeElement ? document.activeElement.outerHTML.slice(0, 80) : 'none'`)}; message: ${await evaluate(`document.querySelector('form.edit p.message').textContent`)})`;
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
                    // The page reloads and moves to the history after the
                    // server answers; the next step must not start before.
                    return (await until(`document.querySelector('.message').textContent === 'Restored.'`))
                        ? null : `restored, but the page said: ${await message()}`;
                }
                await sleep(100);
            }
            return `old name not back (message: ${await message()})`;
        });

        // A page that waits for an answer must not draw once another page
        // is open: hold a History answer, open Settings, then let it go.
        await step('a slow answer does not draw over the page now open', async () => {
            await send('Fetch.enable', {patterns: [{urlPattern: '*/admin/api/history/*'}]});
            const paused = new Promise((resolve) => on('Fetch.requestPaused', resolve));
            try {
                await evaluate(`window.location.hash = '#history/1'`);
                const held = await Promise.race([paused, sleep(5000).then(() => null)]);
                if (!held) {
                    return 'the History answer was never asked for';
                }
                await evaluate(`window.location.hash = '#settings'`);
                if (!(await until(`document.querySelector('form.settings [name=delay_s]')`))) {
                    return 'settings page not shown';
                }
                await send('Fetch.continueRequest', {requestId: held.requestId});
                await sleep(500);
                return (await evaluate(`Boolean(document.querySelector('form.settings [name=delay_s]'))`))
                    ? null : 'the late History answer drew over the Settings page';
            } finally {
                on('Fetch.requestPaused', null);
                await send('Fetch.disable');
            }
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

        // The page itself plays the part of a player: loading / sets the
        // device cookie, and a poll with a stream is a listening session.
        const listen = () => evaluate(`fetch('/', {credentials: 'same-origin'}).then(() =>
            fetch('/api/channels/first/now?buffer_s=0&stream=Default', {credentials: 'same-origin'})).then((r) => r.status)`);
        const deviceRow = `document.querySelector('.usage-table.devices .open-device')`;

        await step('a listener shows on the Usage page', async () => {
            const status = await listen();
            if (status !== 200) {
                return `the poll answered ${status}`;
            }
            await evaluate(`window.location.hash = '#usage'`);
            return (await until(`[...document.querySelectorAll('.usage-now li')].some((n) => n.textContent.includes('First Station'))`, 8000))
                ? null : 'no live listener shown';
        });
        await until(`document.querySelector('.usage-summary .bars')`);
        await noSideways('usage');
        await step('the chart leaves the page bar alone', async () => {
            const paint = await evaluate(`getComputedStyle(document.querySelector('header.bar')).backgroundColor`);
            return paint === 'rgba(0, 0, 0, 0)' ? null : `the page bar is painted ${paint}`;
        });

        await step('rename the device that played', async () => {
            // A second poll keeps the session live, whatever the waits took.
            await listen();
            // The device is listed once the writer has stored its session;
            // the list loads with the page, so the page is drawn again (a
            // navigation to the same hash would not reload it).
            let listed = false;
            for (let i = 0; i < 6 && !listed; i++) {
                await sleep(1500);
                await evaluate(`window.location.hash = '#stations'`);
                await until(`document.querySelector('.station')`);
                await evaluate(`window.location.hash = '#usage'`);
                listed = await until(deviceRow, 3000);
            }
            if (!listed) {
                return 'the device that played is not listed';
            }
            await click('.usage-table.devices .open-device');
            if (!(await until(`document.querySelector('form.rename-device [name=name]')`))) {
                return 'the device did not open';
            }
            await evaluate(`document.querySelector('form.rename-device').elements.name.value = 'Car'`);
            const said = await saveBy('form.rename-device button[type=submit]');
            if (said !== 'Name saved.') {
                return said;
            }
            if (!(await until(`${deviceRow} && ${deviceRow}.textContent === 'Car'`))) {
                return 'the list does not show the new name';
            }
            return (await until(`[...document.querySelectorAll('.usage-now li')].some((n) => n.textContent.startsWith('Car') && n.textContent.includes('First Station'))`))
                ? null : `the live row says ${await evaluate(`document.querySelector('.usage-now').textContent`)}`;
        });
        // The device's sessions are on screen too.
        await until(`document.querySelector('.device .usage-table')`);
        await noSideways('usage device');

        await step('delete the device', async () => {
            await buttonNamed('Delete this device');
            if (!(await until(`document.querySelector('.confirm-delete')`))) {
                return 'no question before deleting';
            }
            const said = await saveBy('.confirm-delete');
            if (said !== 'Device deleted.') {
                return said;
            }
            return (await until(`!${deviceRow} && !document.querySelector('form.rename-device')`))
                ? null : 'the device is still listed';
        });

        const usageSettings = () => evaluate(`fetch('/admin/api/usage/settings').then((r) => r.json())`);
        const usageForm = `document.querySelector('form.usage [name=retention_days]')`;
        await step('usage settings save, and storing addresses asks for a code', async () => {
            await evaluate(`window.location.hash = '#settings'`);
            if (!(await until(usageForm))) {
                return 'no usage settings';
            }
            await evaluate(`document.querySelector('form.usage').retention_days.value = '400'`);
            let said = await saveBy('form.usage button[type=submit]');
            if (said !== 'Usage settings saved.' || (await usageSettings()).retention_days !== 400) {
                return `retention: ${said}`;
            }
            // The form is drawn again from what is stored.
            if (!(await until(`${usageForm} && ${usageForm}.value === '400'`))) {
                return 'the form does not show the stored retention';
            }
            await evaluate(`document.querySelector('form.usage').store_addresses.checked = true`);
            await evaluate(`document.querySelector('.message').textContent = ''`);
            await click('form.usage button[type=submit]');
            if (!(await until(`document.querySelector('form.usage form.confirm-code')`))) {
                return `no code asked (message: ${await message()})`;
            }
            await typeCode();
            if (!(await until(`document.querySelector('.message').textContent !== ''`))) {
                return 'no answer after the code';
            }
            said = await message();
            if (said !== 'Usage settings saved.' || !(await usageSettings()).store_addresses) {
                return `addresses on: ${said}`;
            }
            if (!(await until(`${usageForm} && document.querySelector('form.usage').store_addresses.checked`))) {
                return 'the form does not show addresses on';
            }
            await evaluate(`document.querySelector('form.usage').store_addresses.checked = false`);
            said = await saveBy('form.usage button[type=submit]');
            const stored = await usageSettings();
            if (await evaluate(`Boolean(document.querySelector('form.confirm-code'))`)) {
                return 'switching addresses off asked for a code';
            }
            return said === 'Usage settings saved.' && stored.store_addresses === false ? null : `addresses off: ${said}`;
        });
        await noSideways('settings with usage');

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
            return saveCoded('form.pin button[type=submit]');
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
            const changed = await pinCall({mode: 'site', pin: '7654321'});
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
            const changed = await pinCall({mode: 'stations', pin: '24682468'});
            if (changed !== 200) {
                return `the PIN change answered ${changed}`;
            }
            // Its next poll finds it gone; the keypad offers it back.
            if (!(await until(pinShown, 15000))) {
                return 'no keypad after the PIN changed';
            }
            // The station list fetched right after the PIN fails once (a
            // network blip in the car); the player must try again.
            let failed = 0;
            on('Fetch.requestPaused', async (paused) => {
                if (failed === 0 && paused.request.url.endsWith('/api/channels')) {
                    failed += 1;
                    await send('Fetch.failRequest', {requestId: paused.requestId, errorReason: 'ConnectionReset'});
                } else {
                    await send('Fetch.continueRequest', {requestId: paused.requestId});
                }
            });
            await send('Fetch.enable', {patterns: [{urlPattern: '*/api/channels*'}]});
            await typePin('24682468');
            if (!(await until(`!(${pinShown})`))) {
                return 'the new PIN did not close the keypad';
            }
            const polls = `performance.getEntriesByType('resource').filter((e) => e.name.includes('/api/channels/second/now')).length`;
            const before = await evaluate(polls);
            // Titles resume: the station is polled again within two intervals.
            const resumed = await until(`${polls} > ${before}`, 25000);
            await send('Fetch.disable');
            on('Fetch.requestPaused', null);
            if (!failed) {
                return 'the station list was never fetched after the PIN';
            }
            return resumed ? null : 'the hidden station is no longer polled after the PIN';
        });

        await step('switch the PIN off in the panel', async () => {
            const said = await savePin('off');
            const status = await evaluate(`fetch('/api/channels').then((r) => r.status)`);
            await evaluate(`window.location.hash = '#stations'`);
            return said === 'PIN saved. The player is open to everyone.' && status === 200 ? null : `${said} / ${status}`;
        });

        // No step for a busy PIN answer: a flood needs 100 failures from
        // addresses other than this browser's own. The server and keypad
        // tests cover it.
        await step('a PIN switched off shows the hidden stations', async () => {
            const chosen = await pinCall({mode: 'stations', pin: '13572468'});
            if (chosen !== 200) {
                return `choosing stations answered ${chosen}`;
            }
            // A device without the grant lists one station.
            await send('Network.enable');
            await send('Network.deleteCookies', {name: 'tunebox_grant', url: `${base}/`});
            await send('Page.navigate', {url: `${base}/`});
            const listed = () => evaluate(`document.querySelectorAll('.channel-groups .channel').length`);
            if (!(await until(`document.querySelectorAll('.channel-groups .channel').length === 1`))) {
                return `listed ${await listed()} stations before the change, expected 1`;
            }
            const off = await pinCall({mode: 'off'});
            if (off !== 200) {
                return `switching off answered ${off}`;
            }
            // The page is not reloaded: its next poll sees the new revision.
            return (await until(`document.querySelectorAll('.channel-groups .channel').length === 2`, 12000))
                ? null : `still ${await listed()} stations 12 s after the PIN went off`;
        });

        await step('the keypad leaves Stop reachable', async () => {
            // The page keeps its audio elements where the check can reach them.
            const hook = await send('Page.addScriptToEvaluateOnNewDocument', {source: `
                window.__audios = [];
                const Real = window.Audio;
                window.Audio = function () {
                    const made = new Real();
                    window.__audios.push(made);
                    return made;
                };`});
            try {
                await send('Page.navigate', {url: `${base}/`});
                if (!(await until(`document.querySelector('.play') && document.querySelector('.current-channel').textContent`))) {
                    return 'player not shown';
                }
                // A real mouse press: playing needs a user gesture.
                const box = await evaluate(`(() => {
                    const r = document.querySelector('.play').getBoundingClientRect();
                    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                })()`);
                for (const type of ['mousePressed', 'mouseReleased']) {
                    await send('Input.dispatchMouseEvent', {type, x: box.x, y: box.y, button: 'left', clickCount: 1});
                }
                const playing = `window.__audios.some((a) => !a.paused && a.currentTime > 0)`;
                if (!(await until(playing, 10000))) {
                    return 'the local stream did not start playing';
                }
                const locked = await pinCall({mode: 'site', pin: '97531246'});
                if (locked !== 200) {
                    return `locking the site answered ${locked}`;
                }
                const stopShown = `${pinShown} && document.querySelector('.pin-stop') && !document.querySelector('.pin-stop').hidden`;
                if (!(await until(stopShown, 15000))) {
                    return 'the keypad shows no Stop while the audio plays';
                }
                // Tab from the last control wraps to the first key.
                await evaluate(`document.querySelector('.pin-stop').focus()`);
                for (const type of ['keyDown', 'keyUp']) {
                    await send('Input.dispatchKeyEvent', {type, key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9});
                }
                const landed = await evaluate(`document.activeElement.dataset.key`);
                if (landed !== '1') {
                    return `Tab from Stop landed on key "${landed}", expected the first key`;
                }
                await click('.pin-stop');
                return (await until(`window.__audios.every((a) => a.paused)`)) ? null : 'Stop did not stop the audio';
            } finally {
                // Whatever happened, later steps start from an open player on the panel.
                await send('Page.removeScriptToEvaluateOnNewDocument', {identifier: hook.result.identifier});
                const off = await pinCall({mode: 'off'});
                await send('Page.navigate', {url: `${base}/admin/#stations`});
                if (off !== 200) {
                    throw new Error(`switching off answered ${off}`);
                }
            }
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
            const problem = await newLogin(PASSWORD);
            if (problem) {
                return problem;
            }
            await evaluate(`window.location.hash = '#account'`);
            if (!(await until(`document.querySelectorAll('table.sessions tbody tr').length`))) {
                return 'account page not shown';
            }
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

        // A second browser: its own session, driven through the API, that
        // ends the page's session the way "Sign out" in the list would.
        const NEW_PASSWORD = 'a new browser check password';
        let other = null;
        const otherCall = async (method, path, body) => {
            const headers = {'Content-Type': 'application/json', Origin: base, Cookie: other.cookie};
            if (other.csrf) {
                headers['X-Tunebox-CSRF'] = other.csrf;
            }
            const response = await fetch(`${base}/admin/api/${path}`, {
                method, headers, body: body === undefined ? undefined : JSON.stringify(body),
            });
            return {status: response.status, data: response.status === 204 ? {} : await response.json()};
        };
        const otherLogin = async () => {
            await sleep(1100);
            const response = await fetch(`${base}/admin/api/login`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json', Origin: base},
                body: JSON.stringify({password: NEW_PASSWORD, code: await freshCode()}),
            });
            if (response.status !== 200 && response.status !== 204) {
                throw new Error(`the second login answered ${response.status}`);
            }
            other = {cookie: response.headers.getSetCookie().map((c) => c.split(';')[0]).join('; '), csrf: null};
            other.csrf = (await otherCall('GET', 'state')).data.session.csrf;
        };
        const endPageSession = async () => {
            const {data} = await otherCall('GET', 'account');
            for (const session of data.sessions.filter((item) => !item.current)) {
                await otherCall('POST', `account/sessions/${session.id}/end`);
            }
        };
        const pageLogin = async (password = NEW_PASSWORD) => {
            await sleep(1100);
            // The view keeps the page of the ended session until the next
            // one is drawn.
            await evaluate(`[...document.querySelectorAll('#shell .view > *')].forEach((node) => { node.dataset.old = '1'; })`);
            const problem = await toCode(password);
            if (problem) {
                return problem;
            }
            const code = await freshCode();
            await evaluate(`(() => {
                const form = document.querySelector('#code');
                form.code.value = ${JSON.stringify(code)};
                form.requestSubmit();
            })()`);
            return (await until(`!document.querySelector('#shell').hidden && [...document.querySelectorAll('#shell .view > *')].some((node) => !node.dataset.old)`))
                ? null : `not logged in (message: ${await message()})`;
        };

        await step('history after the session ended shows the login', async () => {
            await otherLogin();
            await endPageSession();
            await evaluate(`window.location.hash = '#history'`);
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return `the login form did not show (message: ${await message()})`;
            }
            const said = await message();
            if (said.indexOf('session ended') < 0) {
                return `message: ${said}`;
            }
            return pageLogin();
        });

        await step('an ended session mid-change shows the login', async () => {
            await evaluate(`window.location.hash = '#account'`);
            if (!(await until(`document.querySelector('[name="new-password"]')`))) {
                return 'account page not shown';
            }
            await endPageSession();
            await evaluate(`(() => {
                const inputs = document.querySelectorAll('[name="new-password"], [name="new-password-again"]');
                for (const input of inputs) {
                    input.value = 'yet another browser check password';
                }
                inputs[0].form.requestSubmit();
            })()`);
            if (!(await until(`!document.querySelector('#login').hidden || document.querySelector('form.confirm-code')`))) {
                return `neither the login nor a code form showed (message: ${await message()})`;
            }
            if (await evaluate(`Boolean(document.querySelector('form.confirm-code'))`)) {
                const code = await freshCode();
                await evaluate(`(() => {
                    const form = document.querySelector('form.confirm-code');
                    form.code.value = ${JSON.stringify(code)};
                    form.requestSubmit();
                })()`);
                if (!(await until(`!document.querySelector('#login').hidden`))) {
                    return 'the login form did not show after the code';
                }
            }
            // The password did not change: the old one still logs in.
            return pageLogin(NEW_PASSWORD);
        });

        await step('an edit whose session ended is offered again', async () => {
            await evaluate(`window.location.hash = '#edit/first'`);
            if (!(await until(`document.querySelector('form.edit')`))) {
                return 'no form';
            }
            await evaluate(`document.querySelector('form.edit').elements.name.value = 'Kept Across Login'`);
            await endPageSession();
            await evaluate(`document.querySelector('form.edit').requestSubmit()`);
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return `the login form did not show (message: ${await message()})`;
            }
            const problem = await pageLogin();
            if (problem) {
                return problem;
            }
            if (!(await until(`document.querySelector('#shell .pending') && !document.querySelector('#shell .pending').hidden`))) {
                return 'no box offering the change';
            }
            const text = await evaluate(`document.querySelector('#shell .pending').textContent`);
            if (text.indexOf('A change was not saved because your session ended.') < 0) {
                return `box says: ${text}`;
            }
            await buttonNamed('Save it now');
            const deadline = Date.now() + 3000;
            while (Date.now() < deadline) {
                if ((await channels(base)).some((c) => c.id === 'first' && c.name === 'Kept Across Login')) {
                    // The page re-reads the config before it moves on; a form
                    // touched before then makes it ask instead of leaving.
                    return (await until(`location.hash === '#stations'`)) ? null : 'the page stayed after saving';
                }
                await sleep(100);
            }
            return `name not stored (message: ${await message()})`;
        });

        await step('a kept edit that went stale is not saved', async () => {
            await evaluate(`window.location.hash = '#edit/first'`);
            if (!(await until(`document.querySelector('form.edit')`))) {
                return 'no form';
            }
            await evaluate(`document.querySelector('form.edit').elements.name.value = 'Lost Update'`);
            await endPageSession();
            await evaluate(`document.querySelector('form.edit').requestSubmit()`);
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return `the login form did not show (message: ${await message()})`;
            }
            const problem = await pageLogin();
            if (problem) {
                return problem;
            }
            if (!(await until(`document.querySelector('#shell .pending') && !document.querySelector('#shell .pending').hidden`))) {
                return 'no box offering the change';
            }
            // Another browser saves meanwhile, so the kept change is stale.
            const {data} = await otherCall('GET', 'config');
            const changed = JSON.parse(JSON.stringify(data.state));
            changed.channels.find((row) => row.id === 'first').doc.name = 'Changed Elsewhere';
            const saved = await otherCall('POST', 'config', {based_on: data.version, state: changed});
            if (saved.status !== 200) {
                return `the other browser's save answered ${saved.status}`;
            }
            await buttonNamed('Save it now');
            if (!(await until(`document.querySelector('.message').textContent.indexOf('kept change was not saved') >= 0`))) {
                return `message: ${await message()}`;
            }
            if (!(await until(`document.querySelector('#shell .pending').hidden`))) {
                return 'the box still offers the stale change';
            }
            const stored = (await channels(base)).find((c) => c.id === 'first');
            return stored && stored.name === 'Changed Elsewhere' ? null : `stored name: ${stored && stored.name}`;
        });

        // A station hidden behind the PIN, unticked on a session with no
        // code from the last 5 minutes: a new login is not one.
        const pageConfig = () => evaluate(`fetch('/admin/api/config').then((r) => r.json())`);
        const hidden = async () => {
            const {state} = await pageConfig();
            const row = state.channels.find((item) => item.id === 'second');
            return Boolean(row && row.doc.needs_pin === true);
        };
        const untickSecond = async () => {
            if ((await evaluate(`location.hash`)) !== '#edit/second') {
                await evaluate(`window.location.hash = '#edit/second'`);
            }
            if (!(await until(`document.querySelector('form.edit [name=needs_pin]')`))) {
                throw new Error('no "Needs the PIN" box');
            }
            await evaluate(`(() => {
                const form = document.querySelector('form.edit');
                form.elements.needs_pin.checked = false;
                form.requestSubmit();
            })()`);
            if (!(await until(`document.querySelector('.shell-code form.confirm-code')`))) {
                throw new Error(`no code asked in the shell (message: ${await message()})`);
            }
        };

        await step('cancelling the code leaves the station hidden', async () => {
            const chosen = await pinCall({mode: 'stations'});
            if (chosen !== 200) {
                return `choosing stations answered ${chosen}`;
            }
            if (!(await hidden())) {
                return 'the second station is not marked "Needs the PIN" before the step';
            }
            const problem = await newLogin(NEW_PASSWORD);
            if (problem) {
                return problem;
            }
            await untickSecond();
            await click('.shell-code form.confirm-code .quiet');
            if (!(await until(`document.querySelector('.message').textContent.indexOf('Not saved.') === 0`))) {
                return `message: ${await message()}`;
            }
            if (!(await hidden())) {
                return 'stored without needs_pin after Cancel';
            }
            if ((await channels(base)).some((c) => c.id === 'second')) {
                return 'the player lists the hidden station after Cancel';
            }
            const left = await evaluate(`document.querySelector('.shell-code').children.length`);
            return left ? 'the code form stayed after Cancel' : null;
        });

        await step('logging out at the code prompt with unsaved edits asks first, where it can be answered', async () => {
            await untickSecond();
            await click('.logout');
            const leave = `[...document.querySelectorAll('#shell .view button')].find((b) => b.textContent.trim() === 'Leave without saving')`;
            if (!(await until(leave))) {
                return `no question about the unsaved edits (message: ${await message()})`;
            }
            if (await evaluate(`Boolean(document.querySelector('.shell-code form.confirm-code'))`)) {
                return 'the code prompt stayed open beside the question';
            }
            if (await evaluate(`Boolean(${leave}.closest('[inert]'))`)) {
                return 'the question is inside the inert view';
            }
            // A real mouse press: an inert view takes none.
            const box = await evaluate(`(() => {
                const r = ${leave}.getBoundingClientRect();
                return {x: r.left + r.width / 2, y: r.top + r.height / 2, seen: r.bottom > 0 && r.top < innerHeight};
            })()`);
            if (!box.seen) {
                return 'the question is out of sight';
            }
            for (const type of ['mousePressed', 'mouseReleased']) {
                await send('Input.dispatchMouseEvent', {type, x: box.x, y: box.y, button: 'left', clickCount: 1});
            }
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return 'the answer did not log out';
            }
            const again = await pageLogin();
            if (again) {
                return again;
            }
            return (await hidden()) ? null : 'stored without needs_pin';
        });

        await step('unticking Needs the PIN asks for a code', async () => {
            await untickSecond();
            await typeCode();
            const mark = `document.querySelector('.station[data-id="second"] .id')`;
            if (!(await until(`location.hash === '#stations' && ${mark} && !${mark}.textContent.includes('PIN')`))) {
                return `not saved (message: ${await message()})`;
            }
            if (await hidden()) {
                return 'still stored with needs_pin';
            }
            if (!(await channels(base)).some((c) => c.id === 'second')) {
                return 'the player does not list the station';
            }
            // Ticking it again needs no code; later steps start hidden and open.
            await evaluate(`window.location.hash = '#edit/second'`);
            if (!(await until(`document.querySelector('form.edit [name=needs_pin]')`))) {
                return 'no "Needs the PIN" box';
            }
            await evaluate(`(() => {
                const form = document.querySelector('form.edit');
                form.elements.needs_pin.checked = true;
                form.requestSubmit();
            })()`);
            if (!(await until(`location.hash === '#stations' && ${mark} && ${mark}.textContent.includes('PIN')`))) {
                return `not hidden again (message: ${await message()})`;
            }
            const off = await pinCall({mode: 'off'});
            return off === 200 ? null : `switching off answered ${off}`;
        });

        // Unticks Needs the PIN on the second station in a session that ends
        // before the save, logs in again and presses Save it now: a new
        // login holds no fresh code, so the shell asks for one.
        const keptUntick = async () => {
            await evaluate(`window.location.hash = '#edit/second'`);
            if (!(await until(`document.querySelector('form.edit [name=needs_pin]')`))) {
                return 'no "Needs the PIN" box';
            }
            await evaluate(`document.querySelector('form.edit').elements.needs_pin.checked = false`);
            await endPageSession();
            await evaluate(`document.querySelector('form.edit').requestSubmit()`);
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return `the login form did not show (message: ${await message()})`;
            }
            const problem = await pageLogin();
            if (problem) {
                return problem;
            }
            if (!(await until(`document.querySelector('#shell .pending') && !document.querySelector('#shell .pending').hidden`))) {
                return 'no box offering the change';
            }
            await buttonNamed('Save it now');
            if (!(await until(`document.querySelector('.shell-code form.confirm-code')`))) {
                return `no code asked in the shell (message: ${await message()})`;
            }
            return (await hidden()) ? null : 'stored without needs_pin before the code';
        };

        await step('a kept edit that unhides a station asks for a code after login', async () => {
            const chosen = await pinCall({mode: 'stations'});
            if (chosen !== 200) {
                return `choosing stations answered ${chosen}`;
            }
            if (!(await hidden())) {
                return 'the second station is not marked "Needs the PIN" before the step';
            }
            const problem = await keptUntick();
            if (problem) {
                return problem;
            }
            await typeCode();
            if (!(await until(`location.hash === '#stations' && document.querySelector('#shell .pending').hidden`))) {
                return `not saved (message: ${await message()})`;
            }
            if (await hidden()) {
                return 'still stored with needs_pin';
            }
            // Hidden again for the next step; ticking the box needs no code.
            await evaluate(`window.location.hash = '#edit/second'`);
            if (!(await until(`document.querySelector('form.edit [name=needs_pin]')`))) {
                return 'no "Needs the PIN" box';
            }
            await evaluate(`(() => {
                const form = document.querySelector('form.edit');
                form.elements.needs_pin.checked = true;
                form.requestSubmit();
            })()`);
            const mark = `document.querySelector('.station[data-id="second"] .id')`;
            return (await until(`location.hash === '#stations' && ${mark} && ${mark}.textContent.includes('PIN')`))
                ? null : `not hidden again (message: ${await message()})`;
        });

        await step('a code prompt that cannot reach tunebox stays open, and logging out there says nothing of saving', async () => {
            const problem = await keptUntick();
            if (problem) {
                return problem;
            }
            const offline = (on) => send('Network.emulateNetworkConditions', {offline: on, latency: 0, downloadThroughput: -1, uploadThroughput: -1});
            await offline(true);
            let said = false;
            try {
                await typeCode();
                said = await until(`document.querySelector('.message').textContent === 'Could not reach tunebox.'`);
            } finally {
                await offline(false);
            }
            if (!said) {
                return `message: ${await message()}`;
            }
            if (!(await evaluate(`Boolean(document.querySelector('.shell-code form.confirm-code'))`))) {
                return 'the code form closed when tunebox could not be reached';
            }
            await click('.logout');
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return 'log out did not show the login';
            }
            await sleep(300);
            const after = await message();
            if (after.indexOf('Not saved') >= 0) {
                return `the login says: ${after}`;
            }
            const again = await pageLogin();
            if (again) {
                return again;
            }
            if (!(await hidden())) {
                return 'stored without needs_pin';
            }
            const off = await pinCall({mode: 'off'});
            return off === 200 ? null : `switching off answered ${off}`;
        });

        await step('a stale PIN form is refused', async () => {
            await send('Page.navigate', {url: `${base}/admin/#settings`});
            if (!(await until(`document.querySelector('form.pin [name=pin_mode]')`))) {
                return 'PIN form not shown';
            }
            // Another page sets the mode meanwhile.
            const status = await pinCall({mode: 'stations'});
            if (status !== 200) {
                return `the other page's change answered ${status}`;
            }
            await evaluate(`document.querySelector('form.pin').pin_mode.value = 'off'`);
            const said = await saveCoded('form.pin button[type=submit]');
            // The form is redrawn from a fresh read after the message.
            const shown = await until(`document.querySelector('form.pin').pin_mode.value === 'stations'`);
            if (said.indexOf('changed elsewhere') < 0 || !shown) {
                return `${said} / form shows ${await evaluate(`document.querySelector('form.pin').pin_mode.value`)}`;
            }
            const back = await savePin('off');
            return back === 'PIN saved. The player is open to everyone.' ? null : back;
        });

        await step('a PIN form with typed digits asks before leaving', async () => {
            await evaluate(`document.querySelector('form.pin').pin.value = '1234'`);
            await click('#shell nav a[href="#stations"]');
            if (!(await until(`document.querySelector('.confirm') && document.querySelector('.confirm').textContent.indexOf('not saved') >= 0`))) {
                return 'no question';
            }
            if ((await evaluate(`location.hash`)) !== '#settings') {
                return 'the page left without an answer';
            }
            await buttonNamed('Leave without saving');
            return (await until(`location.hash === '#stations'`)) ? null : 'did not leave after the answer';
        });

        // Export, import and logo upload run on the History and Edit pages
        // with files chosen through the page's own hidden file inputs.
        const transfer = ownedFolder('tunebox-transfer-');
        const setFile = async (selector, path) => {
            const document = await send('DOM.getDocument', {depth: 0});
            const found = await send('DOM.querySelector', {nodeId: document.result.root.nodeId, selector});
            if (!found.result || !found.result.nodeId) {
                throw new Error(`no ${selector}`);
            }
            await send('DOM.setFileInputFiles', {files: [path], nodeId: found.result.nodeId});
        };
        const openPage = async (hash, ready) => {
            await evaluate(`window.location.hash = ${JSON.stringify(hash)}`);
            return until(ready);
        };
        const exportText = () => evaluate(`fetch('/admin/api/export').then((r) => r.text())`);
        // Chooses a file for the import, waits for its preview.
        const previewImport = async (text, name) => {
            const path = join(transfer, name);
            writeFileSync(path, text);
            if (!(await openPage('#history', `document.querySelector('input[name=import-file]')`))) {
                throw new Error('no import button on the History page');
            }
            await setFile('input[name=import-file]', path);
            if (!(await until(`document.querySelector('.restore .actions')`))) {
                throw new Error(`no preview (message: ${await message()})`);
            }
        };
        // Clicks Replace, gives the fresh code when asked, returns what the
        // page says once it has answered.
        const replaceWithFile = async () => {
            await evaluate(`document.querySelector('.message').textContent = ''`);
            await buttonNamed('Replace the stations with this file');
            if (!(await until(`document.querySelector('form.confirm-code') || document.querySelector('.message').textContent !== ''`))) {
                throw new Error('no answer to Replace');
            }
            if (await evaluate(`Boolean(document.querySelector('form.confirm-code'))`)) {
                await typeCode();
                if (!(await until(`document.querySelector('.message').textContent !== ''`))) {
                    throw new Error('no answer after the code');
                }
            }
            return message();
        };

        await step('export then import changes nothing', async () => {
            const text = await exportText();
            if (text.indexOf('[[channels]]') < 0) {
                return `the export holds no stations: ${text.slice(0, 80)}`;
            }
            await previewImport(text, 'export.toml');
            const summary = await evaluate(`document.querySelector('.restore').textContent`);
            if (summary.indexOf('The file matches what is stored now.') < 0) {
                return `preview says: ${summary.slice(0, 120)}`;
            }
            const said = await replaceWithFile();
            return said === 'No changes to save.' ? null : `message: ${said}`;
        });

        await step('the Export button saves the stored stations under the server\'s filename', async () => {
            if (!(await openPage('#history', `document.querySelector('.transfer')`))) {
                return 'no History page';
            }
            // The page saves through a temporary link to a blob; the link's
            // click is caught here and the blob kept, since the page's
            // connect-src does not allow fetching a blob URL.
            await evaluate(`(() => {
                window.__saved = null;
                window.__click = HTMLAnchorElement.prototype.click;
                window.__make = URL.createObjectURL;
                let blob = null;
                URL.createObjectURL = function (made) { blob = made; return window.__make.call(URL, made); };
                HTMLAnchorElement.prototype.click = function () {
                    const saved = {name: this.download, text: null};
                    window.__saved = saved;
                    blob.text().then((t) => { saved.text = t; });
                };
            })()`);
            await buttonNamed('Export');
            const got = await until(`window.__saved && window.__saved.text !== null`);
            await evaluate(`HTMLAnchorElement.prototype.click = window.__click; URL.createObjectURL = window.__make`);
            if (!got) {
                return `nothing was saved (message: ${await message()})`;
            }
            const saved = await evaluate(`window.__saved`);
            if (!/^tunebox-\d{4}-\d\d-\d\d\.toml$/.test(saved.name)) {
                return `the file is named ${saved.name}`;
            }
            return saved.text === (await exportText()) ? null : 'the saved file differs from the export';
        });

        await step('an import shows its differences', async () => {
            const before = (await channels(base)).find((c) => c.id === 'first').name;
            const text = await exportText();
            const edited = text.replace(/(\[\[channels\]\][\s\S]*?\nname = ")([^"]*)"/, '$1Imported Station"');
            if (edited === text) {
                return 'no station name found to edit in the export';
            }
            await previewImport(edited, 'renamed.toml');
            const table = await evaluate(`document.querySelector('.restore .diff') ? document.querySelector('.restore .diff').textContent : ''`);
            if (table.indexOf('Imported Station') < 0 || table.indexOf(before) < 0) {
                return `the preview does not list the rename: ${table.slice(0, 200)}`;
            }
            const said = await replaceWithFile();
            if (said !== 'Imported.') {
                return `message: ${said}`;
            }
            for (let i = 0; i < 30; i++) {
                if ((await channels(base)).some((c) => c.id === 'first' && c.name === 'Imported Station')) {
                    break;
                }
                await sleep(100);
            }
            if (!(await openPage('#stations', `document.querySelector('.station[data-id="first"]')`))) {
                return 'stations not listed';
            }
            const listed = await evaluate(`document.querySelector('.station[data-id="first"]').textContent`);
            return listed.indexOf('Imported Station') >= 0 ? null : `the list shows: ${listed}`;
        });

        // A small solid PNG, made here so the check needs no fixture file.
        const crcTable = Array.from({length: 256}, (_, n) => {
            let c = n;
            for (let k = 0; k < 8; k++) {
                c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
            }
            return c >>> 0;
        });
        const crc = (bytes) => {
            let c = 0xffffffff;
            for (const byte of bytes) {
                c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8);
            }
            return (c ^ 0xffffffff) >>> 0;
        };
        const chunk = (type, data) => {
            const body = Buffer.concat([Buffer.from(type), data]);
            const head = Buffer.alloc(4);
            head.writeUInt32BE(data.length);
            const tail = Buffer.alloc(4);
            tail.writeUInt32BE(crc(body));
            return Buffer.concat([head, body, tail]);
        };
        const smallPng = () => {
            const size = 8;
            const header = Buffer.alloc(13);
            header.writeUInt32BE(size, 0);
            header.writeUInt32BE(size, 4);
            header.set([8, 2, 0, 0, 0], 8);
            const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(size * 3, 0xc8)]);
            return Buffer.concat([
                Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
                chunk('IHDR', header),
                chunk('IDAT', deflateSync(Buffer.concat(Array(size).fill(row)))),
                chunk('IEND', Buffer.alloc(0)),
            ]);
        };
        const logoField = () => evaluate(`document.querySelector('form.edit').elements.logo.value`);

        await step('an uploaded logo shows in the player', async () => {
            writeFileSync(join(transfer, 'logo.png'), smallPng());
            if (!(await openPage('#edit/first', `document.querySelector('form.edit') && document.querySelector('input[name=logo-file]')`))) {
                return 'no edit form';
            }
            await setFile('input[name=logo-file]', join(transfer, 'logo.png'));
            if (!(await until(`document.querySelector('form.edit').elements.logo.value.startsWith('blob:')`))) {
                return `the Logo field holds ${await logoField()} (message: ${await message()})`;
            }
            if (!(await until(`(() => {
                const img = document.querySelector('.logo-preview img.logo-full');
                return img && img.naturalWidth > 0 && document.querySelector('.logo-preview img.logo-small');
            })()`))) {
                return 'the preview shows no image';
            }
            const reference = await logoField();
            await evaluate(`document.querySelector('form.edit').requestSubmit()`);
            if (!(await until(`location.hash === '#stations' && document.querySelector('.station')`))) {
                return `the page stayed on the form (message: ${await message()})`;
            }
            // A background worker copies the logo; the player reads its path.
            let path = null;
            for (let i = 0; i < 80 && !path; i++) {
                const found = (await channels(base)).find((c) => c.id === 'first');
                path = found && found.logo;
                if (!path) {
                    await sleep(100);
                }
            }
            if (!path) {
                return `the player's list gives no logo for ${reference}`;
            }
            const served = await evaluate(`fetch(${JSON.stringify(path)}).then((r) => r.status + ' ' + r.headers.get('content-type'))`);
            if (served !== '200 image/png') {
                return `${path} answers ${served}`;
            }
            const drawn = await evaluate(`new Promise((done) => {
                const img = new Image();
                img.onload = () => done(img.naturalWidth);
                img.onerror = () => done(0);
                img.src = ${JSON.stringify(path)};
            })`);
            return drawn > 0 ? null : `${path} does not decode as an image`;
        });

        await step('a file that is not an image is refused', async () => {
            writeFileSync(join(transfer, 'page.png'), '<!doctype html><title>not an image</title><script>1</script>');
            if (!(await openPage('#edit/first', `document.querySelector('form.edit') && document.querySelector('input[name=logo-file]')`))) {
                return 'no edit form';
            }
            const before = await logoField();
            await setFile('input[name=logo-file]', join(transfer, 'page.png'));
            const wanted = 'That file is not a PNG, JPEG, WebP or SVG image.';
            // An upload refusal is not a form error: it is said in the
            // page's own message line.
            if (!(await until(`document.querySelector('.message').textContent === ${JSON.stringify(wanted)}`))) {
                return `message: ${await message()}`;
            }
            const after = await logoField();
            if (after !== before) {
                return `the Logo field changed from ${before} to ${after}`;
            }
            await evaluate(`[...document.querySelectorAll('form.edit .actions button')].find((b) => b.textContent.trim() === 'Cancel').click()`);
            return (await until(`location.hash === '#stations' && document.querySelector('.station')`)) ? null : 'could not leave the form';
        });
        await step('an upload that meets an ended session asks for the image again after login', async () => {
            if (!(await openPage('#edit/first', `document.querySelector('form.edit') && document.querySelector('input[name=logo-file]')`))) {
                return 'no edit form';
            }
            await otherLogin();
            await endPageSession();
            await setFile('input[name=logo-file]', join(transfer, 'logo.png'));
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return `the login form did not show (message: ${await message()})`;
            }
            const problem = await pageLogin();
            if (problem) {
                return problem;
            }
            if (!(await until(`document.querySelector('#shell .pending') && !document.querySelector('#shell .pending').hidden`))) {
                return 'no box after login';
            }
            const text = await evaluate(`document.querySelector('#shell .pending').textContent`);
            if (text.indexOf('The image was not uploaded') < 0) {
                return `box says: ${text}`;
            }
            await buttonNamed('Discard');
            return (await until(`document.querySelector('#shell .pending').hidden`)) ? null : 'the box stayed after Discard';
        });

        await step('a group gets an image', async () => {
            // A group's image is downloaded only while the group has a
            // station on, so the first station moves into it.
            if (!(await openPage('#stations', `document.querySelector('form.add-group')`))) {
                return 'no Add group form';
            }
            await evaluate(`document.querySelector('form.add-group input').value = 'Pictured'`);
            const added = await saveBy('form.add-group button[type=submit]');
            if (added.indexOf('Saved') < 0) {
                return `adding the group says: ${added}`;
            }
            if (!(await openPage('#edit/first', `document.querySelector('form.edit')`))) {
                return 'no edit form';
            }
            await evaluate(`document.querySelector('form.edit').elements.group.value = 'Pictured'`);
            await evaluate(`document.querySelector('form.edit').requestSubmit()`);
            if (!(await until(`location.hash === '#stations' && document.querySelector('button[title="Edit group Pictured"]')`))) {
                return `the station did not move (message: ${await message()})`;
            }
            await click('button[title="Edit group Pictured"]');
            if (!(await until(`document.querySelector('form.group-edit input[name=logo-file]')`))) {
                return 'the pencil opened no group form';
            }
            await noSideways('group form');
            await setFile('form.group-edit input[name=logo-file]', join(transfer, 'logo.png'));
            if (!(await until(`document.querySelector('form.group-edit').elements.image.value.startsWith('blob:')`))) {
                return `the Image field holds ${await evaluate(`document.querySelector('form.group-edit').elements.image.value`)} (message: ${await message()})`;
            }
            if (!(await until(`(() => { const img = document.querySelector('form.group-edit .logo-preview img'); return img && img.naturalWidth > 0; })()`))) {
                return 'the form shows no preview of the upload';
            }
            const saved = await saveBy('form.group-edit button[type=submit]');
            if (saved !== 'Saved group Pictured.') {
                return `saving the group says: ${saved}`;
            }
            // A background worker copies the image; the panel lists it once
            // the file is there.
            let listed = false;
            for (let i = 0; i < 50 && !listed; i++) {
                listed = await evaluate(`fetch('/admin/api/config').then((r) => r.json()).then((c) => Boolean(c.group_images.Pictured))`);
                if (!listed) {
                    await sleep(100);
                }
            }
            if (!listed) {
                return 'the panel\'s config lists no image for Pictured';
            }
            await evaluate(`document.body.dataset.beforeReload = '1'`);
            await send('Page.reload', {});
            const shown = `(() => {
                const img = !document.body.dataset.beforeReload && document.querySelector('.group-head img.group-image');
                return Boolean(img && img.naturalWidth > 0 && img.alt === '');
            })()`;
            return (await until(shown)) ? null : 'the Stations heading shows no image for Pictured';
        });

        await step('a group form that meets an ended session offers its change after login', async () => {
            if (!(await openPage('#stations', `document.querySelector('button[title="Edit group Pictured"]')`))) {
                return 'no group Pictured';
            }
            await click('button[title="Edit group Pictured"]');
            if (!(await until(`document.querySelector('form.group-edit input[name=logo-file]')`))) {
                return 'the pencil opened no group form';
            }
            const image = await evaluate(`document.querySelector('form.group-edit').elements.image.value`);
            await evaluate(`document.querySelector('form.group-edit').elements.name.value = 'Pictured Two'`);
            await otherLogin();
            await endPageSession();
            await setFile('form.group-edit input[name=logo-file]', join(transfer, 'logo.png'));
            if (!(await until(`!document.querySelector('#login').hidden`))) {
                return `the login form did not show (message: ${await message()})`;
            }
            const problem = await pageLogin();
            if (problem) {
                return problem;
            }
            if (!(await until(`document.querySelector('#shell .pending') && !document.querySelector('#shell .pending').hidden`))) {
                return 'no box after login';
            }
            const text = await evaluate(`document.querySelector('#shell .pending').textContent`);
            if (text.indexOf('The image was not uploaded') < 0) {
                return `box says: ${text}`;
            }
            await buttonNamed('Save it now');
            if (!(await until(`document.querySelector('#shell .pending').hidden && document.querySelector('button[title="Edit group Pictured Two"]')`))) {
                return `the kept change was not saved (message: ${await message()})`;
            }
            const stored = await evaluate(`fetch('/admin/api/config').then((r) => r.json()).then((c) => JSON.stringify([c.state.groups.indexOf('Pictured') < 0, c.state.group_images['Pictured Two']]))`);
            return stored === JSON.stringify([true, image]) ? null : `stored: ${stored}, the form held ${image}`;
        });

        await step('a group emptied and deleted takes its image along', async () => {
            // Also leaves the station list as the later steps expect it.
            if (!(await openPage('#edit/first', `document.querySelector('form.edit')`))) {
                return 'no edit form';
            }
            await evaluate(`document.querySelector('form.edit').elements.group.value = ''`);
            await evaluate(`document.querySelector('form.edit').requestSubmit()`);
            if (!(await until(`location.hash === '#stations' && document.querySelector('button[title="Delete group Pictured Two"]')`))) {
                return `the emptied group offers no Delete (message: ${await message()})`;
            }
            const deleted = await saveBy('button[title="Delete group Pictured Two"]');
            if (deleted.indexOf('Saved') < 0) {
                return `deleting the group says: ${deleted}`;
            }
            const stored = await evaluate(`fetch('/admin/api/config').then((r) => r.json()).then((c) => JSON.stringify([c.state.groups.indexOf('Pictured Two'), c.state.group_images]))`);
            return stored === JSON.stringify([-1, {}]) ? null : `stored: ${stored}`;
        });

        // The Covers page, on the two songs startTunebox seeds.
        const agents = `document.querySelector('.cover-row[data-key="agents of time|hearts collide"]')`;
        const jessica = `document.querySelector('.cover-row[data-key="jessica|summer song"]')`;
        const rowButton = (row, text) => evaluate(`(() => {
            const found = [...${row}.querySelectorAll('button')].find((b) => b.textContent.trim() === ${JSON.stringify(text)});
            if (!found) { throw new Error('no button ' + ${JSON.stringify(text)}); }
            found.click();
            return true;
        })()`);
        const coverView = (view) => click(`.covers-page button[data-view="${view}"]`);
        const rowState = (row) => evaluate(`${row} ? ${row}.dataset.state : null`);

        await step('the Covers page lists the songs that need a fix', async () => {
            if (!(await openPage('#covers', `${agents} && ${jessica}`))) {
                return `the Problems list holds ${await evaluate(`[...document.querySelectorAll('.cover-row')].map((r) => r.dataset.key + '=' + r.dataset.state).join() || 'nothing'`)} (message: ${await message()})`;
            }
            const states = `${await rowState(agents)} ${await rowState(jessica)}`;
            await noSideways('covers');
            return states === 'looking artist' ? null : `the rows are ${states}`;
        });

        await step('a cover search offers candidates and a pick shows as your fix', async () => {
            // Thumbnails come from Deezer's image host, answered here so they
            // load without the network.
            on('Fetch.requestPaused', (params) => {
                send('Fetch.fulfillRequest', {
                    requestId: params.requestId,
                    responseCode: 200,
                    responseHeaders: [{name: 'Content-Type', value: 'image/png'}],
                    body: smallPng().toString('base64'),
                });
            });
            await send('Fetch.enable', {patterns: [{urlPattern: 'https://*.dzcdn.net/*'}]});
            try {
                await rowButton(agents, 'Fix');
                if (!(await until(`${agents}.querySelector('form.cover-search')`))) {
                    return 'Fix opened no search';
                }
                await evaluate(`${agents}.querySelector('form.cover-search').requestSubmit()`);
                // Thumbnails load lazily, so only once on screen; the feed
                // of the first station may have put more songs above it.
                if (!(await until(`${agents}.querySelector('.cover-choice img')`, 15000))) {
                    return `no candidate (message: ${await message()}, note: ${await evaluate(`${agents}.querySelector('form.cover-search [role=alert]').textContent`)})`;
                }
                await evaluate(`${agents}.querySelector('.cover-choice').scrollIntoView()`);
                if (!(await until(`(() => { const img = ${agents}.querySelector('.cover-choice img'); return img && img.naturalWidth > 0; })()`))) {
                    return 'the candidate shows no image';
                }
                await noSideways('covers search');
                await evaluate(`${agents}.querySelector('.cover-choice').click()`);
                if (!(await until(`document.querySelector('.message').textContent.startsWith('Saved.')`))) {
                    return `the pick says: ${await message()}`;
                }
                // A fixed song is no problem any more: it shows under All.
                await coverView('all');
                // The worker's fetch of the picked image may have no network.
                if (!(await until(`${agents} && ['fix', 'fetching', 'failed'].includes(${agents}.dataset.state)`))) {
                    return `the row is ${await rowState(agents)}`;
                }
                return null;
            } finally {
                await send('Fetch.disable');
                on('Fetch.requestPaused', null);
            }
        });

        await step('Fixed by you lists it and Back to automatic undoes it', async () => {
            await coverView('fixed');
            if (!(await until(`${agents} && document.querySelectorAll('.cover-row').length === 1`))) {
                return `Fixed by you holds ${await evaluate(`document.querySelectorAll('.cover-row').length`)} rows`;
            }
            await rowButton(agents, 'Back to automatic');
            if (!(await until(`document.querySelector('.message').textContent === 'Back to automatic: tunebox looks the cover up again.'`))) {
                return `Back to automatic says: ${await message()}`;
            }
            return (await until(`!${agents} && !document.querySelector('.cover-row')`)) ? null : 'the row stayed under Fixed by you';
        });

        await step('an uploaded cover shows on its row', async () => {
            writeFileSync(join(transfer, 'cover.png'), smallPng());
            await coverView('all');
            if (!(await until(agents))) {
                return 'no row under All';
            }
            await rowButton(agents, 'Fix');
            if (!(await until(`${agents}.querySelector('input[name=cover-file]')`))) {
                return 'Fix opened no upload';
            }
            await setFile('input[name=cover-file]', join(transfer, 'cover.png'));
            if (!(await until(`document.querySelector('.message').textContent.startsWith('Uploaded.')`))) {
                return `the upload says: ${await message()}`;
            }
            // The list does not follow the worker that copies the image:
            // read it again until the row shows it.
            const shown = `(() => { const img = ${agents} && ${agents}.querySelector('img'); return ${agents} && ${agents}.dataset.state === 'fix' && img && img.naturalWidth > 0; })()`;
            for (let i = 0; i < 16; i++) {
                await coverView('all');
                await until(agents, 500);
                await evaluate(`${agents} && ${agents}.scrollIntoView()`);
                if (await until(shown, 500)) {
                    return null;
                }
            }
            return `the row is ${await rowState(agents)} and shows ${await evaluate(`${agents} && ${agents}.querySelector('img') ? ${agents}.querySelector('img').src : 'no image'`)}`;
        });

        await step('a link that is not a Deezer album is refused on the page', async () => {
            let searches = 0;
            on('Fetch.requestPaused', (params) => {
                searches += 1;
                send('Fetch.continueRequest', {requestId: params.requestId});
            });
            await send('Fetch.enable', {patterns: [{urlPattern: '*/admin/api/covers/search'}]});
            try {
                await rowButton(agents, 'Fix');
                const linkForm = `${agents}.querySelectorAll('form.cover-search')[1]`;
                if (!(await until(linkForm))) {
                    return 'Fix opened no link form';
                }
                await evaluate(`(() => {
                    const form = ${linkForm};
                    form.querySelector('input').value = 'https://deezer.page.link/x';
                    form.requestSubmit();
                })()`);
                const rule = 'Paste a Deezer album link (https://www.deezer.com/album/...). Share links (deezer.page.link) are not followed.';
                if (!(await until(`${linkForm}.querySelector('[role=alert]').textContent === ${JSON.stringify(rule)}`))) {
                    return `the form says: ${await evaluate(`${linkForm}.querySelector('[role=alert]').textContent`)}`;
                }
                await sleep(300);
                return searches === 0 ? null : `${searches} search request(s) went out`;
            } finally {
                await send('Fetch.disable');
                on('Fetch.requestPaused', null);
            }
        });

        await step('the search box finds a song by name', async () => {
            await coverView('all');
            await evaluate(`(() => {
                const box = document.querySelector('input[name=cover-search]');
                box.value = 'summer song';
                box.dispatchEvent(new Event('input'));
            })()`);
            if (!(await until(`document.querySelectorAll('.cover-row').length === 1 && ${jessica}`))) {
                return `the list holds ${await evaluate(`[...document.querySelectorAll('.cover-row')].map((r) => r.dataset.key).join() || 'nothing'`)}`;
            }
            return null;
        });

        rmSync(transfer, {recursive: true, force: true});

        // A first start: no account, the setup page on, the link from the log.
        await step('a long station list folds by group and finds a station', async () => {
            // Eighteen more stations in two groups make the list long enough to fold.
            const saved = await evaluate(`(async () => {
                const csrf = (await (await fetch('/admin/api/state')).json()).session.csrf;
                const config = await (await fetch('/admin/api/config')).json();
                const model = config.state.channels[0];
                const extra = [];
                for (let i = 0; i < 18; i++) {
                    const id = 'extra-' + i;
                    const group = i < 9 ? 'Ninety' : 'Hundred';
                    extra.push({...model, id, group, enabled: i % 4 !== 0, doc: {...model.doc, id, name: 'Extra Station ' + i}});
                }
                const groups = [...config.state.groups.filter((g) => g !== 'Ninety' && g !== 'Hundred'), 'Ninety', 'Hundred'];
                const answer = await fetch('/admin/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-Tunebox-CSRF': csrf},
                    body: JSON.stringify({based_on: config.version, state: {...config.state, groups, channels: [...config.state.channels, ...extra]}}),
                });
                return answer.status;
            })()`);
            if (saved !== 200) {
                return `saving the long list answered ${saved}`;
            }
            // The panel holds the configuration it loaded; a reload reads
            // the long list, as a visit would.
            await evaluate(`localStorage.removeItem('tunebox.admin.open-groups'); window.location.hash = '#stations'`);
            await send('Page.reload', {});
            if (!(await until(`document.querySelector('input.find')`))) {
                return 'no find box on a long list';
            }
            await noSideways('stations long');
            const ninety = `[...document.querySelectorAll('section.group')].find((g) => g.textContent.indexOf('Ninety') >= 0)`;
            const folded = await evaluate(`(() => { const g = ${ninety}; return JSON.stringify({folded: g.classList.contains('folded'), hidden: getComputedStyle(g.querySelector('ul')).display === 'none', summary: g.querySelector('.group-summary').textContent}); })()`);
            if (folded !== JSON.stringify({folded: true, hidden: true, summary: '9 stations · 3 off'})) {
                return `a group on a long list starts as ${folded}`;
            }
            await evaluate(`(() => { const f = document.querySelector('input.find'); f.focus(); f.value = 'extra station 12'; f.dispatchEvent(new Event('input', {bubbles: true})); })()`);
            const found = await evaluate(`JSON.stringify([...document.querySelectorAll('.station')].filter((s) => s.getClientRects().length > 0).map((s) => s.dataset.id))`);
            if (found !== JSON.stringify(['extra-12'])) {
                return `finding "extra station 12" shows ${found}`;
            }
            const focusKept = await evaluate(`document.activeElement === document.querySelector('input.find')`);
            await evaluate(`(() => { const f = document.querySelector('input.find'); f.value = ''; f.dispatchEvent(new Event('input', {bubbles: true})); })()`);
            if (!focusKept) {
                return 'the find box lost focus while typing';
            }
            await evaluate(`${ninety}.querySelector('button.fold').click()`);
            await evaluate(`document.body.dataset.beforeReload = '1'`);
            await send('Page.reload', {});
            if (!(await until(`!document.body.dataset.beforeReload && ${ninety}`))) {
                return 'no list after a reload';
            }
            const kept = await evaluate(`(() => { const g = ${ninety}; return g.querySelector('ul').getClientRects().length > 0 && g.querySelector('button.fold').getAttribute('aria-expanded') === 'true'; })()`);
            return kept ? null : 'an opened group did not stay open after a reload';
        });

        await step('the setup link shows the form, keeps its secret and leaves the address bar clean', async () => {
            const setupFolder = ownedFolder('tunebox-setup-');
            let first = null;
            try {
                const setupPort = await freePort();
                first = await startTunebox(setupFolder, setupPort, undefined, true);
                const line = first.log.join('').split('\n').find((text) => text.indexOf('admin_setup_token') >= 0);
                const token = line && JSON.parse(line).token;
                if (!token) {
                    return 'the log holds no setup link';
                }
                const link = `http://127.0.0.1:${setupPort}/admin/#setup=${token}`;
                // A page being replaced has no usable context for a moment.
                const shown = async (expression) => {
                    for (let i = 0; i < 50; i++) {
                        try {
                            if (await evaluate(`Boolean(${expression})`)) {
                                return true;
                            }
                        } catch (error) {
                            // Navigating.
                        }
                        await sleep(100);
                    }
                    return false;
                };
                await send('Page.navigate', {url: link});
                if (!(await shown(`document.querySelector('#setup') && !document.querySelector('#setup').hidden && document.querySelector('.secret').textContent !== ''`))) {
                    return 'the setup form did not show';
                }
                const secret = await evaluate(`document.querySelector('.secret').textContent`);
                await send('Page.reload');
                await sleep(300);
                if (!(await shown(`document.querySelector('#setup') && !document.querySelector('#setup').hidden && document.querySelector('.secret').textContent !== ''`))) {
                    return 'the setup form did not show after a reload';
                }
                const again = await evaluate(`document.querySelector('.secret').textContent`);
                if (again !== secret) {
                    return 'a reload showed another secret';
                }
                const code = totp(Math.floor(Date.now() / 30000), base32(secret));
                await evaluate(`(() => {
                    const form = document.querySelector('#setup');
                    form.password.value = 'a setup check password';
                    form.code.value = ${JSON.stringify(code)};
                    form.requestSubmit();
                })()`);
                if (!(await until(`document.querySelectorAll('ul.recovery li').length === 10`))) {
                    return `no recovery codes (message: ${await message()})`;
                }
                const where = await evaluate(`location.pathname + location.hash`);
                return where === '/admin/' ? null : `the address bar says ${where}`;
            } finally {
                if (first) {
                    const exited = new Promise((done) => first.child.once('exit', done));
                    killGroup(first.child.pid, 'SIGTERM');
                    await Promise.race([exited, sleep(5000)]);
                }
                rmSync(setupFolder, {recursive: true, force: true});
            }
        });
    } finally {
        if (browser) {
            for (const error of browser.errors) {
                failures.push(`page error: ${error}`);
            }
            await browser.close();
        }
        if (stream) {
            stream.close();
        }
        if (catalogue) {
            catalogue.close();
        }
        if (tunebox) {
            const exited = new Promise((done) => tunebox.child.once('exit', done));
            killGroup(tunebox.child.pid, 'SIGTERM');
            await Promise.race([exited, sleep(5000)]);
            if (process.env.DEBUG_TUNEBOX_LOG && failures.length) {
                writeFileSync(process.env.DEBUG_TUNEBOX_LOG, tunebox.log.join(''));
            }
            for (const problem of logProblems(tunebox.log.join(''))) {
                failures.push(`tunebox: ${problem}`);
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
