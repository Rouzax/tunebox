// Covers: the songs of the last 30 days with the image each one shows,
// filtered to the problems, and the forms that fix one. Song text comes
// from the stations' feeds and goes in as text only (dom.js).
import {button, el, icon, when} from './dom.js';
import {stationName} from './usage.js';

// The types the server takes for a cover upload; the bytes decide there.
export const COVER_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
const VIEWS = [['problems', 'Problems'], ['all', 'All'], ['fixed', 'Fixed by you']];
const DEBOUNCE_MS = 300;
// The server's bounds on a search text (TEXT_MAX) and an album link (URL_MAX).
const TEXT_MAX = 300;
const LINK_MAX = 2048;
// Mirrors coversearch._ALBUM: the whole input, Deezer's own host.
const ALBUM = /^https:\/\/(?:www\.)?deezer\.com(?:\/[a-z]{2})?\/album\/[0-9]{1,12}(?:\?[^\s#]*)?$/;
const LINK_RULE = 'Paste a Deezer album link (https://www.deezer.com/album/...). Share links (deezer.page.link) are not followed.';
const REASONS = {
    gone: 'the image is gone',
    unreachable: 'the image host did not answer',
    too_large: 'the image is too large',
    not_image: 'the answer is not an image',
    disk_low: 'the disk is almost full',
    refused: 'the link is not a catalogue image',
    unwritable: 'tunebox cannot write its cover folder',
    crashed: 'tunebox could not save it',
};
export const STATE_TEXT = {
    cover: 'Cover',
    artist: 'Artist photo',
    nothing: 'No image',
    looking: 'Still looking',
    fix: 'Your fix',
    fetching: 'Your fix · fetching',
};

export function stateLine(row) {
    if (row.state === 'failed') {
        return `Fetch failed: ${REASONS[row.reason] || row.reason}`;
    }
    if (row.state === 'lost') {
        return 'Image lost: upload it again';
    }
    const text = STATE_TEXT[row.state] || row.state;
    return row.fix && row.last_at === null ? `${text} · not heard in 30 days` : text;
}

export function listPath(view, query, after) {
    let path = `covers?view=${view}`;
    if (query) {
        path += `&q=${encodeURIComponent(query)}`;
    }
    if (after) {
        path += `&after=${encodeURIComponent(after)}`;
    }
    return path;
}

export function albumLink(text) {
    const trimmed = text.trim();
    return ALBUM.test(trimmed) ? trimmed : null;
}

export function releaseText(release) {
    if (release === null || release === undefined) {
        return 'not checked';
    }
    return release ? 'release cover' : 'not a release cover';
}

export function searchText(row) {
    return `${row.artist} ${row.title}`.trim();
}

// The fix a row's Back to automatic removes: on a song showing its
// artist's photo fix, that photo fix (its key is the photo key).
export function clearKey(row) {
    return row.fix.key;
}

// Every song row offers the photo form: the photo is also the other side
// of a song's cover. A photo fix's own row has no photo_key; its Fix
// opens the photo form.
export function offersPhoto(row) {
    return Boolean(row.photo_key);
}

// The row of a photo fix carries the photo key as its own key.
function photoKey(row) {
    return row.key.startsWith('photo:') ? row.key : row.photo_key;
}

function unreachable(ctx) {
    return () => ctx.message('Could not reach tunebox.');
}

export function render(root, ctx) {
    const current = ctx.stillOpen;
    const page = {view: 'problems', query: '', next: null, rows: []};
    const totals = el('p', {class: 'cover-totals hint'});
    const note = el('p', {class: 'warning', hidden: true}, icon('warning'),
        ' tunebox keeps its song list in memory only (it could not open tunebox.db), so the list is empty until a restart. Fixes still apply.');
    const unread = el('p', {class: 'warning', hidden: true}, icon('warning'),
        ' tunebox could not read its song list just now. Reload the page to try again; fixes still apply.');
    const list = el('ul', {class: 'cover-list'});
    const more = button('history', 'More', () => load(true).catch(unreachable(ctx)), {class: 'quiet', hidden: true});
    const views = VIEWS.map(([value, label]) => el('button', {
        type: 'button', class: 'quiet', 'data-view': value, 'aria-pressed': String(value === page.view),
        onclick: () => {
            page.view = value;
            for (const choice of views) {
                choice.setAttribute('aria-pressed', String(choice.dataset.view === value));
            }
            load(false).catch(unreachable(ctx));
        },
    }, label));
    const search = el('input', {type: 'search', name: 'cover-search', placeholder: 'Artist or title', maxlength: String(TEXT_MAX)});
    let timer = null;
    search.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
            page.query = search.value.trim();
            load(false).catch(unreachable(ctx));
        }, DEBOUNCE_MS);
    });

    // A newer read wins: an older answer that arrives late is dropped.
    let serial = 0;
    async function load(append) {
        serial += 1;
        const mine = serial;
        const answer = await ctx.request('GET', listPath(page.view, page.query, append ? page.next : null));
        if (!current() || mine !== serial || answer.status === 401) {
            return;
        }
        if (!answer.ok) {
            ctx.fail(answer.data);
            return;
        }
        const data = answer.data;
        note.hidden = data.available !== false;
        unread.hidden = data.readable !== false;
        const t = data.totals;
        totals.textContent = `Last 30 days: ${t.songs} songs · ${t.cover} with a cover · ${t.artist} showing an artist photo · ${t.nothing} with no image · ${t.fixed} fixed by you`;
        page.rows = append ? page.rows.concat(data.rows) : data.rows;
        page.next = data.next;
        list.replaceChildren(...(page.rows.length ? page.rows.map((row) => rowNode(row)) : [el('li', {class: 'hint'}, 'Nothing to show.')]));
        more.hidden = !page.next;
    }

    // Opens one form in a row's panel; the same button again closes it.
    function open(panel, form, build) {
        if (!panel.hidden && panel.dataset.form === form) {
            panel.hidden = true;
            return;
        }
        panel.dataset.form = form;
        panel.replaceChildren(...build());
        panel.hidden = false;
    }

    function rowNode(row) {
        const channels = ctx.config.state.channels;
        const panel = el('div', {class: 'cover-fix', hidden: true});
        const photoRow = row.key.startsWith('photo:');
        const actions = el('div', {class: 'actions'},
            button('edit', 'Fix', () => open(panel, photoRow ? 'photo' : 'cover', () => photoRow ? photoForm(row) : coverForms(row)), {class: 'quiet'}));
        if (offersPhoto(row)) {
            actions.append(el('button', {type: 'button', class: 'quiet', onclick: () => open(panel, 'photo', () => photoForm(row))}, 'This artist\'s photo is wrong'));
        }
        if (row.fix) {
            const label = clearKey(row) === row.key ? 'Back to automatic' : 'Back to automatic photo';
            actions.append(button('undo', label, () => clear(row).catch(unreachable(ctx)), {class: 'quiet'}));
        }
        const heard = row.last_at === null ? null : `${row.channels.map((id) => stationName(channels, id)).join(', ')} · ${when(row.last_at / 1000)}`;
        return el('li', {class: 'cover-row', 'data-key': row.key, 'data-state': row.state},
            row.image ? el('img', {src: row.image, alt: '', loading: 'lazy', width: '64', height: '64'}) : el('span', {class: 'cover-empty'}),
            el('div', {class: 'cover-text'},
                el('span', {class: 'name'}, row.title ? `${row.artist} - ${row.title}` : row.artist),
                heard ? el('span', {class: 'hint'}, heard) : null,
                el('span', {class: `cover-state state-${row.state}`}, stateLine(row)),
                row.fix ? el('span', {class: 'hint'}, row.fix.label) : null),
            actions, panel);
    }

    // Sends a change or a search; resolves the data of a good answer, null
    // when the page moved on, the session ended (the login shows) or the
    // refusal went to ctx.fail.
    async function post(path, body) {
        const answer = await ctx.request('POST', path, body);
        if (!current() || answer.status === 401) {
            return null;
        }
        if (!answer.ok) {
            ctx.fail(answer.data);
            return null;
        }
        return answer.status === 204 ? {} : answer.data;
    }

    async function afterChange(text) {
        ctx.message(text);
        await load(false);
    }

    async function pick(token) {
        if (await post('covers/fix', {token})) {
            await afterChange('Saved. It shows in the player once tunebox has downloaded it.');
        }
    }

    async function clear(row) {
        const key = clearKey(row);
        if (await post('covers/fix/clear', {key})) {
            await afterChange(key === row.key
                ? 'Back to automatic: tunebox looks the cover up again.'
                : 'Back to automatic: tunebox looks the artist photo up again for every song by this artist.');
        }
    }

    function choices(box, answer, photo) {
        const said = answer.unanswered.map((name) => el('p', {class: 'warning'}, icon('warning'),
            ` ${name === 'itunes' ? 'iTunes' : 'Deezer'} did not answer; try again later.`));
        const grid = el('div', {class: 'cover-choices'}, answer.choices.map((choice) => el('button', {
            type: 'button', class: 'cover-choice', onclick: () => pick(choice.token).catch(unreachable(ctx)),
        },
        el('img', {src: choice.thumb, alt: '', loading: 'lazy'}),
        el('span', {class: 'name'}, photo ? choice.artist : `${choice.artist} - ${choice.album}`),
        el('span', {class: 'hint'}, photo
            ? `${(choice.fans || 0).toLocaleString()} fans · ${releaseText(choice.release)}`
            : (choice.source === 'itunes' ? 'iTunes' : 'Deezer')))));
        const none = photo && answer.no_photo
            ? button('close', 'No photo', () => pick(answer.no_photo).catch(unreachable(ctx)), {class: 'quiet'})
            : null;
        box.replaceChildren(...said, answer.choices.length ? grid : el('p', {class: 'hint'}, 'No results.'), ...(none ? [none] : []));
    }

    // A form with one text field that asks covers/search; body(text, note)
    // gives the request, or null after it has said why not in note. The
    // button waits for the answer: the server runs one search at a time.
    function searchForm(label, value, maxlength, body, photo) {
        const input = el('input', {name: 'cover-query', value, maxlength: String(maxlength), required: true});
        const submit = el('button', {type: 'submit'}, 'Search');
        const results = el('div', {class: 'cover-results'});
        const note = el('p', {class: 'hint', role: 'alert'});
        const form = el('form', {class: 'cover-search'},
            el('label', null, label, input),
            el('div', {class: 'actions'}, submit), note, results);
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            const request = body(input.value, note);
            if (!request) {
                return;
            }
            note.textContent = 'Searching...';
            submit.disabled = true;
            post('covers/search', request).then((answer) => {
                note.textContent = '';
                if (answer) {
                    choices(results, answer, photo);
                }
            }).catch(unreachable(ctx)).then(() => {
                submit.disabled = false;
            });
        });
        return form;
    }

    function uploadPart(row) {
        const note = el('span', {class: 'hint', role: 'alert'});
        const picker = el('input', {type: 'file', name: 'cover-file', hidden: true, accept: COVER_TYPES.join(',')});
        picker.addEventListener('change', async () => {
            const file = picker.files[0];
            picker.value = '';
            if (!file) {
                return;
            }
            if (!COVER_TYPES.includes(file.type)) {
                note.textContent = 'Upload a JPEG, PNG or WebP image.';
                return;
            }
            if (file.size > ctx.limits.logo_max) {
                note.textContent = `The image is larger than ${ctx.limits.logo_max / 1000000} MB.`;
                return;
            }
            note.textContent = '';
            const query = new URLSearchParams({key: row.key, artist: row.artist, title: row.title}).toString();
            let answer;
            try {
                answer = await ctx.upload(file, null, `covers/upload?${query}`);
            } catch (error) {
                note.textContent = 'Could not reach tunebox.';
                return;
            }
            if (!current() || answer.status === 401) {
                return;
            }
            if (!answer.ok) {
                ctx.fail(answer.data);
                return;
            }
            await afterChange('Uploaded. It shows once tunebox has copied it.');
        });
        return el('div', {class: 'actions'}, button('arrow_upward', 'Upload an image', () => picker.click(), {class: 'quiet'}), picker, note);
    }

    function coverForms(row) {
        const base = {key: row.key, artist: row.artist, title: row.title};
        return [
            searchForm('Search Deezer and iTunes', searchText(row), TEXT_MAX,
                (text) => Object.assign({kind: 'cover', text: text.trim()}, base), false),
            searchForm('Or paste a Deezer album link', '', LINK_MAX, (text, note) => {
                const found = albumLink(text);
                if (!found) {
                    note.textContent = LINK_RULE;
                    return null;
                }
                return Object.assign({kind: 'link', text: found}, base);
            }, false),
            uploadPart(row),
        ];
    }

    function photoForm(row) {
        return [searchForm('Search Deezer artists', row.artist, TEXT_MAX,
            (text) => ({kind: 'photo', key: photoKey(row), artist: row.artist, title: '', text: text.trim()}), true)];
    }

    root.replaceChildren(
        el('section', {class: 'covers-page'},
            el('h2', null, 'Covers'),
            totals, note, unread,
            el('div', {class: 'actions ranges'}, views),
            search, list, more));
    load(false).catch(unreachable(ctx));
}
