// The account: password, authenticator, recovery codes and the browsers
// that are logged in. Each change asks for a code first (within 5 minutes
// of the last one), and signs every other browser out.
import qrcode from '/vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {button, el, field, icon, when} from './dom.js';
import {qrMatrix, qrSvg} from './qr.js';

// Sends; when the server wants a fresh code, asks for one in area, sends
// the code, then sends again. Resolves with the last answer.
async function confirmed(ctx, area, send) {
    const first = await send();
    if (first.status !== 403 || first.data.error !== 'reauth_required') {
        return first;
    }
    return new Promise((resolve) => {
        const input = el('input', {name: 'code', autocomplete: 'one-time-code', inputmode: 'numeric', required: true});
        const form = el('form', {class: 'confirm-code'},
            field('Code from your authenticator app, or a recovery code', input),
            el('div', {class: 'actions'},
                el('button', {type: 'submit'}, icon('lock'), ' Confirm'),
                button('close', 'Cancel', () => {
                    form.remove();
                    resolve({ok: false, status: 0, data: {}});
                }, {class: 'quiet'})));
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const answer = await ctx.request('POST', 'account/reauth', {code: input.value});
            input.value = '';
            if (answer.status === 204) {
                form.remove();
                resolve(await send());
            } else if (answer.status !== 401) {
                ctx.fail(answer.data);
            }
        });
        area.replaceChildren(form);
        input.focus();
    });
}

function passwordPart(ctx) {
    const area = el('div');
    const min = ctx.limits.password_min;
    const first = el('input', {type: 'password', name: 'new-password', autocomplete: 'new-password', minlength: String(min), required: true});
    const again = el('input', {type: 'password', name: 'new-password-again', autocomplete: 'new-password', minlength: String(min), required: true});
    const form = el('form', {class: 'settings'},
        el('h2', null, icon('key'), ' Password'),
        field(`New password (at least ${min} characters)`, first),
        field('The same again', again),
        el('div', {class: 'actions'}, el('button', {type: 'submit'}, icon('check_circle'), ' Change the password')),
        area);
    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (first.value !== again.value) {
            ctx.message('The two passwords differ.');
            return;
        }
        const password = first.value;
        const answer = await confirmed(ctx, area, () => ctx.request('POST', 'account/password', {password}));
        if (answer.ok) {
            first.value = '';
            again.value = '';
            ctx.message('Password changed. Every other browser is signed out.');
        } else if (answer.status && answer.status !== 401) {
            ctx.fail(answer.data);
        }
    });
    return form;
}

function authenticatorPart(ctx) {
    const area = el('div');
    const part = el('section', {class: 'settings'},
        el('h2', null, icon('qr_code'), ' Authenticator app'),
        el('p', {class: 'hint'}, 'Move the login codes to a new app or phone. The old app stops working once the new one is confirmed.'),
        el('div', {class: 'actions'}, button('qr_code', 'Set up a new authenticator', start)),
        area);
    async function start() {
        const answer = await confirmed(ctx, area, () => ctx.request('POST', 'account/totp/start'));
        if (!answer.ok) {
            if (answer.status && answer.status !== 401) {
                ctx.fail(answer.data);
            }
            return;
        }
        const input = el('input', {name: 'code', autocomplete: 'one-time-code', inputmode: 'numeric', required: true});
        const form = el('form', {class: 'new-totp'},
            el('p', null, 'Scan the code with the new app, then type the code it shows.'),
            el('div', {class: 'qr-box'}, qrSvg(document, qrMatrix(qrcode, answer.data.uri))),
            el('p', null, 'Or type this secret: ', el('code', null, answer.data.secret)),
            field('Code from the new app', input),
            el('div', {class: 'actions'}, el('button', {type: 'submit'}, icon('check_circle'), ' Confirm')));
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const done = await confirmed(ctx, area, () => ctx.request('POST', 'account/totp/finish', {code: input.value}));
            input.value = '';
            if (done.ok) {
                area.replaceChildren();
                ctx.message('The new authenticator is in use. Every other browser is signed out.');
            } else if (done.status && done.status !== 401) {
                ctx.fail(done.data);
            }
        });
        area.replaceChildren(form);
        input.focus();
    }
    return part;
}

function recoveryPart(ctx) {
    const area = el('div');
    const part = el('section', {class: 'settings'},
        el('h2', null, icon('lock'), ' Recovery codes'),
        el('p', {class: 'hint'}, 'Each replaces a code from the app once, for when the phone is gone. New ones make the old ones stop working.'),
        el('div', {class: 'actions'}, button('add', 'Make new recovery codes', make)),
        area);
    async function make() {
        const answer = await confirmed(ctx, area, () => ctx.request('POST', 'account/recovery'));
        if (answer.ok) {
            area.replaceChildren(
                el('p', null, 'Keep these somewhere safe; they are not shown again.'),
                el('ul', {class: 'recovery'}, answer.data.recovery_codes.map((code) => el('li', null, code))));
            ctx.message('New recovery codes made. Every other browser is signed out.');
        } else if (answer.status && answer.status !== 401) {
            ctx.fail(answer.data);
        }
    }
    return part;
}

function sessionsPart(ctx, sessions, redraw) {
    const rows = sessions.map((s) => el('tr', null,
        el('td', null, s.user_agent || 'Unknown browser'),
        el('td', null, s.address || ''),
        el('td', null, when(s.last_seen)),
        el('td', null, s.current
            ? el('span', {class: 'hint'}, 'This browser')
            : button('logout', 'Sign out', async () => {
                const answer = await ctx.request('POST', `account/sessions/${s.id}/end`);
                if (answer.ok) {
                    ctx.message('Signed out.');
                } else if (answer.status !== 401) {
                    ctx.fail(answer.data);
                }
                redraw();
            }, {class: 'quiet'}))));
    return el('section', {class: 'settings'},
        el('h2', null, icon('devices'), ' Logged in'),
        el('div', {class: 'scroll'}, el('table', {class: 'sessions'},
            el('thead', null, el('tr', null, el('th', null, 'Browser'), el('th', null, 'Address'), el('th', null, 'Last used'), el('th', null, ''))),
            el('tbody', null, rows))));
}

export async function render(root, ctx) {
    const current = ctx.stillOpen;
    const answer = await ctx.request('GET', 'account');
    if (!current()) {
        return;
    }
    if (!answer.ok) {
        if (answer.status !== 401) {
            ctx.fail(answer.data);
        }
        return;
    }
    const redraw = () => render(root, ctx);
    root.replaceChildren(
        sessionsPart(ctx, answer.data.sessions, redraw),
        passwordPart(ctx),
        authenticatorPart(ctx),
        recoveryPart(ctx));
}
