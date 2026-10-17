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

// How far behind the downloaded edge playback runs. After the connect burst
// that edge is the live edge, so this is how late the listener hears.
export function bufferSeconds(buffered, currentTime) {
    if (!buffered || !buffered.length) {
        return 0;
    }
    const ahead = buffered.end(buffered.length - 1) - currentTime;
    return Math.max(0, Math.min(MAX_BUFFER_S, Math.round(ahead)));
}
