// Headless Chrome over the DevTools protocol, shared by the browser checks.
// Node 22 or later (Node 20 needs --experimental-websocket).

import {spawn} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
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

// port: the DevTools port to open; errors collects page exceptions and
// console errors (CSP violations among them).
export async function connect({port}) {
    const profile = mkdtempSync(join(tmpdir(), 'tunebox-cdp-'));
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
    const chrome = spawn(findChrome(), [...args, 'about:blank'], {stdio: 'ignore'});
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
        chrome.kill();
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
        chrome.kill();
        await Promise.race([exited, sleep(3000)]);
        try {
            rmSync(profile, {recursive: true, force: true});
        } catch (error) {
            // Chrome can still be writing its profile; it is only a temp folder.
        }
    };
    const evaluate = async (expression) => {
        const answer = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
        if (answer.result.exceptionDetails) {
            throw new Error(`${expression.slice(0, 60)}: ${answer.result.exceptionDetails.text}`);
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
