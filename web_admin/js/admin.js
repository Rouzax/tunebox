// The admin shell: log in or set up, then the pages by their #route. It owns
// the loaded configuration and every save, so the pages only build states.
import qrcode from '/vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {render as renderAccount} from './account.js';
import {call, send, setCsrf} from './api.js';
import {askCode} from './dom.js';
import {render as renderEdit} from './edit.js';
import {render as renderHistory} from './history.js';
import {qrMatrix, qrSvg} from './qr.js';
import {render as renderSettings} from './settings.js';
import {render as renderStations} from './stations.js';
import {render as renderUsage} from './usage.js';
import {decodeHashPart, saveMessage, tickets} from './state.js';

const ERRORS = {
    login_failed: 'Login failed.',
    slow_down: 'Too many attempts. Wait a little and try again.',
    setup_refused: 'This setup link is no longer valid.',
    already_set_up: 'tunebox is already set up. Log in; if you did not note the recovery codes, make new ones on the Account page.',
    code_wrong: 'That code does not match. Type the one the app shows now.',
    read_only: 'tunebox cannot write its database right now. Try again later.',
    bad_origin: 'Open the admin panel at the address set as public_origin.',
    bad_csrf: 'This page is out of date. Reload it and try again.',
    not_found: 'Not found.',
    reauth_required: 'Type a code from your authenticator app to confirm this change.',
    no_new_secret: 'Start setting up the new authenticator again.',
    this_session: 'Use Log out to end this session.',
    bad_request: 'tunebox did not understand that. Reload the page and try again.',
    too_large: 'That is too large to send.',
    not_an_image: 'That file is not a PNG, JPEG, WebP or SVG image.',
    unsupported_type: 'Upload a PNG, JPEG, WebP or SVG image.',
    logo_store_full: 'Uploaded logos have reached their 100 MB limit. A logo that no station and no history entry uses is removed at a restart once it is a day old.',
    disk_low: 'The disk tunebox keeps its data on is almost full, so nothing was saved. Free some space and try again.',
    logo_store_failed: 'tunebox cannot store the image right now. Check the free disk space.',
    usage_unavailable: 'tunebox cannot read its usage data right now.',
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
    if (data.error === 'password_too_short') {
        message(`The password needs at least ${ctx.limits.password_min} characters.`);
        return;
    }
    message(ERRORS[data.error] || 'Something went wrong.');
}

// The server holds a body this long, whatever its size (BODY_DEADLINE_S); a
// shorter wait here would cut off a slow upload it would still accept.
const UPLOAD_DEADLINE_MS = 30000;

const view = document.querySelector('#shell .view');
// Outside the view, which is inert while a save waits for its answer.
const codeArea = document.querySelector('.shell-code');
let keepMessage = false;
// The hash the view was drawn for, to go back to when leaving is refused.
let shown = '';

const pages = tickets();

const ctx = {
    config: null,
    // True while the page being drawn is still the one open; a page that
    // waits for an answer checks it before it draws.
    stillOpen: () => true,
    // The server's limits, from GET state; these are what it enforces
    // before it has answered.
    limits: {password_min: 12, pin_min: 6, pin_max: 8, logo_max: 2000000},
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
    // basedOn is the version the state was built on; a change kept across a
    // login passes the one it started from, so the stale check still holds.
    // keep marks a form's save outside the station form (the group form on
    // the Stations page), kept for the next login as that form's is.
    save(state, target, done, basedOn, keep) {
        const body = {based_on: basedOn === undefined ? ctx.config.version : basedOn, state, target};
        return once(() => call('POST', 'config', body), done, {state, target, done, basedOn: body.based_on, keep: Boolean(keep)});
    },
    // Any other admin call: a refused session goes to the login form, as
    // it does for saves. options as for call.
    async request(method, path, body, options) {
        const answer = await call(method, path, body, options);
        if (answer.status === 401) {
            await answered(401, answer.data);
        }
        return answer;
    },
    // Stores a logo image; the answer names it as blob:<sha256>. kept()
    // gives the {state, target, done} of the form's unsaved change, which a
    // session that ended meanwhile offers again after login, as for a save.
    // That change never holds the image (the upload was refused before it
    // was stored), so the offer says to upload it again.
    async upload(file, kept) {
        const answer = await send('POST', 'logos', file, file.type, {deadlineMs: UPLOAD_DEADLINE_MS});
        if (answer.status === 401) {
            const change = kept ? kept() : null;
            const usable = change && !change.problem;
            await answered(401, answer.data, usable && {...change, basedOn: ctx.config.version, upload: true});
        }
        return answer;
    },
    // Replaces the stations with an imported file, as one save. A stale
    // version reloads and a missing fresh code is reported, both without
    // redrawing the page, so it can show the file again.
    async importFile(toml) {
        const body = {toml, based_on: ctx.config.version};
        return once(() => call('POST', 'import', body), 'Imported.', undefined, async (answer) => {
            if (answer.status === 409) {
                await ctx.reload();
                return {ok: false, stale: true};
            }
            if (answer.status === 403 && answer.data.error === 'reauth_required') {
                return {ok: false, reauth: true};
            }
            return null;
        });
    },
    restore(entryId) {
        const body = {based_on: ctx.config.version};
        return once(() => call('POST', `history/${entryId}/restore`, body), 'Restored.');
    },
};

// The save in progress, settled once its view is usable again; null when
// none is.
let saving = null;

// One save at a time: a second click before the first answer would send
// the same version again and be refused as stale. The page is inert
// meanwhile. A request that got no answer may still have been stored, so
// the page asks what is stored before it draws anything.
// early may answer for the caller before the shared handling (a page that
// keeps its own state across a refusal); null leaves the answer to commit.
// Without early, a refusal for want of a fresh code asks for one here and
// sends the same change again.
async function once(send, done, retry, early) {
    if (saving) {
        return {ok: false};
    }
    let ended;
    saving = new Promise((resolve) => {
        ended = resolve;
    });
    view.inert = true;
    message('');
    try {
        let answer = await send();
        // A change that shows a hidden station needs a fresh code: ask for
        // one outside the inert view, then send the same change again.
        while (!early && answer.status === 403 && answer.data.error === 'reauth_required') {
            const typed = await askCode(ctx, codeArea);
            if (typed === null) {
                // The session ended at the prompt: keep a form's change
                // for the next login, as a refused save does.
                await answered(401, {}, retry);
                return {ok: false};
            }
            if (!typed) {
                if (!onForm()) {
                    route();
                }
                message('Not saved. Showing a station the PIN hides needs a code from your authenticator app.');
                return {ok: false};
            }
            answer = await send();
        }
        const handled = early ? await early(answer) : null;
        return handled || await commit(answer, done, retry);
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
        view.inert = false;
        codeArea.replaceChildren();
        saving = null;
        ended();
    }
}

function onForm() {
    return window.location.hash.startsWith('#edit') || window.location.hash === '#new';
}

// The form stays as typed after a refusal, also a stale one (its next Save
// applies the edit to the reloaded state); the other pages are drawn again
// from the stored state after any refusal, so a refused switch flips back.
async function commit(answer, done, retry) {
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
        message(saveMessage(data, done || 'Saved.'));
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
        return {ok: false, stale: true};
    }
    if (status === 400 && data.error === 'invalid') {
        const text = data.message;
        if (!onForm()) {
            route();
        }
        message(text);
        return {ok: false, message: text};
    }
    await answered(status, data, retry);
    if (status !== 401 && !onForm()) {
        route();
    }
    return {ok: false};
}

// The save a form lost to an ended session, offered again after login. One
// at a time: a newer one replaces it.
let pending = null;

// A refused session goes back to the login form; anything else is a message.
// A form's save that the session ended is kept for the next login: the
// station form's, and a save marked keep. Any other page is drawn again
// from the stored state after the login.
async function answered(status, data, retry) {
    if (status === 401) {
        if (retry && (onForm() || retry.keep)) {
            pending = retry;
        }
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

function showPending() {
    const box = document.querySelector('#shell .pending');
    if (!pending) {
        box.hidden = true;
        box.replaceChildren();
        return;
    }
    const change = pending;
    const text = document.createElement('span');
    text.textContent = change.upload
        ? 'The image was not uploaded because your session ended. Save it now keeps the other changes in the form; then upload the image again.'
        : 'A change was not saved because your session ended.';
    const save = document.createElement('button');
    save.type = 'button';
    save.textContent = 'Save it now';
    save.addEventListener('click', guarded(async () => {
        const result = await ctx.save(change.state, change.target, change.done, change.basedOn);
        if (result.ok) {
            pending = null;
            showPending();
            ctx.go('#stations', true);
        } else if (result.stale) {
            // Resent against the version it started from, so the refusal
            // says the stations moved on; the kept change would overwrite them.
            pending = null;
            showPending();
            message('The stations changed elsewhere, so the kept change was not saved.');
        }
    }));
    const discard = document.createElement('button');
    discard.type = 'button';
    discard.className = 'quiet';
    discard.textContent = 'Discard';
    discard.addEventListener('click', () => {
        pending = null;
        showPending();
    });
    box.replaceChildren(text, save, discard);
    box.hidden = false;
}

function route() {
    ctx.unsaved = null;
    if (!ctx.config) {
        return;
    }
    shown = window.location.hash;
    ctx.stillOpen = pages.take();
    const hash = window.location.hash || '#stations';
    for (const link of document.querySelectorAll('#shell nav a')) {
        link.classList.toggle('current', hash.startsWith(link.getAttribute('href')));
    }
    const [name, arg] = hash.slice(1).split('/');
    if (name === 'edit' || name === 'new') {
        // A malformed id is shown as typed, as a station that does not exist.
        const id = decodeHashPart(arg || '');
        renderEdit(view, ctx, name === 'edit' ? (id === null ? arg : id) : null);
    } else if (name === 'settings') {
        renderSettings(view, ctx);
    } else if (name === 'usage') {
        renderUsage(view, ctx);
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

// The setup token comes in the fragment (#setup=...), which the browser
// never sends to a server or a proxy's log. It is URL-safe base64.
function setupToken() {
    const match = /^#setup=([0-9A-Za-z_-]+)$/.exec(window.location.hash);
    return match ? match[1] : null;
}

async function refresh() {
    const {ok, data} = await call('GET', 'state');
    if (!ok) {
        // An error is not an answer about the account: say so, change nothing.
        fail(data);
        return;
    }
    ctx.limits = data.limits || ctx.limits;
    if (data.session) {
        setCsrf(data.session.csrf);
        document.querySelector('.client').textContent = data.session.client;
        document.querySelector('.warning').hidden = !data.session.totp_suspected;
        show('shell');
        await ctx.reload();
        route();
        showPending();
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
        if (data.error === 'already_set_up') {
            window.history.replaceState(null, '', '/admin/');
            show('login');
        } else {
            show('none');
        }
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
        if (data.error === 'already_set_up') {
            form.password.value = '';
            window.history.replaceState(null, '', '/admin/');
            show('login');
        }
        return;
    }
    form.password.value = '';
    // The token is spent; do not leave it in the address bar.
    window.history.replaceState(null, '', '/admin/');
    const items = data.recovery_codes.map((code) => {
        const item = document.createElement('li');
        item.textContent = code;
        return item;
    });
    document.querySelector('.recovery').replaceChildren(...items);
    message('');
    show('codes');
}));

document.querySelector('.to-login').addEventListener('click', () => show('login'));

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
    pending = null;
    showPending();
    view.replaceChildren();
    message('');
    show('login');
});

document.querySelector('.logout').addEventListener('click', async () => {
    // A save waiting at the code prompt holds the view inert, where the
    // page's question about unsaved edits could not be answered: the save
    // ends as cancelled first, so the next login can save again.
    const cancel = codeArea.querySelector('.confirm-code .quiet');
    if (cancel && saving) {
        cancel.click();
        await saving;
    }
    if (mayLeave(logout)) {
        logout();
    }
});

refresh().catch(() => message('Could not reach tunebox.'));
