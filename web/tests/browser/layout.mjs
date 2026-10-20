// Layout check for the player: overlap, clipping and sideways scroll at every
// window size the player is used at, on every page, in day and night, stopped
// and playing, embedded or not, with long titles. Also checks that a page
// open loads only the current channel's logo before the channel page is
// opened.
//
// Needs Google Chrome or Chromium (or CHROME=<path>) and a running tunebox.
// Node 22 or later (Node 20 needs --experimental-websocket):
//
//   node web/tests/browser/layout.mjs http://127.0.0.1:8080 [screenshot folder]
//
// Exits 1 and lists every problem when one is found, 2 when Chrome could not
// run the check (it did not start, or the page crashed or stopped answering).

import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

import {connect, sleep} from './cdp.mjs';

const BASE = (process.argv[2] || 'http://127.0.0.1:8080').replace(/\/$/, '');
const SHOTS = process.argv[3] || null;
const PORT = 9300 + Math.floor(Math.random() * 500);

// The car's browser windows (a third, two thirds and full width of its
// screen), a phone and a desktop.
const SIZES = [
    {name: 'car-third', width: 480, height: 560},
    // Measured on the car's /check.html with the browser in a third.
    {name: 'car-third-measured', width: 489, height: 518},
    {name: 'car-two-thirds', width: 960, height: 560},
    {name: 'car-full', width: 1440, height: 560},
    {name: 'phone', width: 390, height: 844, mobile: true},
    {name: 'phone-landscape', width: 844, height: 390, mobile: true},
    {name: 'desktop', width: 1280, height: 800},
];
const PAGES = ['main', 'history', 'channels', 'viz'];
const LONG_TITLE = 'An Extremely Long Track Title That Will Never Fit On One Line (Extended Festival Mix)';
const LONG_ARTIST = 'ARTIST ONE, ARTIST TWO, ARTIST THREE, ARTIST FOUR, ARTIST FIVE';
const LONG_PLAYED = 'Somebody With A Long Name Mainstage Tomorrowland Winter 2026';
// Sub-pixel rounding of transforms and borders.
const SLACK = 1;

if (typeof WebSocket === 'undefined') {
    console.error('This Node has no WebSocket client: use Node 22+, or run Node 20 with --experimental-websocket.');
    process.exit(2);
}

// Runs in the page: measures everything and returns the problems found.
// Stacked, the card keeps one height whatever the text, once ticker.js has
// set the scrolling lines: the cover above it takes the rest, and a card
// that changed with the song would resize it. Runs in the page; resolves
// to the problems found.
async function steadyCard() {
    const root = document.querySelector('.tunebox');
    if (root.dataset.layout === 'wide' || root.dataset.page !== 'main') {
        return [];
    }
    const nodes = ['.car-title', '.car-artist', '.car-played'].map((s) => document.querySelector(s));
    const playedLine = nodes[2].closest('.car-line');
    const kept = [...nodes.map((n) => n.textContent), playedLine.hidden];
    const settle = () => new Promise((resolve) => setTimeout(resolve, 150));
    const measure = async (title, artist, played) => {
        nodes[0].textContent = title;
        nodes[1].textContent = artist;
        nodes[2].textContent = played;
        playedLine.hidden = !played;
        await settle();
        return {
            card: document.querySelector('.onair').getBoundingClientRect().height,
            cover: document.querySelector('.artwork-img').getBoundingClientRect().width,
            scrolling: document.querySelectorAll('.onair .scrolling').length,
        };
    };
    // Three times over, too long for any window.
    const longer = (text) => [text, text, text].join(' ');
    const long = await measure(longer(kept[0]), longer(kept[1]), longer(kept[2] || 'Somebody With A Long Name Mainstage'));
    const short = await measure('Rain', 'Kaytranada', '');
    nodes.forEach((n, i) => { n.textContent = kept[i]; });
    playedLine.hidden = kept[3];
    await settle();
    const problems = [];
    if (Math.abs(long.card - short.card) > 0.5 || Math.abs(long.cover - short.cover) > 0.5) {
        problems.push(`the card's height follows the text (${long.card.toFixed(1)} vs ${short.card.toFixed(1)} px)`);
    }
    if (!long.scrolling || short.scrolling) {
        problems.push(`long lines scroll: ${long.scrolling}, short lines scroll: ${short.scrolling}`);
    }
    return problems;
}

function inspect(page, slack) {
    const problems = [];
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    const root = document.querySelector('.tunebox');
    // Faded-out controls (opacity 0, as the day/night button behind text
    // pages) cannot be seen or tapped.
    const faded = (el) => {
        for (let node = el; node && node !== document.body; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.opacity === '0' || style.pointerEvents === 'none') {
                return true;
            }
        }
        return false;
    };
    const visible = (el) => {
        if (el.closest('.hidden') || el.closest('[hidden]') || faded(el)) {
            return false;
        }
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const label = (el) => {
        const cls = typeof el.className === 'string' && el.className ? `.${el.className.trim().split(/\s+/).join('.')}` : '';
        const text = (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 30);
        return `${el.tagName.toLowerCase()}${cls}${text ? ` "${text}"` : ''}`;
    };
    const box = (el) => el.getBoundingClientRect();
    const overlap = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > slack
        && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > slack;
    const inside = (r, area) => r.left >= area.left - slack && r.right <= area.right + slack
        && r.top >= area.top - slack && r.bottom <= area.bottom + slack;
    const viewport = {left: 0, top: 0, right: vw, bottom: vh};

    if (document.documentElement.scrollWidth > vw + slack) {
        problems.push(`page scrolls sideways (${document.documentElement.scrollWidth} > ${vw})`);
    }
    const container = document.querySelector('.container');
    if (container.scrollLeft !== 0 || container.scrollTop !== 0) {
        problems.push(`page container is scrolled (${container.scrollLeft}, ${container.scrollTop})`);
    }

    // The active page fills the area above the bar.
    const activePage = document.querySelector(`.page.${page === 'channels' ? 'channels-page' : page === 'viz' ? 'viz-page' : page}`);
    const pageRect = box(activePage);
    if (!inside(pageRect, viewport)) {
        problems.push(`active page is not within the window (${Math.round(pageRect.left)},${Math.round(pageRect.top)} ${Math.round(pageRect.width)}x${Math.round(pageRect.height)})`);
    }
    const bar = document.querySelector('.player');
    const barRect = box(bar);
    if (!inside(barRect, viewport)) {
        problems.push(`control bar is not within the window (top ${Math.round(barRect.top)}, bottom ${Math.round(barRect.bottom)} of ${vh})`);
    }
    if (overlap(pageRect, barRect)) {
        problems.push('active page runs under the control bar');
    }

    // Every control a listener can reach on this view: inside the window
    // (or, on a scrolling page, inside the page horizontally and reachable by
    // scrolling) and not covered by anything else.
    const controls = [...document.querySelectorAll('button, input, a')].filter(visible).filter((el) => {
        const owner = el.closest('.page');
        return !owner || owner === activePage;
    });
    for (const el of controls) {
        const r = box(el);
        const scrolls = el.closest('.page') === activePage;
        if (scrolls) {
            if (r.left < pageRect.left - slack || r.right > pageRect.right + slack) {
                problems.push(`${label(el)} sticks out of the page sideways`);
            }
            const visibleNow = r.bottom > pageRect.top && r.top < pageRect.bottom;
            if (!visibleNow) {
                continue;
            }
        } else if (!inside(r, viewport)) {
            problems.push(`${label(el)} is outside the window (${Math.round(r.left)},${Math.round(r.top)} ${Math.round(r.width)}x${Math.round(r.height)})`);
            continue;
        }
        const x = Math.min(Math.max(r.left + r.width / 2, 0), vw - 1);
        const y = Math.min(Math.max(r.top + r.height / 2, Math.max(0, pageRect.top)), (scrolls ? pageRect.bottom : vh) - 1);
        const hit = document.elementFromPoint(x, y);
        if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) {
            problems.push(`${label(el)} is covered by ${label(hit)}`);
        }
    }

    // Controls in the bar never touch each other.
    const barControls = controls.filter((el) => bar.contains(el));
    for (let i = 0; i < barControls.length; i++) {
        for (let j = i + 1; j < barControls.length; j++) {
            if (overlap(box(barControls[i]), box(barControls[j]))) {
                problems.push(`${label(barControls[i])} overlaps ${label(barControls[j])}`);
            }
        }
    }
    const daynight = document.querySelector('.car-daynight');
    if (visible(daynight) && page === 'main') {
        const art = box(document.querySelector('.artwork-img'));
        if (overlap(box(daynight), art)) {
            problems.push('day/night button covers the cover');
        }
    }

    if (page === 'main') {
        // Now playing fits its page: a scrollbar there means something is cut.
        if (activePage.scrollHeight > activePage.clientHeight + slack) {
            problems.push(`now playing page needs scrolling (${activePage.scrollHeight} > ${activePage.clientHeight})`);
        }
        const art = box(document.querySelector('.artwork-img'));
        const lines = [...document.querySelectorAll('.onair .car-line')].filter(visible);
        const text = lines.map(box).reduce((a, b) => ({
            left: Math.min(a.left, b.left), top: Math.min(a.top, b.top),
            right: Math.max(a.right, b.right), bottom: Math.max(a.bottom, b.bottom),
        }));
        if (art.width < 64) {
            problems.push(`cover is too small (${Math.round(art.width)} px)`);
        }
        if (!inside(art, viewport)) {
            problems.push('cover is not within the window');
        }
        if (overlap(art, text)) {
            problems.push('cover and track text overlap');
        }
        if (text.bottom > barRect.top + slack) {
            problems.push(`track text runs under the control bar (${Math.round(text.bottom)} > ${Math.round(barRect.top)})`);
        }
        // The track text sits on a card: on the control bar across the
        // window when stacked, floating beside the cover when wide, with a
        // width that does not follow the title.
        const card = document.querySelector('.onair');
        const cardBox = box(card);
        if (root.dataset.layout === 'wide') {
            if (cardBox.bottom > barRect.top - 4) {
                problems.push('the card touches the control bar beside the cover');
            }
            if (cardBox.left < art.right) {
                problems.push('the card overlaps the cover');
            }
        } else {
            if (cardBox.left > slack || cardBox.right < vw - slack) {
                problems.push(`the card does not span the window (${Math.round(cardBox.left)}..${Math.round(cardBox.right)})`);
            }
            if (Math.abs(cardBox.bottom - barRect.top) > slack) {
                problems.push(`the card is not on the control bar (${Math.round(cardBox.bottom)} vs ${Math.round(barRect.top)})`);
            }
        }
        const titleNode = document.querySelector('.car-title');
        const artistNode = document.querySelector('.car-artist');
        const playedLine = document.querySelector('.car-played-line');
        const shown = [titleNode.textContent, artistNode.textContent, playedLine.hidden];
        titleNode.textContent = 'Rain';
        artistNode.textContent = 'Kaytranada';
        playedLine.hidden = true;
        const short = card.getBoundingClientRect();
        const shortArt = document.querySelector('.artwork-img').getBoundingClientRect();
        [titleNode.textContent, artistNode.textContent, playedLine.hidden] = shown;
        if (Math.abs(short.width - cardBox.width) > 1) {
            problems.push(`the card's width follows the title (${Math.round(short.width)} vs ${Math.round(cardBox.width)})`);
        }
        // Stacked, the cover takes the height the card leaves: a card that
        // grew with the text would resize the cover on every song.
        if (root.dataset.layout !== 'wide' && (Math.abs(short.height - cardBox.height) > 1 || Math.abs(shortArt.width - art.width) > 1)) {
            problems.push(`the card's height follows the text (${Math.round(short.height)} vs ${Math.round(cardBox.height)})`);
        }
        // Text on the card reads at arm's length: WCAG contrast 4.5 or more.
        const rgba = (value) => (value.match(/[\d.]+/g) || []).map(Number);
        const lum = ([r, g, b]) => [r, g, b].map((v) => {
            const c = v / 255;
            return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
        }).reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
        const ground = rgba(getComputedStyle(card).backgroundColor);
        if (ground.length < 3 || (ground.length === 4 && ground[3] < 0.85)) {
            problems.push(`the card is not solid (${getComputedStyle(card).backgroundColor})`);
        } else {
            for (const line of lines) {
                const ink = rgba(getComputedStyle(line).color);
                const [a, b] = [lum(ink), lum(ground)].sort((x, y) => y - x);
                const ratio = (a + 0.05) / (b + 0.05) * (parseFloat(getComputedStyle(line).opacity) || 1);
                if (ratio < 4.5) {
                    problems.push(`"${line.textContent.slice(0, 20)}" has contrast ${ratio.toFixed(1)} on the card`);
                }
            }
        }
        if (text.top < 0 || text.left < -slack || text.right > vw + slack) {
            problems.push('track text is outside the window');
        }
        // Beside the cover a glance reads still text: every line wraps, to
        // two lines at most, the title three. Stacked, every line is one
        // line, and one that does not fit scrolls.
        const wide = root.dataset.layout === 'wide';
        for (const line of lines) {
            const style = getComputedStyle(line);
            const height = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.4;
            // Below 1.3, tall Poppins glyphs (parentheses) of a cut line's
            // hidden next line show as marks above the cut.
            if (parseFloat(style.lineHeight) < 1.3 * parseFloat(style.fontSize) - 0.5) {
                problems.push(`"${line.textContent.slice(0, 30)}" has line-height ${style.lineHeight} at ${style.fontSize}`);
            }
            const most = !wide ? 1 : line.classList.contains('car-title-line') ? 3 : 2;
            const limit = parseInt(style.webkitLineClamp, 10);
            if (wide ? !(limit >= 1 && limit <= most) : style.whiteSpace !== 'nowrap') {
                problems.push(`"${line.textContent.slice(0, 30)}" may take more than ${most} lines`);
            }
            // Stacked, a line is exactly one line tall, scrolling or still:
            // a pixel either way would resize the cover when scrolling starts.
            if (!wide && Math.abs(line.getBoundingClientRect().height - height) > 0.5) {
                problems.push(`"${line.textContent.slice(0, 30)}" is ${line.getBoundingClientRect().height.toFixed(1)} px tall, not one line (${height.toFixed(1)} px)`);
            }
            if (line.getBoundingClientRect().height > most * height + slack) {
                problems.push(`"${line.textContent.slice(0, 30)}" takes more than ${most} lines`);
            }
            // The drawn text, not scrollWidth: a one-line clamp reports the
            // unwrapped width there although it draws an ellipsis.
            const edges = line.getBoundingClientRect();
            const words = document.createRange();
            words.selectNodeContents(line);
            const runs = [...words.getClientRects()].some((r) => r.left < edges.left - slack || r.right > edges.right + slack);
            const scrolling = line.classList.contains('scrolling');
            if (runs && !scrolling) {
                problems.push(`"${line.textContent.slice(0, 30)}" runs sideways out of its line`);
            }
            // A line cut off with "..." must have used its width first:
            // balanced wrapping spreads long text evenly over more lines
            // than are shown, so the visible ones end short ("MEDUZA,...").
            if (wide && line.scrollHeight > line.clientHeight + 1) {
                const shown = [...words.getClientRects()].filter((r) => r.bottom <= edges.bottom + 1 && r.width > 0);
                const tops = [...new Set(shown.map((r) => Math.round(r.top)))];
                for (const top of tops) {
                    const used = shown.filter((r) => Math.round(r.top) === top)
                        .reduce((a, r) => ({left: Math.min(a.left, r.left), right: Math.max(a.right, r.right)}), {left: Infinity, right: -Infinity});
                    if (used.right - used.left < 0.6 * edges.width) {
                        problems.push(`"${line.textContent.slice(0, 30)}" is cut off with a short line (${Math.round(used.right - used.left)} of ${Math.round(edges.width)} px)`);
                        break;
                    }
                }
            }
            if (!wide && scrolling !== (line.firstElementChild.offsetWidth > line.clientWidth + 1)) {
                problems.push(`"${line.textContent.slice(0, 30)}" ${scrolling ? 'scrolls although it fits' : 'is cut instead of scrolling'}`);
            }
        }
        // Only a scrolling line moves (its text, and its edge fades), and only stacked.
        const onair = document.querySelector('.onair');
        if (document.getAnimations().some((a) => a.effect && a.effect.target && onair.contains(a.effect.target)
            && (wide || !a.effect.target.matches('.car-line.scrolling, .scrolling > .car-line-text')))) {
            problems.push('track text moves');
        }
        const played = document.querySelector('.car-played-line');
        if (visible(played) && parseFloat(getComputedStyle(played).fontSize) < 16) {
            problems.push(`played-by line is ${getComputedStyle(played).fontSize}, under 16px`);
        }
        // The station sits at the top of the window, out of the song's caption.
        const station = document.querySelector('.car-station');
        if (!visible(station) || onair.contains(station)) {
            problems.push('station name is not shown at the top');
        } else {
            // The words and logo, not the padded header, which spans the width.
            const parts = [...station.children].filter(visible).map(box);
            const where = parts.reduce((a, b) => ({
                left: Math.min(a.left, b.left), top: Math.min(a.top, b.top),
                right: Math.max(a.right, b.right), bottom: Math.max(a.bottom, b.bottom),
            }));
            if (overlap(where, art) || overlap(where, text)) {
                problems.push('station name overlaps the cover or the track text');
            }
            if (visible(daynight) && overlap(where, box(daynight))) {
                problems.push('station name runs under the day/night button');
            }
            if (!inside(where, viewport)) {
                problems.push('station name is outside the window');
            }
        }
        for (let i = 0; i < lines.length; i++) {
            for (let j = i + 1; j < lines.length; j++) {
                if (overlap(box(lines[i]), box(lines[j]))) {
                    problems.push(`text lines ${i + 1} and ${j + 1} overlap`);
                }
            }
        }
        // A status message shows on every start and channel change, just
        // when a glance goes to the track text.
        const toast = document.querySelector('.toast');
        const wasHidden = toast.classList.contains('hidden');
        const oldText = toast.textContent;
        toast.textContent = 'Playing One World Radio...';
        toast.classList.remove('hidden');
        if (overlap(box(toast), text)) {
            problems.push('a status message covers the track text');
        }
        // The box is dark in day and night alike, so its text must be light.
        const [r, g, b] = getComputedStyle(toast).color.match(/[\d.]+/g).map(Number);
        if (0.2126 * r + 0.7152 * g + 0.0722 * b < 200) {
            problems.push(`status message text is not light (${getComputedStyle(toast).color})`);
        }
        // The longest message the player shows: it wraps, inside its box.
        toast.textContent = 'This browser cannot play AAC. Pick another quality on the channel page.';
        const frame = toast.getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(toast);
        const words = range.getBoundingClientRect();
        if (words.bottom > frame.bottom + slack || words.top < frame.top - slack) {
            problems.push('a long status message runs out of its box');
        }
        if (overlap(box(toast), text)) {
            problems.push('a long status message covers the track text');
        }
        toast.textContent = oldText;
        toast.classList.toggle('hidden', wasHidden);
    }
    if (page === 'channels') {
        const unloaded = [...document.querySelectorAll('.channel img')].filter((img) => !img.getAttribute('src'));
        if (unloaded.length) {
            problems.push(`${unloaded.length} channel logo(s) not loaded on the channel page`);
        }
        // tunebox keeps a small copy of large PNG logos for this list.
        const oversized = [...document.querySelectorAll('.channel img')].filter((img) => img.naturalWidth > 256);
        if (oversized.length) {
            problems.push(`${oversized.length} channel logo(s) loaded at full size (${oversized[0].naturalWidth} px)`);
        }
    }
    if (page === 'viz') {
        // Buttons side by side in the grid start at the same height.
        const tops = [...document.querySelectorAll('.viz-list button')].map((b) => b.getBoundingClientRect());
        const crooked = tops.filter((r) => tops.some((o) => o !== r && Math.abs(o.bottom - r.bottom) < r.height / 2 && Math.abs(o.top - r.top) > 1));
        if (crooked.length) {
            problems.push(`visualizer buttons in one row are not level (${crooked.length})`);
        }
    }
    return {problems, layout: root.dataset.layout};
}

// The car has a touch screen and no keyboard, yet buttons there showed the
// keyboard focus ring after leaving fullscreen. A tap followed by a key
// that is not navigation (Escape) must not leave that ring on the tapped
// button; Tab still shows one.
async function checkFocusRing(send, evaluate) {
    const problems = [];
    await send('Emulation.setDeviceMetricsOverride', {width: 489, height: 518, deviceScaleFactor: 1, mobile: false});
    await send('Emulation.setTouchEmulationEnabled', {enabled: true, maxTouchPoints: 5});
    await send('Page.navigate', {url: `${BASE}/`});
    await sleep(2500);
    const ring = () => evaluate(`(() => {
        const focused = document.querySelector(':focus-visible');
        return focused && getComputedStyle(focused).outlineStyle !== 'none' ? focused.className.baseVal || focused.className : null;
    })()`);
    const key = async (name, code) => {
        await send('Input.dispatchKeyEvent', {type: 'keyDown', key: name, code: name, windowsVirtualKeyCode: code});
        await send('Input.dispatchKeyEvent', {type: 'keyUp', key: name, code: name, windowsVirtualKeyCode: code});
        await sleep(200);
    };
    const at = await evaluate(`(() => {
        const box = document.querySelector('.volume-icon').getBoundingClientRect();
        return {x: box.x + box.width / 2, y: box.y + box.height / 2};
    })()`);
    await send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [at]});
    await send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    await sleep(200);
    await key('Escape', 27);
    const stray = await ring();
    if (stray) {
        problems.push(`focus ring on ${stray} after a tap and Escape`);
    }
    await key('Tab', 9);
    if (!(await ring())) {
        problems.push('no focus ring after Tab');
    }
    await send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [at]});
    await send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    await sleep(200);
    const after = await ring();
    if (after) {
        problems.push(`focus ring on ${after} after a tap that followed Tab`);
    }
    await send('Emulation.setTouchEmulationEnabled', {enabled: false});
    return problems;
}

async function run() {
    const {send, errors, close, evaluate} = await connect({port: PORT});
    const failures = [];
    let views = 0;
    if (SHOTS) {
        mkdirSync(SHOTS, {recursive: true});
    }
    try {
        failures.push(...await checkFocusRing(send, evaluate));
        for (const size of SIZES) {
            await send('Emulation.setDeviceMetricsOverride', {
                width: size.width, height: size.height, deviceScaleFactor: 1, mobile: Boolean(size.mobile),
            });
            for (const embed of [false, true]) {
                for (const mode of ['day', 'night']) {
                    await send('Page.navigate', {url: `${BASE}/check.html`});
                    await sleep(200);
                    await evaluate(`localStorage.clear(); localStorage.setItem('tunebox.daynight', '${mode}'); localStorage.setItem('tunebox.visualizer', 'off')`);
                    await send('Page.navigate', {url: `${BASE}/${embed ? '?embed=1' : ''}`});
                    await sleep(2500);
                    // On mobile data every page open pays for what it loads: the
                    // channel logos wait for the channel page. The header shows
                    // the current channel's logo, and the cover may too, at
                    // either size (logos/ or logos/small/).
                    if (await evaluate(`Boolean(document.querySelector('.debug'))`)) {
                        failures.push(`${size.name}: the debug panel shows without ?debug=1`);
                    }
                    const logos = await evaluate(`new Set(performance.getEntriesByType('resource')
                        .filter((e) => e.name.indexOf('/logos/') >= 0)
                        .map((e) => e.name.split('/').pop().split(/[.?]/)[0])).size`);
                    if (logos > 1) {
                        failures.push(`${size.name} ${embed ? 'embed ' : ''}${mode}: page open loads ${logos} station logos`);
                    }
                    for (const state of ['stopped', 'playing']) {
                        // Layout only: the state and the long text are set directly.
                        await evaluate(`(() => {
                            document.querySelector('.tunebox').dataset.state = '${state}';
                            document.querySelector('.car-title').textContent = ${JSON.stringify(LONG_TITLE)};
                            document.querySelector('.car-artist').textContent = ${JSON.stringify(LONG_ARTIST)};
                            document.querySelector('.car-played').textContent = ${JSON.stringify(LONG_PLAYED)};
                            document.querySelector('.car-played').closest('.car-line').hidden = false;
                        })()`);
                        for (const page of PAGES) {
                            await evaluate(`(() => {
                                const root = document.querySelector('.tunebox');
                                const open = {history: '.open-history', channels: '.open-channels', viz: '.viz-toggle'}['${page}'];
                                if (root.dataset.page !== 'main') {
                                    root.querySelector('.page:not(.main) .btn-back') && [...root.querySelectorAll('.btn-back')][0].click();
                                }
                                if (open) {
                                    root.querySelector(open).click();
                                }
                            })()`);
                            // Page slide (600 ms) and fonts.
                            await sleep(900);
                            const view = `${size.name} ${embed ? 'embed ' : ''}${mode} ${state} ${page}`;
                            const result = await evaluate(`(${inspect.toString()})('${page}', ${SLACK})`);
                            views += 1;
                            for (const problem of [...result.problems, ...await evaluate(`(${steadyCard.toString()})()`)]) {
                                failures.push(`${view} [${result.layout}]: ${problem}`);
                            }
                            if (SHOTS && state === 'playing') {
                                const shot = await send('Page.captureScreenshot', {format: 'png'});
                                writeFileSync(join(SHOTS, `${view.replace(/ /g, '-')}.png`), Buffer.from(shot.result.data, 'base64'));
                            }
                        }
                        await evaluate(`document.querySelector('.tunebox').dataset.page !== 'main' && document.querySelector('.btn-back') && [...document.querySelectorAll('.btn-back')].forEach((b) => b.offsetParent && b.click())`);
                        await sleep(700);
                    }
                }
            }
        }
    } finally {
        await close();
    }
    for (const error of errors) {
        failures.push(`page error: ${error}`);
    }
    console.log(`${views} views checked`);
    if (failures.length) {
        console.log(`${failures.length} problem(s):`);
        for (const failure of failures) {
            console.log(`  ${failure}`);
        }
        process.exit(1);
    }
    console.log('no overlap, clipping or sideways scroll found');
}

run().catch((error) => {
    console.error(error);
    process.exit(2);
});
