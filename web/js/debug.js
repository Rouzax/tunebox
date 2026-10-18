import {createDebugLog, describeError, describeMedia} from './debuglog.js';

const MEDIA_EVENTS = [
    'loadstart', 'suspend', 'abort', 'emptied', 'stalled', 'loadedmetadata', 'loadeddata',
    'canplay', 'canplaythrough', 'play', 'playing', 'waiting', 'pause', 'ended',
];
const TICK_MS = 1000;

function element(tag, className, text) {
    const el = document.createElement(tag);
    el.className = className;
    if (text) {
        el.textContent = text;
    }
    return el;
}

// The ?debug=1 panel: a timeline of the player's audio events, readable and
// copyable on a phone or in the car, where no developer console opens.
// Nothing leaves the browser.
export function bindDebug(app) {
    const {root, events, player, toast} = app;
    const log = createDebugLog(() => Date.now());
    const panel = element('div', 'debug');
    const buttons = element('div', 'debug-buttons');
    const copy = element('button', 'debug-copy', 'Copy');
    const clear = element('button', 'debug-clear', 'Clear');
    const list = element('textarea', 'debug-list');
    copy.type = 'button';
    clear.type = 'button';
    list.readOnly = true;
    list.setAttribute('aria-label', 'Debug events');
    buttons.append(copy, clear);
    panel.append(buttons, list);
    root.append(panel);

    log.onLine(() => {
        list.value = log.text();
        list.scrollTop = list.scrollHeight;
    });
    // navigator.clipboard needs a secure context, and the player also runs
    // on plain http on a home network.
    copy.addEventListener('click', () => {
        list.focus();
        list.select();
        let copied = false;
        try {
            copied = document.execCommand('copy');
        } catch (error) {
            copied = false;
        }
        toast.show(copied ? 'Copied.' : 'Selected: copy it from the menu.');
    });
    clear.addEventListener('click', () => {
        log.clear();
        list.value = '';
    });

    const probe = document.createElement('audio');
    log.add(navigator.userAgent);
    log.add(`page ${window.location.protocol}//${window.location.host}, secure=${window.isSecureContext}`);
    log.add(`canPlayType aac="${probe.canPlayType('audio/aac')}" mpeg="${probe.canPlayType('audio/mpeg')}"`);

    let progress = 0;
    const watched = new WeakSet();
    events.on('audio', ({element: el, cors}) => {
        log.add(`new audio element, cors=${cors}`);
        if (watched.has(el)) {
            return;
        }
        watched.add(el);
        for (const type of MEDIA_EVENTS) {
            el.addEventListener(type, () => {
                if (el === player.element()) {
                    log.add(`${type} ${type === 'loadstart' ? el.currentSrc || el.src : describeMedia(el)}`);
                }
            });
        }
        el.addEventListener('progress', () => {
            progress += 1;
        });
        el.addEventListener('error', () => {
            if (el === player.element()) {
                log.add(`error ${describeError(el.error)}`);
            }
        });
    });
    events.on('state', (state) => {
        if (state === 'loading') {
            log.mark();
        }
        log.add(`state ${state}`);
    });
    events.on('debug', (note) => log.add(note));
    events.on('webaudio-unavailable', () => log.add('playing without Web Audio'));
    events.on('autoplay-refused', () => log.add('autoplay refused'));

    window.setInterval(() => {
        const el = player.element();
        if (el && player.state() !== 'stopped') {
            log.add(`tick ${describeMedia(el)} progress=${progress}`);
            progress = 0;
        }
    }, TICK_MS);
}
