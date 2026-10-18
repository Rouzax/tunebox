// The admin shell: log in, set up, see who tunebox thinks you are, log out.
import qrcode from '/vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {qrMatrix, qrSvg} from './qr.js';

const ERRORS = {
    login_failed: 'Login failed.',
    slow_down: 'Too many attempts. Wait a little and try again.',
    setup_refused: 'This setup link is no longer valid.',
    password_too_short: 'The password needs at least 12 characters.',
    code_wrong: 'That code does not match. Type the one the app shows now.',
    read_only: 'tunebox cannot write its database right now. Try again later.',
    bad_origin: 'Open the admin panel at its public https address.',
};

let csrf = null;

async function call(method, path, body) {
    const headers = {'Content-Type': 'application/json'};
    if (csrf) {
        headers['X-Tunebox-CSRF'] = csrf;
    }
    const response = await fetch(`/admin/api/${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'same-origin',
        cache: 'no-store',
    });
    let data = {};
    if (response.status !== 204) {
        // A proxy's error page is not JSON; it still counts as an answer.
        data = await response.json().catch(() => ({}));
    }
    return {ok: response.ok, data};
}

function show(id) {
    for (const part of document.querySelectorAll('main > form, main > section')) {
        part.hidden = part.id !== id;
    }
}

function message(text) {
    const box = document.querySelector('.message');
    box.textContent = text || '';
    box.hidden = !text;
}

function fail(data) {
    message(ERRORS[data.error] || 'Something went wrong.');
}

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
        csrf = data.session.csrf;
        document.querySelector('.client').textContent = data.session.client;
        document.querySelector('.warning').hidden = !data.session.totp_suspected;
        show('shell');
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
    csrf = null;
    message('');
    show('login');
}));

refresh().catch(() => message('Could not reach tunebox.'));
