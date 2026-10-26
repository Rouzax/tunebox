// The station list: one section per group, every change saved at once. A
// long list folds by group, and the find box narrows it to the stations
// whose name or id holds the text typed.
import {button, el, field} from './dom.js';
import {logoPart} from './logo.js';
import {
    FOLD_FROM, addGroup, deleteGroup, deleteStation, duplicateStation, groupSummary, matchesStation,
    moveGroup, moveStation, renameGroup, setEnabled, setGroupImage, stationsByGroup,
} from './state.js';

// Which groups are open stays with the browser (per viewer, nothing the
// server needs); storage that is missing or refused just starts folded.
const OPEN_KEY = 'tunebox.admin.open-groups';
let query = '';

function loadOpen() {
    try {
        const stored = JSON.parse(window.localStorage.getItem(OPEN_KEY) || '[]');
        return new Set(Array.isArray(stored) ? stored.filter((name) => typeof name === 'string') : []);
    } catch (error) {
        return new Set();
    }
}

function storeOpen(open) {
    try {
        window.localStorage.setItem(OPEN_KEY, JSON.stringify([...open]));
    } catch (error) {
        // Kept for this page only.
    }
}

// The stations without a group are a group of their own here.
function groupKey(group) {
    return group.name === null ? '' : group.name;
}

// Own keys only: a group named like an Object property (__proto__,
// constructor) must not read one from the prototype.
function own(map, key) {
    return map && Object.prototype.hasOwnProperty.call(map, key) ? map[key] : null;
}

function clearedText(id) {
    return `Saved. The default station was ${id}; the player now starts on the first station.`;
}

// An inline confirmation in place of window.confirm, which blocks the page.
function confirmRow(question, label, onyes, oncancel) {
    return el('div', {class: 'confirm'},
        el('span', null, question),
        button('delete', label, onyes, {class: 'danger'}),
        button('close', 'Cancel', oncancel, {class: 'quiet'}));
}

function stationRow(ctx, row, peers, index, redraw) {
    const state = ctx.config.state;
    const name = row.doc.name || row.id;
    const isDefault = state.player.default_channel === row.id;
    const toggle = el('input', {
        type: 'checkbox',
        class: 'switch',
        checked: row.enabled,
        'aria-label': `${name} on`,
        onchange: (event) => {
            const result = setEnabled(state, row.id, event.target.checked);
            ctx.save(result.state, row.id, result.clearedDefault ? clearedText(row.id) : null);
        },
    });
    const actions = el('div', {class: 'actions'},
        button('arrow_upward', null, () => ctx.save(moveStation(state, row.id, -1), row.id),
            {title: `Move ${name} up`, disabled: index === 0}),
        button('arrow_downward', null, () => ctx.save(moveStation(state, row.id, 1), row.id),
            {title: `Move ${name} down`, disabled: index === peers.length - 1}),
        button('edit', null, () => ctx.go(`#edit/${row.id}`), {title: `Edit ${name}`}),
        button('content_copy', null, () => {
            // Off until edited: a copy would otherwise show up in the
            // player as a second, identical station.
            const result = duplicateStation(state, row.id);
            const copy = setEnabled(result.state, result.id, false).state;
            ctx.save(copy, result.id, `Saved as ${result.id}, switched off until you edit it.`);
        }, {title: `Duplicate ${name}`}),
        button('delete', null, () => redraw({confirm: row.id}), {title: `Delete ${name}`}));
    const item = el('li', {class: row.enabled ? 'station' : 'station off', 'data-id': row.id},
        el('label', {class: 'toggle'}, toggle),
        el('div', {class: 'who'},
            el('span', {class: 'name'}, name),
            el('span', {class: 'id'}, row.id, isDefault ? ' · default' : '', row.doc.needs_pin ? ' · PIN' : '', row.enabled ? '' : ' · off')),
        actions);
    if (redraw.state.confirm === row.id) {
        item.append(confirmRow(`Delete ${name}?`, 'Delete', () => {
            const result = deleteStation(state, row.id);
            ctx.save(result.state, row.id, result.clearedDefault ? clearedText(row.id) : null);
        }, () => redraw({})));
    }
    return item;
}

// The group's name and image, saved together: the rename goes first, so
// the image lands on the new name. A refused save draws the list again,
// as for every change on this page; the browser's own checks catch a
// malformed link before it is sent.
function groupForm(ctx, group, redraw) {
    const nameInput = el('input', {name: 'name', value: group.name, required: true});
    const imageInput = el('input', {name: 'image', type: 'url', value: own(ctx.config.state.group_images, group.name) || '',
        placeholder: 'https://', pattern: 'https://.+|blob:[0-9a-f]{64}'});
    const error = el('p', {class: 'message', role: 'alert', hidden: true});
    // The save the form asks for: {state, target, done}, or {problem}.
    function pendingChange() {
        const name = nameInput.value.trim();
        const current = ctx.config.state;
        if (!name) {
            return {problem: 'The group needs a name.'};
        }
        if (current.groups.indexOf(group.name) < 0) {
            return {problem: `The group ${group.name} was deleted elsewhere in the meantime; nothing was saved.`};
        }
        if (name !== group.name && current.groups.indexOf(name) >= 0) {
            return {problem: `There is already a group named ${name}.`};
        }
        const renamed = name === group.name ? current : renameGroup(current, group.name, name);
        return {state: setGroupImage(renamed, name, imageInput.value), target: name, done: `Saved group ${name}.`, keep: true};
    }
    const stored = own(ctx.config.state.group_images, group.name);
    const logo = logoPart(ctx, imageInput, () => {
        const change = pendingChange();
        return change.problem ? null : change;
    }, stored ? {value: stored, src: own(ctx.config.group_images, group.name)} : null);
    const form = el('form', {class: 'group-edit', onsubmit: async (event) => {
        event.preventDefault();
        const change = pendingChange();
        error.textContent = change.problem || '';
        error.hidden = !change.problem;
        if (!change.problem) {
            await ctx.save(change.state, change.target, change.done, undefined, true);
        }
    }},
    field('Name', nameInput),
    field('Image', imageInput, 'Downloaded and served by tunebox, or an image uploaded here. A PNG gives the lightest tiles.'),
    logo.controls,
    logo.preview,
    error,
    el('div', {class: 'actions'},
        el('button', {type: 'submit'}, 'Save'),
        button('close', 'Cancel', () => redraw({}), {class: 'quiet'})));
    return form;
}

function groupHeader(ctx, group, position, count, redraw, fold) {
    const state = ctx.config.state;
    if (group.name === null) {
        return el('div', {class: 'group-head'}, fold.toggle('Not in a group'));
    }
    if (redraw.state.group === group.name) {
        return groupForm(ctx, group, redraw);
    }
    const image = own(ctx.config.group_images, group.name);
    return el('div', {class: 'group-head'},
        image ? el('img', {class: 'group-image', src: image, alt: ''}) : null,
        fold.toggle(group.name),
        el('div', {class: 'actions'},
            button('arrow_upward', null, () => ctx.save(moveGroup(state, group.name, -1), group.name),
                {title: `Move group ${group.name} up`, disabled: position === 0}),
            button('arrow_downward', null, () => ctx.save(moveGroup(state, group.name, 1), group.name),
                {title: `Move group ${group.name} down`, disabled: position === count - 1}),
            button('edit', null, () => redraw({group: group.name}), {title: `Edit group ${group.name}`}),
            group.rows.length ? null : button('delete', null,
                () => ctx.save(deleteGroup(state, group.name), group.name),
                {title: `Delete group ${group.name}`})));
}

function addGroupForm(ctx) {
    const input = el('input', {placeholder: 'New group name', 'aria-label': 'New group name'});
    return el('form', {class: 'add-group', onsubmit: (event) => {
        event.preventDefault();
        const name = input.value.trim();
        if (!name) {
            return;
        }
        if (ctx.config.state.groups.indexOf(name) >= 0) {
            ctx.message(`There is already a group named ${name}.`);
            return;
        }
        ctx.save(addGroup(ctx.config.state, name), name);
    }}, input, el('button', {type: 'submit'}, 'Add group'));
}

export function render(root, ctx) {
    const redraw = (view) => {
        redraw.state = view;
        draw();
    };
    redraw.state = {};
    const open = loadOpen();
    function draw() {
        const state = ctx.config.state;
        const folding = state.channels.length >= FOLD_FROM;
        // A short list has no find box, so it has no search either: one
        // left from a longer list (a station deleted while finding) would
        // hide stations with nothing on the page to clear it.
        if (!folding) {
            query = '';
        }
        const groups = stationsByGroup(state);
        const named = groups.filter((group) => group.name !== null);
        const shown = groups.map((group) => {
            const key = groupKey(group);
            const id = `group-${groups.indexOf(group)}`;
            const summary = el('span', {class: 'hint group-summary'}, groupSummary(group.rows));
            const toggleButton = el('button', {type: 'button', class: 'fold', 'aria-controls': id});
            // The heading holds the button that folds the group, the
            // accessible way round (a heading inside a button is invalid).
            const fold = {
                toggle: (title) => {
                    if (!folding) {
                        return el('h2', null, title);
                    }
                    toggleButton.replaceChildren(el('span', {class: 'fold-mark', 'aria-hidden': 'true'}), title, summary);
                    toggleButton.addEventListener('click', () => {
                        if (open.has(key)) {
                            open.delete(key);
                        } else {
                            open.add(key);
                        }
                        storeOpen(open);
                        apply();
                    });
                    return el('h2', null, toggleButton);
                },
            };
            const rows = group.rows.map((row, index) => ({row, item: stationRow(ctx, row, group.rows, index, redraw)}));
            const body = group.rows.length
                ? el('ul', {class: 'stations', id}, rows.map((entry) => entry.item))
                : el('p', {class: 'hint', id}, 'No stations in this group.');
            const node = el('section', {class: 'group'},
                groupHeader(ctx, group, named.indexOf(group), named.length, redraw, fold), body);
            return {group, key, rows, body, node, summary, toggleButton};
        });
        // Folding and finding only show and hide what is drawn, so the find
        // box keeps its focus while typing.
        function apply() {
            const finding = query.trim() !== '';
            shown.forEach((entry) => {
                const matches = entry.rows.filter((r) => matchesStation(r.row, query));
                entry.rows.forEach((r) => { r.item.hidden = finding && matches.indexOf(r) < 0; });
                // A group being edited, or a station being deleted, keeps its group open.
                const busy = entry.rows.some((r) => r.row.id === redraw.state.confirm) || redraw.state.group === entry.group.name;
                const isOpen = !folding || busy || (finding ? matches.length > 0 : open.has(entry.key));
                entry.node.hidden = finding && matches.length === 0;
                entry.body.hidden = !isOpen;
                entry.summary.hidden = isOpen;
                entry.toggleButton.setAttribute('aria-expanded', String(isOpen));
                entry.node.classList.toggle('folded', !isOpen);
            });
            nothing.hidden = !finding || shown.some((entry) => !entry.node.hidden);
        }
        const nothing = el('p', {class: 'hint'}, 'No station name or id holds that text.');
        const find = el('input', {type: 'search', class: 'find', placeholder: 'Find a station', 'aria-label': 'Find a station',
            value: query, oninput: (event) => { query = event.target.value; apply(); }});
        const all = (opening) => () => {
            shown.forEach((entry) => {
                if (opening) {
                    open.add(entry.key);
                } else {
                    open.delete(entry.key);
                }
            });
            storeOpen(open);
            apply();
        };
        root.replaceChildren(...[
            el('div', {class: 'toolbar'},
                button('add', 'Add station', () => ctx.go('#new')),
                addGroupForm(ctx)),
            folding
                ? el('div', {class: 'toolbar'}, find,
                    el('button', {type: 'button', class: 'quiet', onclick: all(true)}, 'Open all'),
                    el('button', {type: 'button', class: 'quiet', onclick: all(false)}, 'Fold all'))
                : null,
            nothing,
            ...shown.map((entry) => entry.node),
        ].filter(Boolean));
        apply();
    }
    draw();
}
