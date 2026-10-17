// Each reconnect downloads a burst of about 1 MB of past audio, so retries back off.
export const RETRY_S = [2, 4, 8, 16, 30, 60];
// Playback time standing still this long while "playing" means the stream died.
export const STALL_MS = 8000;
// The backoff starts over after this much clean playback.
export const HEALTHY_MS = 60000;
// The server accepts at most this much buffer.
export const MAX_BUFFER_S = 120;

export function retryDelayMs(attempt) {
    return RETRY_S[Math.min(attempt, RETRY_S.length - 1)] * 1000;
}

export function createWatchdog() {
    let lastTime = -1;
    let lastChange = 0;
    let since = 0;
    return {
        start(now) {
            lastTime = -1;
            lastChange = now;
            since = now;
        },
        observe(currentTime, now) {
            if (currentTime !== lastTime && currentTime > 0) {
                lastTime = currentTime;
                lastChange = now;
                return 'ok';
            }
            return now - lastChange >= STALL_MS ? 'stalled' : 'ok';
        },
        healthy(now) {
            return now - since >= HEALTHY_MS && now - lastChange < STALL_MS;
        },
    };
}

// How far playback has fallen behind the moment the stream was asked for:
// start-up, buffering and stalls. The audio the stream sends on connect
// comes on top; tunebox measures and adds that, because media.buffered
// cannot show it (Chromium keeps only a couple of seconds there).
export function lagSeconds(sinceRequestMs, currentTime) {
    const lag = sinceRequestMs / 1000 - currentTime;
    return Math.max(0, Math.min(MAX_BUFFER_S, Math.round(lag)));
}

// A stream routed through Web Audio is silent while its AudioContext is
// suspended, or "interrupted" on iOS during a call or a navigation prompt,
// yet playback time keeps advancing, so the watchdog cannot see it. Resume
// the context whenever it leaves "running" while playback is wanted.
// Returns the check, for calling on a play as well.
export function keepAudioRunning(context, wanted) {
    const resume = () => {
        if (!wanted() || context.state === 'running' || context.state === 'closed') {
            return;
        }
        const resumed = context.resume();
        if (resumed && resumed.catch) {
            // Refused until the next user gesture; the next state change or play tries again.
            resumed.catch(() => {});
        }
    };
    context.onstatechange = resume;
    return resume;
}
