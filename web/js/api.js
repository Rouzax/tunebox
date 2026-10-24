// The tunebox API and the /now poller.
export const POLL_MS = 10000;

function failed(path, status) {
    const error = new Error(`${path}: ${status}`);
    error.status = status;
    return error;
}

export function createApi(fetchFn) {
    async function get(path) {
        const response = await fetchFn(path, {headers: {}, cache: 'no-store'});
        if (!response.ok) {
            throw failed(path, response.status);
        }
        return response;
    }

    return {
        // version: the configuration version; /now reports it too, and a
        // different one there means this list is out of date.
        // pin: the listening PIN's revision; /now reports it too, and a
        // different one there means this list is out of date.
        // images: the logo and default image revision; /now reports it too,
        // and a different one there means the addresses in this list are out
        // of date.
        // more: stations wait behind the listening PIN ("chosen stations").
        async channels() {
            const response = await get('/api/channels');
            return {
                channels: await response.json(),
                version: Number(response.headers.get('X-Tunebox-Version')) || 0,
                more: response.headers.get('X-Tunebox-Pin') === 'more',
                pin: response.headers.get('X-Tunebox-Pin-Revision') || '',
                images: response.headers.get('X-Tunebox-Images') || '',
            };
        },
        // The listening PIN: 204 when accepted (the answer sets the grant
        // cookie), 401 when wrong, 429 when this address must wait
        // (error "slow_down") or the server is busy (error "busy").
        async pin(code) {
            const response = await fetchFn('/api/pin', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({pin: code}),
                cache: 'no-store',
            });
            const data = response.status === 204 ? {} : await response.json().catch(() => ({}));
            return {status: response.status, error: data.error || null};
        },
        player: async () => (await get('/api/player')).json(),
        // stream: the name of the stream playing, so tunebox can add the
        // audio it sent on connect; null when nothing plays.
        async now(channelId, bufferS, etag, stream) {
            const query = `buffer_s=${bufferS}${stream === null || stream === undefined ? '' : `&stream=${encodeURIComponent(stream)}`}`;
            const path = `/api/channels/${encodeURIComponent(channelId)}/now?${query}`;
            const headers = etag ? {'If-None-Match': etag} : {};
            const response = await fetchFn(path, {headers, cache: 'no-store'});
            const date = response.headers.get('Date');
            if (response.status === 304) {
                return {status: 304, date};
            }
            if (!response.ok) {
                throw failed(path, response.status);
            }
            return {status: 200, body: await response.json(), etag: response.headers.get('ETag'), date};
        },
    };
}

// One poll at a time per channel. Every start, stop or refresh opens a new
// generation, so an answer still on its way for an earlier one is dropped.
// onReached: any answer, 304 included; onMissing: a 404 or 400, the
// channel or stream is no longer there; onError: no answer at all.
export function createPoller({api, getBufferS, getStream, onBody, onReached, onMissing, onDate, onError, setTimer, clearTimer}) {
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
            const answer = await api.now(channelId, getBufferS(), etag, getStream());
            if (own !== generation) {
                return;
            }
            onReached();
            if (answer.date) {
                onDate(answer.date);
            }
            if (answer.status === 200) {
                etag = answer.etag;
                onBody(answer.body);
            }
        } catch (error) {
            if (own === generation) {
                if (error.status === 404 || error.status === 400) {
                    onReached();
                    onMissing();
                } else {
                    onError(error);
                }
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
