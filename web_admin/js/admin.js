// The admin shell: log in or set up, then the pages by their #route. It owns
// the loaded configuration and every save, so the pages only build states.
import qrcode from '/vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {render as renderAccount} from './account.js';
import {call, setCsrf} from './api.js';
import {render as renderEdit} from './edit.js';
import {render as renderHistory} from './history.js';
import {qrMatrix, qrSvg} from './qr.js';
import {render as renderSettings} from './settings.js';
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
    reauth_required: 'Type a code from your authenticator app to confirm this change.',
    no_new_secret: 'Start setting up the new authenticator again.',
    this_session: 'Use Log out to end this session.',
    bad_request: 'tunebox did not understand that. Reload the page and try again.',
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
// The hash the view was drawn for, to go back to when leaving is refused.
let shown = '';

const ctx = {
    config: null,
    // A page with edits of its own sets {dirty(), ask(proceed)}: ask shows
    // its question and calls proceed when the edits may go.
    unsaved: null,
    message,
    fail,
    // keep: the page moves on after a save and its "Saved ..." stays;
    // any other navigation clears the message.
    go(hash, keep) {
        if (!mayLeave(() => ctx.go(hash, keep))) {
            return;
        }
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
    // Any other admin call: a refused session goes to the login form, as
    // it does for saves.
    async request(method, path, body) {
        const answer = await call(method, path, body);
        if (answer.status === 401) {
            await answered(401, answer.data);
        }
        return answer;
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

// True when the page may be left now; otherwise the page asks, and
// proceed runs if the answer is to leave.
function mayLeave(proceed) {
    const page = ctx.unsaved;
    if (!page || !page.dirty()) {
        return true;
    }
    page.ask(() => {
        ctx.unsaved = null;
        proceed();
    });
    return false;
}

function route() {
    ctx.unsaved = null;
    if (!ctx.config) {
        return;
    }
    shown = window.location.hash;
    const hash = window.location.hash || '#stations';
    for (const link of document.querySelectorAll('#shell nav a')) {
        link.classList.toggle('current', hash.startsWith(link.getAttribute('href')));
    }
    const [name, arg] = hash.slice(1).split('/');
    if (name === 'edit' || name === 'new') {
        renderEdit(view, ctx, name === 'edit' ? decodeURIComponent(arg || '') : null);
    } else if (name === 'settings') {
        renderSettings(view, ctx);
    } else if (name === 'account') {
        renderAccount(view, ctx);
    } else if (name === 'history') {
        renderHistory(view, ctx, arg || null);
    } else {
        renderStations(view, ctx);
    }
}

window.addEventListener('beforeunload', (event) => {
    if (ctx.unsaved && ctx.unsaved.dirty()) {
        event.preventDefault();
        event.returnValue = '';
    }
});

// Links and ctx.go ask before the hash changes. Back and forward cannot
// be cancelled, so a refused one puts the old hash back without drawing.
document.querySelector('#shell nav').addEventListener('click', (event) => {
    const link = event.target.closest('a');
    if (link && !mayLeave(() => ctx.go(link.getAttribute('href')))) {
        event.preventDefault();
    }
});

window.addEventListener('hashchange', () => {
    const target = window.location.hash;
    if (!mayLeave(() => ctx.go(target))) {
        window.history.replaceState(null, '', shown || '#stations');
        return;
    }
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

// The password and the code go to the server together; the code has a form
// of its own because authenticator autofill (1Password on Android) fills a
// code only there. A refusal does not say which was wrong, so it starts
// again from the password.
const login = document.querySelector('#login');
const codeForm = document.querySelector('#code');

login.addEventListener('submit', (event) => {
    event.preventDefault();
    message('');
    show('code');
    codeForm.code.focus();
});

codeForm.querySelector('.back').addEventListener('click', () => {
    codeForm.code.value = '';
    show('login');
});

codeForm.addEventListener('submit', guarded(async (event) => {
    event.preventDefault();
    const answer = {password: login.password.value, code: codeForm.code.value};
    codeForm.code.value = '';
    const {ok, data} = await call('POST', 'login', answer);
    if (!ok) {
        fail(data);
        show('login');
        return;
    }
    login.password.value = '';
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

const logout = guarded(async () => {
    const {ok, data} = await call('POST', 'logout');
    if (!ok) {
        // Still logged in: do not pretend otherwise.
        fail(data);
        return;
    }
    setCsrf(null);
    ctx.config = null;
    ctx.unsaved = null;
    view.replaceChildren();
    message('');
    show('login');
});

document.querySelector('.logout').addEventListener('click', () => {
    if (mayLeave(logout)) {
        logout();
    }
});

refresh().catch(() => message('Could not reach tunebox.'));
