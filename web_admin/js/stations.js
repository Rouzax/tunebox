// The station list: one section per group, every change saved at once.
import {button, el} from './dom.js';
import {
    addGroup, deleteGroup, deleteStation, duplicateStation, moveGroup, moveStation, renameGroup,
    setEnabled, stationsByGroup,
} from './state.js';

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

function groupHeader(ctx, group, position, count, redraw) {
    const state = ctx.config.state;
    if (group.name === null) {
        return el('h2', null, 'Not in a group');
    }
    if (redraw.state.rename === group.name) {
        const input = el('input', {value: group.name, 'aria-label': 'Group name', required: true});
        return el('form', {class: 'rename', onsubmit: (event) => {
            event.preventDefault();
            const name = input.value.trim();
            if (name && name !== group.name) {
                ctx.save(renameGroup(state, group.name, name), name);
            } else {
                redraw({});
            }
        }}, input,
        el('button', {type: 'submit'}, 'Rename'),
        button('close', 'Cancel', () => redraw({}), {class: 'quiet'}));
    }
    return el('div', {class: 'group-head'},
        el('h2', null, group.name),
        el('div', {class: 'actions'},
            button('arrow_upward', null, () => ctx.save(moveGroup(state, group.name, -1), group.name),
                {title: `Move group ${group.name} up`, disabled: position === 0}),
            button('arrow_downward', null, () => ctx.save(moveGroup(state, group.name, 1), group.name),
                {title: `Move group ${group.name} down`, disabled: position === count - 1}),
            button('edit', null, () => redraw({rename: group.name}), {title: `Rename group ${group.name}`}),
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
    function draw() {
        const groups = stationsByGroup(ctx.config.state);
        const named = groups.filter((group) => group.name !== null);
        const sections = groups.map((group) => el('section', {class: 'group'},
            groupHeader(ctx, group, named.indexOf(group), named.length, redraw),
            group.rows.length
                ? el('ul', {class: 'stations'},
                    group.rows.map((row, index) => stationRow(ctx, row, group.rows, index, redraw)))
                : el('p', {class: 'hint'}, 'No stations in this group.')));
        root.replaceChildren(
            el('div', {class: 'toolbar'},
                button('add', 'Add station', () => ctx.go('#new')),
                addGroupForm(ctx)),
            ...sections);
    }
    draw();
}
