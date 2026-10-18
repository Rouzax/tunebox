import {createWatchdog, lagSeconds, retryDelayMs} from './reconnect.js';

// MediaError code 4. Browsers report an unplayable format, a stream refused
// under CORS and an HTTP error on a new source (a 503 while a server
// restarts) all the same way. Formats are checked before playing
// (channels.canPlay), so here it means CORS only for a stream that has not
// played yet; otherwise it is a connection problem like any other.
const NOT_SUPPORTED = 4;
const TICK_MS = 1000;

// One live stream. Stop drops the source instead of pausing, so play always
// starts at live and nothing keeps downloading. While a stream is wanted the
// player never gives up by itself: dead streams are retried with backoff.
// Events: state, audio ({element, cors}), webaudio-unavailable, autoplay-refused
// ({resumed}: refused on a reconnect), interrupted (paused from outside),
// debug (a reconnect decision, as text).
export function createPlayer({events, makeAudio, timers, now}) {
    let audio = null;
    let audioCors = true;
    let url = null;
    let requestedAt = 0;
    let state = 'stopped';
    let attempt = 0;
    let retry = null;
    let tick = null;
    let volume = 0.5;
    let muted = false;
    // Streams that only play without CORS, and so without Web Audio.
    const noCors = new Set();
    const played = new Set();
    const watchdog = createWatchdog();

    function setState(next) {
        if (state !== next) {
            state = next;
            events.emit('state', next);
        }
    }

    function element(cors) {
        if (audio && audioCors === cors) {
            return audio;
        }
        if (audio) {
            audio.removeAttribute('src');
            audio.load();
        }
        // A media element routed into Web Audio stays routed, so playing
        // without CORS needs a fresh element.
        const el = makeAudio();
        if (cors) {
            el.crossOrigin = 'anonymous';
        }
        el.preload = 'none';
        el.volume = volume;
        el.muted = muted;
        el.addEventListener('playing', () => {
            if (el === audio && url) {
                played.add(url);
                setState('playing');
            }
        });
        el.addEventListener('progress', () => {
            if (el === audio && url) {
                watchdog.data(now());
            }
        });
        el.addEventListener('error', () => {
            if (el === audio && url) {
                failed(el.error ? el.error.code : 0);
            }
        });
        // A pause the player did not ask for (a phone call, headphones
        // unplugged) is a stop: resuming on a stall would undo it. The
        // player never pauses; it drops the source, which clears url first.
        el.addEventListener('pause', () => {
            if (el === audio && url && el.paused && !el.ended) {
                halt();
                events.emit('interrupted');
            }
        });
        el.addEventListener('ended', () => {
            if (el === audio && url) {
                scheduleRetry('stream ended');
            }
        });
        audio = el;
        audioCors = cors;
        events.emit('audio', {element: el, cors});
        return el;
    }

    function connect() {
        const resumed = state === 'reconnecting';
        const el = element(!noCors.has(url));
        watchdog.start(now());
        requestedAt = now();
        el.src = url;
        const started = el.play();
        if (started && started.catch) {
            started.catch((error) => {
                // Other rejections come with an error event, handled there.
                if (el === audio && url && error && error.name === 'NotAllowedError') {
                    halt();
                    events.emit('autoplay-refused', {resumed});
                }
            });
        }
    }

    function failed(code) {
        if (code === NOT_SUPPORTED && !played.has(url) && !noCors.has(url)) {
            noCors.add(url);
            events.emit('debug', 'media error 4 before playing, retrying without CORS');
            events.emit('webaudio-unavailable');
            connect();
            return;
        }
        scheduleRetry(`media error ${code}`);
    }

    // The reason goes to the ?debug=1 panel.
    function scheduleRetry(reason) {
        if (retry !== null) {
            return;
        }
        setState('reconnecting');
        const delay = retryDelayMs(attempt);
        events.emit('debug', `${reason}, reconnecting in ${delay / 1000} s`);
        attempt += 1;
        retry = timers.setTimeout(() => {
            retry = null;
            if (url) {
                connect();
            }
        }, delay);
    }

    function onTick() {
        if (!url || retry !== null) {
            return;
        }
        const at = now();
        if (watchdog.observe(audio.currentTime, at) === 'stalled') {
            scheduleRetry('stalled');
        } else if (watchdog.healthy(at)) {
            attempt = 0;
        }
    }

    function halt() {
        url = null;
        if (retry !== null) {
            timers.clearTimeout(retry);
            retry = null;
        }
        if (tick !== null) {
            timers.clearInterval(tick);
            tick = null;
        }
        if (audio) {
            audio.removeAttribute('src');
            audio.load();
        }
        setState('stopped');
    }

    return {
        play(streamUrl) {
            url = streamUrl;
            attempt = 0;
            if (retry !== null) {
                timers.clearTimeout(retry);
                retry = null;
            }
            setState('loading');
            connect();
            if (tick === null) {
                tick = timers.setInterval(onTick, TICK_MS);
            }
        },
        stop: halt,
        online() {
            if (url && retry !== null) {
                timers.clearTimeout(retry);
                retry = null;
                connect();
            }
        },
        state: () => state,
        bufferS() {
            return audio && state === 'playing' ? lagSeconds(now() - requestedAt, audio.currentTime) : 0;
        },
        setVolume(level) {
            volume = level;
            if (audio) {
                audio.volume = level;
            }
        },
        setMuted(on) {
            muted = on;
            if (audio) {
                audio.muted = on;
            }
        },
        element: () => audio,
    };
}
