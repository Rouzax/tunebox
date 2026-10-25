// Usage: who listens now, listening over time, and the devices that played.
// Names and agents come from players, so they go in as text only (dom.js).
import {button, el, field, icon, when} from './dom.js';
import {tickets} from './state.js';

// usage/device answers this many sessions per page; a full page may have more.
const PAGE = 50;
const LIVE_EVERY_MS = 10000;
const RANGES = [['7d', '7 days'], ['30d', '30 days'], ['12m', '12 months'], ['all', 'All']];

// A station deleted since it was played shows by its id.
export function stationName(channels, id) {
    const row = channels.find((channel) => channel.id === id);
    return row && row.doc.name ? row.doc.name : id;
}

export function duration(seconds) {
    const whole = Math.max(0, Math.round(seconds));
    if (whole < 60) {
        return `${whole} s`;
    }
    const minutes = Math.round(whole / 60);
    if (minutes < 60) {
        return `${minutes} min`;
    }
    const rest = minutes % 60;
    return rest ? `${Math.floor(minutes / 60)} h ${rest} min` : `${Math.floor(minutes / 60)} h`;
}

// Each bucket with its share of the largest one, 0 to 1; all zero when
// nothing was heard. key is 'seconds' (the default) or 'visits'.
export function bars(buckets, by) {
    const key = by || 'seconds';
    const largest = Math.max(0, ...buckets.map((bucket) => bucket[key]));
    return buckets.map((bucket) => ({
        label: bucket.label,
        seconds: bucket.seconds,
        visits: bucket.visits,
        share: largest > 0 ? bucket[key] / largest : 0,
    }));
}

// nowS is the browser's clock, which may run behind the server's.
export function endedLine(last, channels, nowS) {
    return `${last.label} · ${stationName(channels, last.channel)} · ended ${duration(nowS - last.ended_at)} ago`;
}

function unavailable() {
    return el('p', {class: 'warning'}, icon('warning'), ' tunebox cannot read its usage data right now.');
}

// Reads path for the part drawn in box. Resolves the data; null when the
// part shows the warning instead, the refusal went to ctx.fail, or the
// answer is no longer wanted (the page was left, the panel logged out, a
// newer read was asked); false when the session ended (the login shows).
async function load(ctx, box, path, wanted) {
    const current = ctx.stillOpen;
    const answer = await ctx.request('GET', path);
    if (answer.status === 401) {
        return false;
    }
    if (!current() || !box.isConnected || (wanted && !wanted())) {
        return null;
    }
    if (answer.status === 503 && answer.data.error === 'usage_unavailable') {
        box.replaceChildren(unavailable());
        return null;
    }
    if (!answer.ok) {
        ctx.fail(answer.data);
        return null;
    }
    return answer.data;
}

function unreachable(ctx) {
    return () => ctx.message('Could not reach tunebox.');
}

function nowPart(ctx) {
    const box = el('div', {class: 'usage-live'});
    const section = el('section', {class: 'usage-part'}, el('h2', null, icon('radio'), ' Listening now'), box);
    let busy = false;

    function draw(data) {
        const channels = ctx.config.state.channels;
        const list = el('ul', {class: 'usage-now'});
        if (data.sessions.length) {
            for (const session of data.sessions) {
                list.append(el('li', null,
                    el('span', {class: 'name'}, session.label),
                    el('span', null, stationName(channels, session.channel)),
                    el('span', {class: 'id'}, session.stream),
                    el('span', {class: 'when'}, duration(session.seconds)),
                    session.address ? el('span', {class: 'id'}, session.address) : null));
            }
        } else if (data.last_ended) {
            list.append(el('li', {class: 'hint'}, endedLine(data.last_ended, channels, Date.now() / 1000)));
        } else {
            list.append(el('li', {class: 'hint'}, 'Nobody has listened yet'));
        }
        box.replaceChildren(
            ...(data.saved === false ? [el('p', {class: 'warning'}, icon('warning'), ' Usage data is not being saved.')] : []),
            list);
    }

    // Resolves false once the session has ended, so the refresh can stop.
    async function refresh() {
        if (busy) {
            return true;
        }
        busy = true;
        try {
            const data = await load(ctx, box, 'usage/live');
            if (data) {
                draw(data);
            }
            return data !== false;
        } finally {
            busy = false;
        }
    }
    return {section, refresh};
}

// Refreshes the live part every 10 s while it is on screen and the tab is
// visible; a tab shown again refreshes at once. Everything stops for good
// once the page is left, the panel logs out (the view is emptied) or the
// session ends.
function keepLive(ctx, now) {
    const current = ctx.stillOpen;
    let timer = null;
    function stop() {
        if (timer !== null) {
            clearInterval(timer);
            timer = null;
        }
    }
    function gone() {
        if (current() && now.section.isConnected) {
            return false;
        }
        stop();
        document.removeEventListener('visibilitychange', changed);
        return true;
    }
    function tick() {
        if (!gone()) {
            now.refresh().then((going) => {
                if (!going) {
                    stop();
                    document.removeEventListener('visibilitychange', changed);
                }
            }, unreachable(ctx));
        }
    }
    function start() {
        stop();
        if (!document.hidden) {
            timer = setInterval(tick, LIVE_EVERY_MS);
        }
    }
    function changed() {
        if (gone()) {
            return;
        }
        if (document.hidden) {
            stop();
        } else {
            tick();
            start();
        }
    }
    document.addEventListener('visibilitychange', changed);
    start();
}

function chart(title, buckets, key, format) {
    const total = buckets.reduce((sum, bucket) => sum + bucket[key], 0);
    const plot = el('div', {class: 'bars', role: 'img', 'aria-label': `${title}: ${format(total)} in total`});
    for (const bar of bars(buckets, key)) {
        const node = el('div', {class: 'bar', title: `${bar.label}: ${format(bar[key])}`});
        // The CSP refuses style attributes; a property set from script is allowed.
        node.style.height = `${bar.share * 100}%`;
        plot.append(node);
    }
    return el('div', {class: 'chart'},
        el('h3', null, title, ' ', el('span', {class: 'when'}, `${format(total)} in total`)),
        plot,
        buckets.length ? el('div', {class: 'axis'},
            el('span', null, buckets[0].label),
            el('span', null, buckets[buckets.length - 1].label)) : null);
}

function topList(title, rows, name) {
    return el('div', {class: 'top'},
        el('h3', null, title),
        rows.length
            ? el('ol', {class: 'usage-top'}, rows.map(([key, seconds]) => el('li', null,
                el('span', {class: 'name'}, name(key)),
                el('span', {class: 'when'}, duration(seconds)))))
            : el('p', {class: 'hint'}, 'Nothing yet.'));
}

function visits(count) {
    return count === 1 ? '1 visit' : `${count} visits`;
}

function overTimePart(ctx) {
    const box = el('div', {class: 'usage-summary'});
    const take = tickets();
    const choices = RANGES.map(([range, label]) => el('button', {
        type: 'button', class: 'quiet', 'data-range': range, 'aria-pressed': 'false',
        onclick: () => show(range).catch(unreachable(ctx)),
    }, label));

    function draw(data) {
        const channels = ctx.config.state.channels;
        const own = (key) => key;
        box.replaceChildren(
            el('p', {class: 'hint'}, `Days are in ${data.zone} time.`),
            chart('Listening', data.buckets, 'seconds', duration),
            chart('Visits', data.buckets, 'visits', visits),
            data.peak.day ? el('p', null, `Most listeners at once: ${data.peak.value} on ${data.peak.day}`) : null,
            el('div', {class: 'tops'},
                topList('Stations', data.channels, (id) => stationName(channels, id)),
                topList('Streams', data.streams, own),
                topList('Browsers and systems', data.agents, own)));
    }

    async function show(range) {
        for (const choice of choices) {
            choice.setAttribute('aria-pressed', String(choice.dataset.range === range));
        }
        const data = await load(ctx, box, `usage/summary?range=${range}`, take.take());
        if (data) {
            draw(data);
        }
    }
    const section = el('section', {class: 'usage-part'},
        el('h2', null, icon('history'), ' Over time'),
        el('div', {class: 'actions ranges'}, choices),
        box);
    return {section, show};
}

function sessionsTable(rows, channels) {
    const addresses = rows.some((row) => row.address);
    return el('div', {class: 'scroll'}, el('table', {class: 'usage-table'},
        el('thead', null, el('tr', null,
            el('th', null, 'Station'), el('th', null, 'Stream'), el('th', null, 'Started'), el('th', null, 'Duration'),
            addresses ? el('th', null, 'Address') : null)),
        el('tbody', null, rows.map((row) => el('tr', null,
            el('td', null, stationName(channels, row.channel)),
            el('td', null, row.stream),
            el('td', null, when(row.started_at)),
            el('td', null, duration(row.seconds)),
            addresses ? el('td', null, row.address || '') : null)))));
}

function devicesPart(ctx, changed) {
    const box = el('div');
    const detail = el('div', {class: 'device'});
    const section = el('section', {class: 'usage-part'}, el('h2', null, icon('devices'), ' Devices'), box, detail);
    const opening = tickets();

    async function refresh() {
        const data = await load(ctx, box, 'usage/devices');
        if (!data) {
            return;
        }
        const table = data.devices.length ? el('div', {class: 'scroll'}, el('table', {class: 'usage-table devices'},
            el('thead', null, el('tr', null,
                el('th', null, 'Name'), el('th', null, 'Browser and system'), el('th', null, 'First seen'),
                el('th', null, 'Last seen'), el('th', null, 'Last 30 days'))),
            el('tbody', null, data.devices.map((device) => el('tr', null,
                el('td', null, el('button', {
                    type: 'button', class: 'link open-device',
                    onclick: () => open(device.id).catch(unreachable(ctx)),
                }, device.name || 'Unnamed device')),
                el('td', null, device.agent),
                el('td', null, when(device.first_seen)),
                el('td', null, when(device.last_seen)),
                el('td', null, duration(device.seconds_30d)))))))
            : el('p', {class: 'hint'}, 'No device has played yet.');
        box.replaceChildren(table, el('p', {class: 'hint'}, `Visitors that never played: ${data.never_played}`));
    }

    function close() {
        opening.take();
        detail.replaceChildren();
    }

    async function afterChange(text) {
        ctx.message(text);
        changed();
        await refresh();
    }

    async function open(id) {
        const mine = opening.take();
        const path = `usage/device?id=${encodeURIComponent(id)}`;
        const data = await load(ctx, detail, path, mine);
        if (!data) {
            return;
        }
        const current = ctx.stillOpen;
        const channels = ctx.config.state.channels;
        const rows = data.sessions.slice();
        const sessions = el('div', null, rows.length ? sessionsTable(rows, channels) : el('p', {class: 'hint'}, 'No listening stored.'));
        const older = button('history', 'Older', () => more().catch(unreachable(ctx)), {class: 'quiet', hidden: data.sessions.length < PAGE});
        async function more() {
            const page = await load(ctx, detail, `${path}&before=${rows[rows.length - 1].started_at}`, mine);
            if (!page) {
                return;
            }
            rows.push(...page.sessions);
            sessions.replaceChildren(sessionsTable(rows, channels));
            older.hidden = page.sessions.length < PAGE;
        }

        const name = el('input', {name: 'name', maxlength: '40', placeholder: 'Unnamed device', value: data.name || ''});
        const rename = el('form', {class: 'rename-device'},
            field('Name', name, 'Up to 40 characters; empty clears the name.'),
            el('div', {class: 'actions'}, el('button', {type: 'submit'}, icon('edit'), ' Save the name')));
        rename.addEventListener('submit', (event) => {
            event.preventDefault();
            (async () => {
                const answer = await ctx.request('POST', 'usage/device/name', {id, name: name.value});
                if (!current() || answer.status === 401) {
                    return;
                }
                if (answer.ok) {
                    await afterChange('Name saved.');
                    await open(id);
                } else {
                    ctx.fail(answer.data);
                }
            })().catch(unreachable(ctx));
        });

        const question = el('div', {class: 'confirm'});
        const remove = button('delete', 'Delete this device', () => {
            const keep = button('close', 'Keep it', () => question.replaceChildren(), {class: 'quiet'});
            question.replaceChildren(
                el('span', null, 'Delete this device and its listening history? The daily totals stay.'),
                button('delete', 'Delete', () => erase().catch(unreachable(ctx)), {class: 'danger confirm-delete'}),
                keep);
            keep.focus();
        }, {class: 'danger'});
        async function erase() {
            const answer = await ctx.request('POST', 'usage/device/delete', {id});
            if (!current() || answer.status === 401) {
                return;
            }
            if (answer.ok) {
                close();
                await afterChange('Device deleted.');
            } else {
                ctx.fail(answer.data);
            }
        }

        detail.replaceChildren(
            el('h3', null, data.name || 'Unnamed device'),
            el('p', {class: 'hint'}, `${data.agent} · first seen ${when(data.first_seen)} · last seen ${when(data.last_seen)}`),
            rename,
            el('h3', null, 'Most played, last 30 days'),
            data.top.length
                ? el('ol', {class: 'usage-top'}, data.top.map(([channel, seconds]) => el('li', null,
                    el('span', {class: 'name'}, stationName(channels, channel)),
                    el('span', {class: 'when'}, duration(seconds)))))
                : el('p', {class: 'hint'}, 'Nothing in the last 30 days.'),
            el('h3', null, 'Sessions'),
            sessions,
            older,
            el('div', {class: 'actions'}, remove, button('close', 'Close', close, {class: 'quiet'})),
            question);
    }
    return {section, refresh};
}

export function render(root, ctx) {
    const now = nowPart(ctx);
    const over = overTimePart(ctx);
    const devices = devicesPart(ctx, () => now.refresh().catch(unreachable(ctx)));
    root.replaceChildren(now.section, over.section, devices.section);
    now.refresh().catch(unreachable(ctx));
    over.show('7d').catch(unreachable(ctx));
    devices.refresh().catch(unreachable(ctx));
    keepLive(ctx, now);
}
