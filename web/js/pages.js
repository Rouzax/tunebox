import {groupChannels, mosaicFor, openGroupAfter, streamLabel, tilesFor} from './channels.js';
import {coverFor, nextCover, timeAgo} from './tracks.js';

export const PAGES = ['main', 'history', 'channels', 'viz'];
const HISTORY_SHOWN = 20;
const AGO_REFRESH_MS = 30000;

// replaceChildren() is too new for older car browsers (Chrome 86).
function replaceContent(el, children) {
    el.textContent = '';
    el.append(...children);
}

function element(tag, className, text) {
    const el = document.createElement(tag);
    if (className) {
        el.className = className;
    }
    if (text !== undefined) {
        el.textContent = text;
    }
    return el;
}

// Sliding pages: now playing, history, channels and the visualizer menu.
export function bindPages(app) {
    const {root, events} = app;
    const historyList = root.querySelector('.history-list');
    const historyEmpty = root.querySelector('.history-empty');
    const groups = root.querySelector('.channel-groups');
    const toTiles = root.querySelector('.btn-tiles');
    let page = 'main';
    // The group whose stations the channel page shows instead of the tiles.
    let openGroup = null;

    // Focus moving to a button on another page (keyboard, screen reader)
    // makes the browser scroll this overflow: hidden container; the pages
    // are placed by transform only, so any scroll here is undone.
    const container = root.querySelector('.container');
    container.addEventListener('scroll', () => {
        container.scrollLeft = 0;
        container.scrollTop = 0;
    });

    app.page = () => page;
    app.showPage = (name) => {
        page = PAGES.indexOf(name) >= 0 ? name : 'main';
        root.dataset.page = page;
        if (page === 'history') {
            renderHistory();
        } else if (page === 'channels') {
            if (openGroup !== null) {
                openGroup = null;
                renderChannels();
            }
            loadLogos();
            markActive();
            centreActive();
        }
        events.emit('page', page);
    };
    const toggle = (name) => () => app.showPage(page === name ? 'main' : name);
    root.querySelector('.open-history').addEventListener('click', toggle('history'));
    root.querySelector('.open-channels').addEventListener('click', toggle('channels'));
    for (const back of root.querySelectorAll('.btn-back')) {
        back.addEventListener('click', () => app.showPage('main'));
    }
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape' || event.defaultPrevented) {
            return;
        }
        if (page === 'channels' && openGroup !== null) {
            showGroup(null);
        } else {
            app.showPage('main');
        }
    });

    function renderHistory() {
        const now = Date.now() + app.clockOffset;
        const items = app.history.slice(0, HISTORY_SHOWN).map((track) => {
            const item = element('li');
            const image = element('img');
            image.alt = '';
            image.loading = 'lazy';
            image.src = coverFor(track, app.channel);
            const channel = app.channel;
            image.addEventListener('error', () => {
                const next = nextCover(image.getAttribute('src'), channel);
                if (next) {
                    image.src = next;
                }
            });
            const what = element('div', 'what');
            const title = element('div', 'title', track.title);
            if (track.badge) {
                title.prepend(element('span', 'badge', track.badge));
            }
            what.append(title, element('div', 'artist', track.artist));
            item.append(image, what, element('span', 'ago', timeAgo(track.started_at, now)));
            return item;
        });
        replaceContent(historyList, items);
        historyEmpty.classList.toggle('hidden', items.length > 0);
    }
    events.on('history', () => {
        if (page === 'history') {
            renderHistory();
        }
    });
    window.setInterval(() => {
        if (page === 'history') {
            renderHistory();
        }
    }, AGO_REFRESH_MS);

    function stationRows(channels) {
        const list = element('ul');
        for (const channel of channels) {
            const item = element('li');
            const button = element('button', 'channel');
            button.type = 'button';
            button.dataset.id = channel.id;
            if (channel.logo) {
                const logo = element('img');
                logo.alt = '';
                logo.dataset.src = channel.logo_small || channel.logo;
                button.append(logo);
            }
            button.append(element('span', 'name', channel.name));
            item.append(button);
            list.append(item);
        }
        return list;
    }

    function mosaic(sources, lazy) {
        const box = element('span', 'mosaic');
        for (const source of sources) {
            const image = element('img');
            image.alt = '';
            if (lazy) {
                image.dataset.src = source;
            } else {
                image.src = source;
            }
            box.append(image);
        }
        return box;
    }

    function tileButton(tile) {
        const button = element('button', 'tile');
        button.type = 'button';
        const art = element('span', 'tile-art');
        if (tile.kind === 'group') {
            button.dataset.group = tile.name;
        } else {
            button.dataset.id = tile.id;
        }
        if (tile.image) {
            const image = element('img');
            image.alt = '';
            image.dataset.src = tile.image;
            art.append(image);
        } else if (tile.mosaic && tile.mosaic.length) {
            art.append(mosaic(tile.mosaic, true));
        }
        const playing = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        playing.setAttribute('class', 'tile-playing');
        playing.setAttribute('viewBox', '0 0 24 24');
        playing.setAttribute('aria-hidden', 'true');
        const bars = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        bars.setAttribute('d', 'M7 18h2V6H7v12zm4 4h2V2h-2v20zm-8-8h2v-4H3v4zm12 4h2V6h-2v12zm4-8v4h2v-4h-2z');
        playing.append(bars);
        button.append(art, playing, element('span', 'name', tile.name));
        return button;
    }

    // Three views: the plain list (fewer than two groups), the tiles, or the
    // stations of one open group. A group's rows are built only when it
    // opens, so the tile page loads tile and mosaic images alone.
    function renderChannels() {
        const tiles = tilesFor(app.channels);
        const listed = groupChannels(app.channels);
        openGroup = openGroupAfter(app.channels, openGroup);
        const open = openGroup !== null && listed.find((group) => group.name === openGroup);
        let content;
        if (!tiles) {
            content = listed.map((group) => {
                const section = element('section');
                if (group.name) {
                    section.append(element('h4', '', group.name));
                }
                section.append(stationRows(group.channels));
                return section;
            });
        } else if (open) {
            const section = element('section');
            section.append(element('h4', '', open.name), stationRows(open.channels));
            content = [section];
        } else {
            const grid = element('div', 'channel-tiles');
            grid.append(...tiles.map(tileButton));
            content = [grid];
        }
        if (app.morePin) {
            const more = element('button', 'btn more-stations', 'More stations with the PIN');
            more.type = 'button';
            content.push(more);
        }
        toTiles.classList.toggle('hidden', !open);
        groups.classList.toggle('tiled', Boolean(tiles) && !open);
        replaceContent(groups, content);
    }

    // Opens a group's stations, or the tiles again with null. Back on the
    // tiles, the group just left is centred and, when focus was on the
    // page, focused: the re-render removed the pressed control.
    function showGroup(name) {
        const left = openGroup;
        const hadFocus = root.contains(document.activeElement) && document.activeElement !== document.body;
        openGroup = name;
        renderChannels();
        loadLogos();
        markActive();
        const pageBox = groups.closest('.page');
        pageBox.scrollTop = 0;
        const leftTile = openGroup === null
            ? [...groups.querySelectorAll('.tile[data-group]')].find((tile) => tile.dataset.group === left)
            : null;
        centreActive(leftTile);
        const target = openGroup !== null ? toTiles : leftTile;
        if (hadFocus && target) {
            target.focus({preventScroll: true});
        }
    }

    // A tile image that does not load gives way to the group's mosaic, a
    // mosaic piece to the others, and an empty mosaic to the name alone.
    groups.addEventListener('error', (event) => {
        const image = event.target;
        if (!image || image.tagName !== 'IMG' || !image.closest('.tile')) {
            return;
        }
        const art = image.parentElement;
        image.remove();
        const tile = art.closest('.tile');
        if (art.classList.contains('tile-art') && tile.dataset.group !== undefined) {
            const group = groupChannels(app.channels).find((candidate) => candidate.name === tile.dataset.group);
            const sources = group ? mosaicFor(group.channels) : [];
            if (sources.length) {
                art.append(mosaic(sources, false));
            }
        } else if (art.classList.contains('mosaic') && !art.children.length) {
            art.remove();
        }
    }, true);

    // Logos load when the channel page is first opened, not with the page:
    // loading="lazy" does not hold them back on the hidden page, and on
    // mobile data they are most of what a page open costs.
    function loadLogos() {
        for (const logo of groups.querySelectorAll('img[data-src]')) {
            logo.src = logo.dataset.src;
            logo.removeAttribute('data-src');
        }
    }

    // Scrolls the channel page only. scrollIntoView() would also scroll the
    // page container sideways, which is overflow: hidden but still scrollable,
    // and leave every page shifted. target: centred in place of the active
    // row or tile.
    function centreActive(target) {
        const active = target || groups.querySelector('.channel.active, .tile.active');
        const pageBox = groups.closest('.page');
        if (!active || !pageBox) {
            return;
        }
        const offset = active.getBoundingClientRect().top - pageBox.getBoundingClientRect().top;
        pageBox.scrollTop += offset - (pageBox.clientHeight - active.offsetHeight) / 2;
    }

    // Quality choices show under the active channel only; with dozens of
    // channels, choices under every one would bury the list.
    function markActive() {
        for (const old of groups.querySelectorAll('.streams')) {
            old.remove();
        }
        for (const button of groups.querySelectorAll('.channel')) {
            const active = app.channel && button.dataset.id === app.channel.id;
            button.classList.toggle('active', active);
            if (active) {
                addStreams(button);
            }
        }
        // The tile of the playing station, or of the group holding it. A
        // station's own tile gets its choices under it, across the grid.
        for (const tile of groups.querySelectorAll('.tile')) {
            const active = Boolean(app.channel) && (tile.dataset.group !== undefined
                ? tile.dataset.group === app.channel.group
                : tile.dataset.id === app.channel.id);
            tile.classList.toggle('active', active);
            if (active && tile.dataset.id !== undefined) {
                addStreams(tile);
            }
        }
    }

    function addStreams(after) {
        if (app.channel.streams.length < 2) {
            return;
        }
        const streams = element('div', 'streams');
        for (const stream of app.channel.streams) {
            const choice = element('button', stream === app.stream ? 'active' : '', streamLabel(stream));
            choice.type = 'button';
            choice.dataset.stream = stream.name;
            streams.append(choice);
        }
        after.after(streams);
    }

    groups.addEventListener('click', (event) => {
        if (event.target.closest('.more-stations')) {
            app.unlock();
            return;
        }
        const choice = event.target.closest('.streams button');
        if (choice) {
            app.selectStream(choice.dataset.stream);
            return;
        }
        const group = event.target.closest('.tile[data-group]');
        if (group) {
            showGroup(group.dataset.group);
            return;
        }
        const button = event.target.closest('.channel, .tile[data-id]');
        if (!button) {
            return;
        }
        if (!app.channel || button.dataset.id !== app.channel.id) {
            app.selectChannel(button.dataset.id);
        }
        app.showPage('main');
    });
    toTiles.addEventListener('click', () => showGroup(null));
    events.on('channel', markActive);
    events.on('stream', markActive);
    events.on('channels', () => {
        renderChannels();
        if (page === 'channels') {
            loadLogos();
        }
        markActive();
    });

    renderChannels();
    markActive();
}
