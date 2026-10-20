// The listening PIN's keypad, in the player and on the page a locked site
// shows. Its own keys work on the car's touch screen, which has no
// keyboard; typed digits work too.
export const PIN_MIN = 6;
export const PIN_MAX = 8;

const KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'back', '0', 'ok'];
const BACKSPACE = 'M22 3H7c-.69 0-1.23.35-1.59.88L0 12l5.41 8.11c.36.53.9.89 1.59.89h15c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-3 12.59L17.59 17 14 13.41 10.41 17 9 15.59 12.59 12 9 8.41 10.41 7 14 10.59 17.59 7 19 8.41 15.41 12 19 15.59z';
const MESSAGES = {
    401: 'Wrong PIN. Try again.',
    429: 'Too many tries. Wait a few minutes and try again.',
};
const BUSY = 'Busy, trying again...';
const RETRY_MS = 2000;

// The digits after a key: a digit adds one, back removes one, the rest
// changes nothing.
export function press(entry, key) {
    if (key === 'back') {
        return entry.slice(0, -1);
    }
    if (/^[0-9]$/.test(key) && entry.length < PIN_MAX) {
        return entry + key;
    }
    return entry;
}

export function messageFor(answer) {
    if (shouldRetry(answer)) {
        return BUSY;
    }
    return MESSAGES[answer.status] || 'Could not reach tunebox. Try again.';
}

// A busy answer is someone else's flood, not this PIN: one more try after
// the wait the server named, then the keypad waits for the driver again.
export function shouldRetry(answer) {
    return answer.status === 429 && answer.error === 'busy';
}

// Tab wraps around inside the keypad, so focus never reaches the player
// underneath it.
export function trapFocus(container, event) {
    if (event.key !== 'Tab') {
        return;
    }
    const items = container.querySelectorAll('button:not([disabled]):not([hidden]), input');
    if (!items.length) {
        return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    if (!event.shiftKey && event.target === last) {
        event.preventDefault();
        first.focus();
    } else if (event.shiftKey && event.target === first) {
        event.preventDefault();
        last.focus();
    }
}

// While the keypad shows, every poll gets a 401 too: one question, and one
// refresh after the answer.
export function onceAsked(ask, after) {
    let asking = false;
    return () => {
        if (asking) {
            return;
        }
        asking = true;
        ask().then((accepted) => {
            asking = false;
            if (accepted) {
                after();
            }
        });
    };
}

function node(tag, className, text) {
    const made = document.createElement(tag);
    if (className) {
        made.className = className;
    }
    if (text) {
        made.textContent = text;
    }
    return made;
}

function backIcon() {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', BACKSPACE);
    svg.append(path);
    return svg;
}

// options.onStop: over the player, the keypad offers Stop so the music
// does not have to play on while the PIN is typed. options.canStop is asked
// each time the keypad opens: Stop shows only while something plays.
export function bindPinScreen(root, api, options) {
    const screen = root.querySelector('.pin-screen');
    const title = node('h2', 'pin-title');
    title.id = 'pin-title';
    const dots = node('p', 'pin-dots');
    dots.setAttribute('aria-live', 'polite');
    const error = node('p', 'pin-error');
    error.setAttribute('role', 'alert');
    error.hidden = true;
    const keys = node('div', 'pin-keys');
    for (const name of KEYS) {
        const button = node('button', name === 'ok' ? 'pin-ok' : '', name === 'back' ? '' : (name === 'ok' ? 'OK' : name));
        button.type = 'button';
        button.dataset.key = name;
        if (name === 'back') {
            button.title = 'Delete a digit';
            button.setAttribute('aria-label', 'Delete a digit');
            button.append(backIcon());
        }
        keys.append(button);
    }
    const cancel = node('button', 'pin-cancel btn', 'Cancel');
    cancel.type = 'button';
    screen.setAttribute('aria-labelledby', 'pin-title');
    screen.append(title, dots, error, keys, cancel);
    let stop = null;
    if (options && options.onStop) {
        stop = node('button', 'pin-stop btn', 'Stop');
        stop.type = 'button';
        stop.hidden = true;
        stop.addEventListener('click', () => {
            stop.hidden = true;
            options.onStop();
        });
        screen.append(stop);
    }
    const ok = keys.querySelector('.pin-ok');

    let entry = '';
    let sending = false;
    let waiting = null;

    function draw() {
        dots.textContent = '•'.repeat(entry.length);
        ok.disabled = sending || entry.length < PIN_MIN;
    }

    function say(text) {
        error.textContent = text;
        error.hidden = !text;
    }

    function finish(accepted) {
        screen.hidden = true;
        const done = waiting.resolve;
        waiting = null;
        entry = '';
        draw();
        done(accepted);
    }

    function key(name) {
        if (!sending) {
            entry = press(entry, name);
            say('');
            draw();
        }
    }

    async function send() {
        try {
            return await api.pin(entry);
        } catch (failure) {
            return {status: 0, error: null};
        }
    }

    async function submit() {
        if (sending || entry.length < PIN_MIN) {
            return;
        }
        sending = true;
        draw();
        let answer = await send();
        if (shouldRetry(answer)) {
            say(messageFor(answer));
            await new Promise((done) => setTimeout(done, RETRY_MS));
            // Cancelled during the wait: the keypad is gone, send nothing.
            if (!waiting) {
                sending = false;
                draw();
                return;
            }
            answer = await send();
        }
        sending = false;
        if (answer.status === 204 && waiting) {
            finish(true);
            return;
        }
        entry = '';
        say(messageFor(answer));
        draw();
    }

    keys.addEventListener('click', (event) => {
        const button = event.target.closest('button[data-key]');
        if (!button) {
            return;
        }
        if (button.dataset.key === 'ok') {
            submit();
        } else {
            key(button.dataset.key);
        }
    });
    cancel.addEventListener('click', () => {
        if (waiting) {
            finish(false);
        }
    });
    document.addEventListener('keydown', (event) => {
        if (!waiting) {
            return;
        }
        trapFocus(screen, event);
        if (event.key === 'Enter' && event.target.closest && event.target.closest('.pin-stop, .pin-cancel')) {
            // Enter on Stop or Cancel presses that button, not OK.
            return;
        }
        if (event.key === 'Enter') {
            event.preventDefault();
            submit();
        } else if (event.key === 'Backspace') {
            key('back');
        } else if (event.key === 'Escape' && !cancel.hidden) {
            finish(false);
        } else {
            key(event.key);
        }
    });

    return {
        // Resolves true once a PIN is accepted, false when cancelled (only
        // offered when the player works without the PIN). Asking again
        // while the keypad shows waits for the same answer.
        ask(options) {
            if (!waiting) {
                let resolve;
                const promise = new Promise((done) => {
                    resolve = done;
                });
                waiting = {promise, resolve};
                title.textContent = options.title;
                cancel.hidden = !options.cancellable;
                entry = '';
                say('');
                draw();
                screen.hidden = false;
                if (stop) {
                    stop.hidden = !options.canStop();
                }
                keys.querySelector('button').focus();
            }
            return waiting.promise;
        },
    };
}
