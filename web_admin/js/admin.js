// The admin shell: log in or set up, then the pages by their #route. It owns
// the loaded configuration and every save, so the pages only build states.
import qrcode from '/vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {call, setCsrf} from './api.js';
import {render as renderEdit} from './edit.js';
import {render as renderHistory} from './history.js';
import {qrMatrix, qrSvg} from './qr.js';
import {render as renderStations} from './stations.js';

const ERRORS = {
    login_failed: 'Login failed.',
    slow_down: 'Too many attempts. Wait a little and try again.',
    setup_refused: 'This setup link is no longer valid.',
    password_too_short: 'The password needs at least 12 characters.',
    code_wrong: 'That code does not match. Type the one the app shows now.',
    read_only: 'tunebox cannot write its database right now. Try again later.',
    bad_origin: 'Open the admin panel at the address set as public_origin.',
    bad_csrf: 'This page is out of date. Reload it and try again.',
    not_found: 'Not found.',
};

function show(id) {
    for (const part of document.querySelectorAll('main > form, main > section')) {
        part.hidden = part.id !== id;
    }
    document.body.classList.toggle('in', id === 'shell');
}

function message(text) {
    const box = document.querySelector('.message');
    box.textContent = text || '';
    box.hidden = !text;
}

function fail(data) {
    message(ERRORS[data.error] || 'Something went wrong.');
}

// The server names fields by position (channels[3].streams[0].url); the
// station's id next to it says which one that is.
function explain(text, state) {
    return text.replace(/channels\[(\d+)\]/g, (whole, index) => {
        const row = state.channels[Number(index)];
        return row ? `${whole} (${row.id})` : whole;
    });
}

const view = document.querySelector('#shell .view');
let keepMessage = false;

const ctx = {
    config: null,
    message,
    fail,
    // keep: the page moves on after a save and its "Saved ..." stays;
    // any other navigation clears the message.
    go(hash, keep) {
        keepMessage = Boolean(keep);
        if (window.location.hash === hash) {
            if (!keepMessage) {
                message('');
            }
            keepMessage = false;
            route();
        } else {
            window.location.hash = hash;
        }
    },
    async reload() {
        const {ok, status, data} = await call('GET', 'config');
        if (!ok) {
            await answered(status, data);
            throw new Error('config');
        }
        ctx.config = data;
    },
    // Saves a whole state. Returns {ok} or {ok: false, message} with the
    // server's reason; a stale version reloads and asks to try again.
    save(state, target, done) {
        const body = {based_on: ctx.config.version, state, target};
        return once(() => call('POST', 'config', body), state, done);
    },
    restore(entryId) {
        const body = {based_on: ctx.config.version};
        return once(() => call('POST', `history/${entryId}/restore`, body), ctx.config.state, 'Restored.');
    },
};

let saving = false;

// One save at a time: a second click before the first answer would send
// the same version again and be refused as stale. The page is inert
// meanwhile. A request that got no answer may still have been stored, so
// the page asks what is stored before it draws anything.
async function once(send, state, done) {
    if (saving) {
        return {ok: false};
    }
    saving = true;
    view.inert = true;
    message('');
    try {
        return await commit(await send(), state, done);
    } catch (error) {
        try {
            await ctx.reload();
            message('No answer from tunebox: the change may or may not have been saved. The page shows what is stored now.');
        } catch (reloadError) {
            message('Could not reach tunebox.');
        }
        if (!onForm()) {
            route();
        }
        return {ok: false};
    } finally {
        saving = false;
        view.inert = false;
    }
}

function onForm() {
    return window.location.hash.startsWith('#edit') || window.location.hash === '#new';
}

// The form stays as typed after a refusal, also a stale one (its next Save
// applies the edit to the reloaded state); the other pages are drawn again
// from the stored state after any refusal, so a refused switch flips back.
async function commit(answer, state, done) {
    const {ok, status, data} = answer;
    if (ok) {
        try {
            await ctx.reload();
        } catch (error) {
            // Stored all the same. A refused reload already said why (a
            // session that ended, say); otherwise say what is missing.
            if (!document.querySelector('.message').textContent) {
                message(`${done || 'Saved.'} The page could not load the result; reload it to see it.`);
            }
            return {ok: true};
        }
        if (!onForm()) {
            route();
        }
        message(done || 'Saved.');
        return {ok: true};
    }
    if (status === 409) {
        await ctx.reload();
        if (onForm()) {
            message('Changed elsewhere in the meantime. Check the form and save again.');
        } else {
            route();
            message('Changed elsewhere; the page now shows the current stations. Check them before changing anything.');
        }
        return {ok: false};
    }
    if (status === 400 && data.error === 'invalid') {
        const text = explain(data.message, state);
        if (!onForm()) {
            route();
        }
        message(text);
        return {ok: false, message: text};
    }
    await answered(status, data);
    if (status !== 401 && !onForm()) {
        route();
    }
    return {ok: false};
}

// A refused session goes back to the login form; anything else is a message.
async function answered(status, data) {
    if (status === 401) {
        setCsrf(null);
        message('Your session ended. Log in again.');
        show('login');
        return;
    }
    fail(data);
}

function route() {
    if (!ctx.config) {
        return;
    }
    const hash = window.location.hash || '#stations';
    for (const link of document.querySelectorAll('#shell nav a')) {
        link.classList.toggle('current', hash.startsWith(link.getAttribute('href')));
    }
    const [name, arg] = hash.slice(1).split('/');
    if (name === 'edit' || name === 'new') {
        renderEdit(view, ctx, name === 'edit' ? decodeURIComponent(arg || '') : null);
    } else if (name === 'history') {
        renderHistory(view, ctx, arg || null);
    } else {
        renderStations(view, ctx);
    }
}

window.addEventListener('hashchange', () => {
    if (!keepMessage) {
        message('');
    }
    keepMessage = false;
    route();
});

function setupToken() {
    return new URLSearchParams(window.location.search).get('token');
}

async function refresh() {
    const {ok, data} = await call('GET', 'state');
    if (!ok) {
        // An error is not an answer about the account: say so, change nothing.
        fail(data);
        return;
    }
    if (data.session) {
        setCsrf(data.session.csrf);
        document.querySelector('.client').textContent = data.session.client;
        document.querySelector('.warning').hidden = !data.session.totp_suspected;
        show('shell');
        await ctx.reload();
        route();
    } else if (data.account) {
        show('login');
    } else if (data.setup && setupToken()) {
        await startSetup(setupToken());
    } else {
        show('none');
    }
}

async function startSetup(token) {
    const {ok, data} = await call('POST', 'setup/start', {token});
    if (!ok) {
        fail(data);
        show('none');
        return;
    }
    document.querySelector('.qr-box').replaceChildren(qrSvg(document, qrMatrix(qrcode, data.uri)));
    document.querySelector('.secret').textContent = data.secret;
    show('setup');
}

// Runs a handler and turns a network failure into a message instead of
// nothing at all.
function guarded(handler) {
    return (event) => handler(event).catch(() => message('Could not reach tunebox.'));
}

document.querySelector('#login').addEventListener('submit', guarded(async (event) => {
    event.preventDefault();
    const form = event.target;
    const answer = {password: form.password.value, code: form.code.value};
    form.code.value = '';
    const {ok, data} = await call('POST', 'login', answer);
    if (!ok) {
        fail(data);
        return;
    }
    form.password.value = '';
    message('');
    await refresh();
}));

document.querySelector('#setup').addEventListener('submit', guarded(async (event) => {
    event.preventDefault();
    const form = event.target;
    const answer = {token: setupToken(), password: form.password.value, code: form.code.value};
    const {ok, data} = await call('POST', 'setup/finish', answer);
    if (!ok) {
        fail(data);
        return;
    }
    form.password.value = '';
    const items = data.recovery_codes.map((code) => {
        const item = document.createElement('li');
        item.textContent = code;
        return item;
    });
    document.querySelector('.recovery').replaceChildren(...items);
    message('');
    show('codes');
}));

document.querySelector('.to-login').addEventListener('click', () => {
    window.history.replaceState(null, '', '/admin/');
    show('login');
});

document.querySelector('.logout').addEventListener('click', guarded(async () => {
    const {ok, data} = await call('POST', 'logout');
    if (!ok) {
        // Still logged in: do not pretend otherwise.
        fail(data);
        return;
    }
    setCsrf(null);
    ctx.config = null;
    view.replaceChildren();
    message('');
    show('login');
}));

refresh().catch(() => message('Could not reach tunebox.'));
