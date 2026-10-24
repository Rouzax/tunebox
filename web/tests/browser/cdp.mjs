// Headless Chrome over the DevTools protocol, shared by the browser checks.
// Node 22 or later (Node 20 needs --experimental-websocket).

import {spawn} from 'node:child_process';
import {existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from 'node:fs';
import os, {tmpdir} from 'node:os';
import {join} from 'node:path';

// No CDP call here takes more than a few seconds; a call that gets no answer
// means the renderer is gone, and waiting on it would hang the check.
const CALL_TIMEOUT_MS = 30000;

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The first Chrome or Chromium on PATH; CHROME overrides.
export function findChrome() {
    if (process.env.CHROME) {
        return process.env.CHROME;
    }
    const dirs = (process.env.PATH || '').split(':');
    for (const name of ['google-chrome', 'chromium', 'chromium-browser']) {
        const found = dirs.map((dir) => join(dir, name)).find((path) => existsSync(path));
        if (found) {
            return found;
        }
    }
    return 'google-chrome';
}

const PROFILE_PREFIX = 'tunebox-cdp-';

export const alive = (pid) => {
    try {
        process.kill(pid, 0);
        return true;
    } catch (error) {
        return error.code === 'EPERM';
    }
};

// Chrome runs in its own process group so one signal reaches the browser
// and every helper it forked; a helper left behind keeps writing the profile.
export function killGroup(pid, signal) {
    try {
        process.kill(-pid, signal);
    } catch (error) {
        // Already gone.
    }
}

function removeProfile(profile) {
    rmSync(profile, {recursive: true, force: true, maxRetries: 5, retryDelay: 200});
}

// A check killed outright (SIGKILL, a timeout) runs no exit handler; its
// profile names its owner, so the next check removes what that run left.
// The Chrome pid is killed only while its command line still names the
// profile, so a reused pid is never hit.
function sweepStaleProfiles() {
    for (const name of readdirSync(tmpdir())) {
        if (!name.startsWith(PROFILE_PREFIX)) {
            continue;
        }
        const profile = join(tmpdir(), name);
        let owner = null;
        try {
            owner = JSON.parse(readFileSync(join(profile, 'owner.json'), 'utf8'));
        } catch (error) {
            continue;
        }
        if (alive(owner.node)) {
            continue;
        }
        try {
            if (readFileSync(`/proc/${owner.chrome}/cmdline`, 'utf8').includes(profile)) {
                killGroup(owner.chrome, 'SIGKILL');
            }
        } catch (error) {
            // Chrome is gone too.
        }
        try {
            removeProfile(profile);
        } catch (error) {
            // Left for the next sweep.
        }
    }
}

// port: the DevTools port to open; errors collects page exceptions and
// console errors (CSP violations among them).
export async function connect({port}) {
    sweepStaleProfiles();
    const profile = mkdtempSync(join(tmpdir(), PROFILE_PREFIX));
    // /dev/shm is 64 MB in a container by default, too small for the
    // renderer; Chrome refuses to run as root without --no-sandbox, and an
    // unpacked Chrome for Testing has no sandbox where AppArmor blocks user
    // namespaces (Ubuntu 24.04), hence CHROME_NO_SANDBOX for that one.
    const args = [
        '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
        '--disable-dev-shm-usage', `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`,
    ];
    if ((process.getuid && process.getuid() === 0) || process.env.CHROME_NO_SANDBOX === '1') {
        args.push('--no-sandbox');
    }
    const chrome = spawn(findChrome(), [...args, 'about:blank'], {stdio: 'ignore', detached: true});
    writeFileSync(join(profile, 'owner.json'), JSON.stringify({node: process.pid, chrome: chrome.pid}));
    // A failed check can exit from anywhere; Chrome and its profile go too.
    const onExit = () => {
        killGroup(chrome.pid, 'SIGKILL');
        try {
            removeProfile(profile);
        } catch (error) {
            // The next check's sweep removes it.
        }
    };
    process.once('exit', onExit);
    for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
        if (process.listenerCount(signal) === 0) {
            process.once(signal, () => process.exit(128 + os.constants.signals[signal]));
        }
    }
    let targets = null;
    for (let i = 0; i < 100 && !targets; i++) {
        await sleep(100);
        try {
            targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
        } catch (error) {
            // Chrome is still starting.
        }
    }
    if (!targets) {
        process.removeListener('exit', onExit);
        onExit();
        throw new Error('Chrome did not start (is google-chrome or chromium installed?)');
    }
    const ws = new WebSocket(targets.find((t) => t.type === 'page').webSocketDebuggerUrl);
    await new Promise((resolve) => ws.addEventListener('open', resolve));
    let id = 0;
    const pending = new Map();
    const errors = [];
    // One handler per CDP event name, such as Fetch.requestPaused.
    const listeners = new Map();
    let lost = null;
    const failAll = (reason) => {
        lost = lost || reason;
        for (const call of pending.values()) {
            call.reject(new Error(lost));
        }
        pending.clear();
    };
    ws.addEventListener('close', () => failAll('the connection to Chrome closed'));
    ws.addEventListener('message', (event) => {
        const msg = JSON.parse(event.data);
        const listener = msg.method && listeners.get(msg.method);
        if (listener) {
            listener(msg.params);
        }
        if (msg.id && pending.has(msg.id)) {
            pending.get(msg.id).resolve(msg);
            pending.delete(msg.id);
        } else if (msg.method === 'Inspector.targetCrashed') {
            failAll('the page crashed in Chrome (too little /dev/shm or memory?)');
        } else if (msg.method === 'Runtime.exceptionThrown') {
            errors.push(msg.params.exceptionDetails.exception
                ? msg.params.exceptionDetails.exception.description
                : msg.params.exceptionDetails.text);
        } else if (msg.method === 'Log.entryAdded' && msg.params.entry.level === 'error'
            && !/Failed to load resource/.test(msg.params.entry.text)) {
            errors.push(msg.params.entry.text);
        }
    });
    const send = (method, params = {}) => new Promise((resolve, reject) => {
        if (lost) {
            reject(new Error(lost));
            return;
        }
        id += 1;
        const callId = id;
        const timer = setTimeout(() => {
            pending.delete(callId);
            reject(new Error(`${method} got no answer from Chrome in ${CALL_TIMEOUT_MS / 1000} s`));
        }, CALL_TIMEOUT_MS);
        const settle = (fn) => (value) => {
            clearTimeout(timer);
            fn(value);
        };
        pending.set(callId, {resolve: settle(resolve), reject: settle(reject)});
        ws.send(JSON.stringify({id: callId, method, params}));
    });
    await send('Inspector.enable');
    await send('Runtime.enable');
    await send('Log.enable');
    const close = async () => {
        ws.close();
        const exited = new Promise((resolve) => chrome.once('exit', resolve));
        killGroup(chrome.pid, 'SIGTERM');
        await Promise.race([exited, sleep(3000)]);
        process.removeListener('exit', onExit);
        onExit();
    };
    const evaluate = async (expression) => {
        const answer = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
        if (answer.result.exceptionDetails) {
            const details = answer.result.exceptionDetails;
            const why = details.exception && details.exception.description ? details.exception.description : details.text;
            throw new Error(`${expression.slice(0, 60)}: ${why}`);
        }
        return answer.result.result.value;
    };
    const on = (method, handler) => {
        if (handler) {
            listeners.set(method, handler);
        } else {
            listeners.delete(method);
        }
    };
    return {send, errors, close, evaluate, on};
}
