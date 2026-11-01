// Health: whether each enabled station's streams and now-playing feed work,
// from the daily check and the live poller. Station and stream names come
// from the configuration and go in as text only (dom.js).
import {button, el, icon} from './dom.js';
import {duration} from './usage.js';

const POLL_MS = 3000;
const FEED_NAME = 'Now-playing feed';
const STATUS = {broken: 'Broken', warning: 'Warning', ok: 'OK'};
const UNAVAILABLE = 'tunebox cannot read its health results right now.';
const STOPPED = 'The health check is not running; restart tunebox.';

// A station reason; name is the stream it is about.
const REASONS = {
    streams_failing: () => 'No stream works',
    stream_failing: (name) => `${name} does not work`,
    plain_http: (name) => `${name} plays over plain http, which browsers block on the player's https page`,
    feed_failing: () => 'The now-playing feed does not answer',
    feed_check_failed_polls_ok: () => 'The daily feed check failed, but the feed has answered since',
    no_songs: () => 'The feed answers but lists no songs',
    poll_failing: () => 'The now-playing feed has failed for more than 10 minutes',
    stuck: () => 'No new song for more than 2 hours',
};

// Why one stream or feed failed, as the end of "<name> ...".
const ITEM_REASONS = {
    unreachable: 'cannot be reached',
    timeout: 'took too long',
    connect_failed: 'the connection failed',
    hls: 'is an HLS stream',
    playlist: 'is a playlist, not a stream',
    no_audio: 'sent no AAC or MP3 audio',
    icy_plain_http: 'is an old SHOUTcast server (plain http only)',
    unreadable: 'answered something tunebox cannot read',
    feed_error: 'answered something tunebox cannot read',
};

function itemReason(code) {
    const status = /^http_([0-9]+)$/.exec(code || '');
    if (status) {
        return `answered HTTP ${status[1]}`;
    }
    return ITEM_REASONS[code] || code;
}

function capital(text) {
    return text.charAt(0).toUpperCase() + text.slice(1);
}

// item is the station's item the reason names, or null.
// Each item line already says "Not checked yet", so the station's list
// leaves those reasons out rather than repeating them once per item.
export function shownReasons(reasons) {
    return reasons.filter((reason) => reason.code !== 'not_checked');
}

export function reasonText(reason, item) {
    const text = REASONS[reason.code];
    return text ? text(item ? item.name : reason.item) : reason.code;
}

// Times are the server's ms; nowMs is the browser's clock.
export function itemLine(item, nowMs) {
    if (item.ok === null || item.ok === undefined) {
        return 'Not checked yet';
    }
    if (item.ok) {
        if (item.kind === 'feed') {
            return 'Answers';
        }
        if (!item.codec) {
            return 'Works';
        }
        const codec = item.codec.toUpperCase();
        return item.kbps ? `${codec} ${item.kbps} kbps` : codec;
    }
    const text = capital(String(itemReason(item.reason)));
    if (item.failing_since === null || item.failing_since === undefined) {
        return text;
    }
    return `${text}, failing for ${duration((nowMs - item.failing_since) / 1000)}`;
}

// progress is null between a press and the start of its run.
export function progressText(progress) {
    return progress && progress.total ? `Checking ${progress.done} of ${progress.total}` : 'Checking';
}

export function retryText(seconds) {
    return `You can check again in ${Math.max(1, Math.ceil(seconds / 60))} min.`;
}

export function runLine(run, nowMs) {
    if (run.last_finished_at === null || run.last_finished_at === undefined) {
        return 'Not checked yet. The first check runs 5 minutes after tunebox starts, then daily at 04:30.';
    }
    const ago = duration((nowMs - run.last_finished_at) / 1000);
    // Whether and when a retry comes depends on the retries left and the
    // next daily run, which the page does not know.
    if (run.outcome === 'network_down') {
        return `Last check ${ago} ago could not reach the internet; tunebox tries again later.`;
    }
    if (run.outcome === 'error') {
        return `Last check ${ago} ago stopped on an error in tunebox; see the log.`;
    }
    return `Last check ${ago} ago: ${run.items} items in ${duration(run.duration_ms / 1000)}.`;
}

export function menuText(count) {
    return count.broken > 0 ? `Health (${count.broken})` : 'Health';
}

function itemName(item) {
    return item.kind === 'feed' ? FEED_NAME : item.name;
}

// counts is null when tunebox cannot count; a station missing from it has
// no song without an image.
function coversLine(counts, id) {
    if (counts === null || counts === undefined) {
        return 'No image: unknown';
    }
    const songs = counts[id] || 0;
    return `No image: ${songs} ${songs === 1 ? 'song' : 'songs'} in 30 days`;
}

function stationNode(station, counts, nowMs) {
    const items = new Map(station.items.map((item) => [item.item, item]));
    const last = station.live.last_change;
    const reasons = shownReasons(station.reasons);
    return el('li', {class: `health-station ${station.status}`},
        el('div', {class: 'health-head'},
            el('strong', null, station.name),
            el('span', {class: 'health-status'},
                icon(station.status === 'ok' ? 'check_circle' : 'warning'), ' ', STATUS[station.status] || station.status)),
        reasons.length
            ? el('ul', {class: 'health-reasons'}, reasons.map((reason) => el('li', null,
                reasonText(reason, reason.item ? items.get(reason.item) || null : null))))
            : null,
        el('ul', {class: 'health-items hint'}, station.items.map((item) => el('li', null,
            `${itemName(item)}: ${itemLine(item, nowMs)}`))),
        last === null || last === undefined
            ? null
            : el('span', {class: 'hint'}, `Last song change ${duration((nowMs - last) / 1000)} ago`),
        el('span', {class: 'hint'}, coversLine(counts, station.id)));
}

function unreachable(ctx) {
    return () => ctx.message('Could not reach tunebox.');
}

export function render(root, ctx) {
    const current = ctx.stillOpen;
    const run = el('p', {class: 'hint'});
    const progress = el('p', {class: 'health-progress', hidden: true});
    const notice = el('p', {class: 'warning', hidden: true});
    const list = el('ul', {class: 'health-list'});
    const check = button('radio', 'Check all now', () => press().catch(unreachable(ctx)));
    const section = el('section', {class: 'health'},
        el('h2', null, 'Health'), run, progress, el('div', {class: 'actions'}, check), notice, list);
    root.replaceChildren(section);
    let stopped = false;

    function open() {
        return current() && section.isConnected;
    }

    function say(text) {
        notice.replaceChildren(icon('warning'), ` ${text}`);
        notice.hidden = false;
    }

    // The two 503 refusals have their own text; true when data was one.
    function refused(data) {
        if (data.error === 'health_unavailable') {
            say(UNAVAILABLE);
            return true;
        }
        if (data.error === 'health_stopped') {
            stopped = true;
            check.disabled = true;
            progress.hidden = true;
            say(STOPPED);
            return true;
        }
        return false;
    }

    function running(state) {
        progress.hidden = !state;
        check.disabled = state || stopped;
    }

    // A newer read wins: an older answer that arrives late is dropped.
    let serial = 0;
    async function load() {
        serial += 1;
        const mine = serial;
        const [page, covers] = await Promise.all([ctx.request('GET', 'health'), ctx.request('GET', 'health/covers')]);
        if (page.status === 401 || covers.status === 401 || !open() || mine !== serial) {
            return;
        }
        if (!page.ok) {
            list.replaceChildren();
            if (!refused(page.data)) {
                ctx.fail(page.data);
            }
            return;
        }
        if (!covers.ok) {
            ctx.fail(covers.data);
        }
        const counts = covers.ok ? covers.data.counts : null;
        const data = page.data;
        const now = Date.now();
        notice.hidden = true;
        run.textContent = runLine(data.run, now);
        list.replaceChildren(...data.stations.map((station) => stationNode(station, counts, now)));
        if (data.progress) {
            progress.textContent = progressText(data.progress);
            running(true);
            poll.start();
        }
    }

    async function press() {
        const answer = await ctx.request('POST', 'health/check', {});
        if (answer.status === 401 || !open()) {
            return;
        }
        if (answer.status === 202 || answer.status === 409) {
            // 409: a run is already going, which the poll follows as well.
            notice.hidden = true;
            progress.textContent = progressText(null);
            running(true);
            poll.start();
        } else if (answer.status === 429) {
            say(retryText(answer.data.retry_s));
        } else if (!refused(answer.data)) {
            ctx.fail(answer.data);
        }
    }

    // Follows a run every 3 s while the page is on screen and the tab is
    // visible; a tab shown again asks at once. It stops for good once the
    // page is left, the panel logs out (the view is emptied), the session
    // ends or the run is over, which redraws the page and the menu.
    const poll = (() => {
        let timer = null;
        let listening = false;
        let busy = false;
        function pause() {
            if (timer !== null) {
                clearInterval(timer);
                timer = null;
            }
        }
        function quit() {
            pause();
            if (listening) {
                document.removeEventListener('visibilitychange', changed);
                listening = false;
            }
        }
        function gone() {
            if (open()) {
                return false;
            }
            quit();
            return true;
        }
        async function ask() {
            const answer = await ctx.request('GET', 'health/count');
            if (answer.status === 401) {
                quit();
                return;
            }
            if (gone()) {
                return;
            }
            if (!answer.ok) {
                quit();
                running(false);
                ctx.fail(answer.data);
                return;
            }
            const data = answer.data;
            if (data.stopped) {
                quit();
                refused({error: 'health_stopped'});
                return;
            }
            if (data.running) {
                progress.textContent = progressText(data.progress);
                return;
            }
            quit();
            running(false);
            ctx.healthChanged();
            await load();
        }
        function tick() {
            if (gone() || busy) {
                return;
            }
            busy = true;
            ask().catch(unreachable(ctx)).finally(() => {
                busy = false;
            });
        }
        function resume() {
            pause();
            if (!document.hidden) {
                timer = setInterval(tick, POLL_MS);
            }
        }
        function changed() {
            if (gone()) {
                return;
            }
            if (document.hidden) {
                pause();
            } else {
                tick();
                resume();
            }
        }
        function start() {
            if (!listening) {
                document.addEventListener('visibilitychange', changed);
                listening = true;
            }
            resume();
        }
        return {start};
    })();

    load().catch(unreachable(ctx));
}
