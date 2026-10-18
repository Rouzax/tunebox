// The event list behind the ?debug=1 panel: what the browser reports about
// the stream, for devices where no developer console can be opened.
export const DEBUG_LINES = 400;

export function createDebugLog(now, limit = DEBUG_LINES) {
    let origin = now();
    let lines = [];
    const listeners = [];
    return {
        // Times count from the latest play.
        mark() {
            origin = now();
        },
        add(text) {
            const line = `${((now() - origin) / 1000).toFixed(1)} ${text}`;
            lines.push(line);
            if (lines.length > limit) {
                lines.shift();
            }
            for (const listener of listeners) {
                listener(line);
            }
        },
        onLine(listener) {
            listeners.push(listener);
        },
        clear() {
            lines = [];
        },
        text: () => lines.join('\n'),
    };
}

export function describeMedia(el) {
    const buffered = el.buffered && el.buffered.length ? el.buffered.end(el.buffered.length - 1) : 0;
    return `t=${el.currentTime.toFixed(1)} buffered=${buffered.toFixed(1)} ready=${el.readyState} network=${el.networkState} ${el.paused ? 'paused' : 'playing'}`;
}

// Firefox and Chrome put the decoder's or network's own reason in message.
export function describeError(error) {
    if (!error) {
        return 'no error object';
    }
    return error.message ? `code=${error.code} ${error.message}` : `code=${error.code}`;
}
