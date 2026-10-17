import {groupChannels, streamLabel} from './channels.js';
import {coverFor, timeAgo} from './tracks.js';

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
    let page = 'main';

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
        if (event.key === 'Escape') {
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
            const what = element('div', 'what');
            what.append(element('div', 'title', track.title), element('div', 'artist', track.artist));
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

    function renderChannels() {
        const sections = groupChannels(app.channels).map((group) => {
            const section = element('section');
            if (group.name) {
                section.append(element('h4', '', group.name));
            }
            const list = element('ul');
            for (const channel of group.channels) {
                const item = element('li');
                const button = element('button', 'channel');
                button.type = 'button';
                button.dataset.id = channel.id;
                if (channel.logo) {
                    const logo = element('img');
                    logo.alt = '';
                    logo.loading = 'lazy';
                    logo.src = channel.logo;
                    button.append(logo);
                }
                button.append(element('span', 'name', channel.name));
                item.append(button);
                list.append(item);
            }
            section.append(list);
            return section;
        });
        replaceContent(groups, sections);
    }

    // Scrolls the channel page only. scrollIntoView() would also scroll the
    // page container sideways, which is overflow: hidden but still scrollable,
    // and leave every page shifted.
    function centreActive() {
        const active = groups.querySelector('.channel.active');
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
            if (active && app.channel.streams.length > 1) {
                const streams = element('div', 'streams');
                for (const stream of app.channel.streams) {
                    const choice = element('button', stream === app.stream ? 'active' : '', streamLabel(stream));
                    choice.type = 'button';
                    choice.dataset.stream = stream.name;
                    streams.append(choice);
                }
                button.after(streams);
            }
        }
    }

    groups.addEventListener('click', (event) => {
        const choice = event.target.closest('.streams button');
        if (choice) {
            app.selectStream(choice.dataset.stream);
            return;
        }
        const button = event.target.closest('.channel');
        if (!button) {
            return;
        }
        if (!app.channel || button.dataset.id !== app.channel.id) {
            app.selectChannel(button.dataset.id);
        }
        app.showPage('main');
    });
    events.on('channel', markActive);
    events.on('stream', markActive);

    renderChannels();
    markActive();
}
