// Elements from data only: text goes in as text, never as markup.
export function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
        if (key === 'class') {
            node.className = value;
        } else if (key.startsWith('on')) {
            node.addEventListener(key.slice(2), value);
        } else if (key === 'value' || key === 'checked') {
            // Properties, not attributes: they are what a form reads back.
            node[key] = value;
        } else if (value === true) {
            node.setAttribute(key, '');
        } else if (value !== false && value !== null && value !== undefined) {
            node.setAttribute(key, String(value));
        }
    }
    for (const child of children.flat()) {
        if (child !== null && child !== undefined && child !== false) {
            node.append(child instanceof Node ? child : document.createTextNode(String(child)));
        }
    }
    return node;
}

const SPRITE = '/vendor/material-symbols-rounded-737e332/icons.svg';

export function icon(name) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', `${SPRITE}#${name}`);
    svg.append(use);
    return svg;
}

// A button with an icon; a label of null makes an icon-only button, named
// for screen readers by title.
export function button(iconName, label, onclick, extra) {
    const attrs = Object.assign({type: 'button', onclick}, extra || {});
    if (label === null) {
        attrs['aria-label'] = attrs.title;
        attrs.class = `${attrs.class || ''} plain`.trim();
    }
    return el('button', attrs, icon(iconName), label);
}

export function field(label, input, hint) {
    return el('label', null, label, input, hint ? el('span', {class: 'hint'}, hint) : null);
}

export function when(seconds) {
    return new Date(seconds * 1000).toLocaleString();
}

// Asks for a code in area and confirms it. Resolves true once the server
// has a fresh code, false when the owner cancels, null when the session
// ended meanwhile (ctx.request has then gone to the login form).
export function askCode(ctx, area) {
    return new Promise((resolve) => {
        const input = el('input', {name: 'code', autocomplete: 'one-time-code', inputmode: 'numeric', required: true});
        const form = el('form', {class: 'confirm-code'},
            field('Code from your authenticator app, or a recovery code', input),
            el('div', {class: 'actions'},
                el('button', {type: 'submit'}, icon('lock'), ' Confirm'),
                button('close', 'Cancel', () => {
                    form.remove();
                    resolve(false);
                }, {class: 'quiet'})));
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            // area may sit inside the form whose change this confirms, and
            // a submit bubbles: that form must not send its change again.
            event.stopPropagation();
            let answer;
            try {
                answer = await ctx.request('POST', 'account/reauth', {code: input.value});
            } catch (error) {
                // No answer: the prompt stays for another try or Cancel.
                ctx.message('Could not reach tunebox.');
                return;
            }
            input.value = '';
            if (answer.status === 204) {
                form.remove();
                resolve(true);
            } else if (answer.status === 401) {
                form.remove();
                resolve(null);
            } else {
                ctx.fail(answer.data);
            }
        });
        area.replaceChildren(form);
        input.focus();
    });
}

// Sends; when the server wants a fresh code, asks for one in area, sends
// the code, then sends again. Resolves with the last answer: status 0 when
// the owner cancels, 401 when the session ended at the prompt.
export async function confirmed(ctx, area, send) {
    const first = await send();
    if (first.status !== 403 || first.data.error !== 'reauth_required') {
        return first;
    }
    const typed = await askCode(ctx, area);
    if (typed) {
        return send();
    }
    return {ok: false, status: typed === null ? 401 : 0, data: {}};
}
