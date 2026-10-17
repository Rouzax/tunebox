// The tunebox API and the /now poller.
export const POLL_MS = 10000;

export function createApi(fetchFn) {
    async function json(path) {
        const response = await fetchFn(path, {headers: {}, cache: 'no-store'});
        if (!response.ok) {
            throw new Error(`${path}: ${response.status}`);
        }
        return response.json();
    }

    return {
        channels: () => json('/api/channels'),
        player: () => json('/api/player'),
        async now(channelId, bufferS, etag) {
            const path = `/api/channels/${encodeURIComponent(channelId)}/now?buffer_s=${bufferS}`;
            const headers = etag ? {'If-None-Match': etag} : {};
            const response = await fetchFn(path, {headers, cache: 'no-store'});
            const date = response.headers.get('Date');
            if (response.status === 304) {
                return {status: 304, date};
            }
            if (!response.ok) {
                throw new Error(`${path}: ${response.status}`);
            }
            return {status: 200, body: await response.json(), etag: response.headers.get('ETag'), date};
        },
    };
}

// One poll at a time per channel. Every start, stop or refresh opens a new
// generation, so an answer still on its way for an earlier one is dropped.
export function createPoller({api, getBufferS, onBody, onDate, onError, setTimer, clearTimer}) {
    let channelId = null;
    let etag = null;
    let generation = 0;
    let timer = null;
    let running = false;

    function cancel() {
        if (timer !== null) {
            clearTimer(timer);
            timer = null;
        }
    }

    async function poll(own) {
        timer = null;
        try {
            const answer = await api.now(channelId, getBufferS(), etag);
            if (own !== generation) {
                return;
            }
            if (answer.date) {
                onDate(answer.date);
            }
            if (answer.status === 200) {
                etag = answer.etag;
                onBody(answer.body);
            }
        } catch (error) {
            if (own === generation) {
                onError(error);
            }
        }
        if (own === generation && running) {
            timer = setTimer(() => poll(own), POLL_MS);
        }
    }

    return {
        start(id) {
            cancel();
            channelId = id;
            etag = null;
            running = true;
            generation += 1;
            poll(generation);
        },
        stop() {
            cancel();
            running = false;
            generation += 1;
        },
        refresh() {
            if (!running) {
                return;
            }
            cancel();
            generation += 1;
            poll(generation);
        },
    };
}
