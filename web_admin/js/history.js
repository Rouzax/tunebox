// Every change, newest first, with what it changed and a way back.
import {call} from './api.js';
import {button, el, when} from './dom.js';
import {diffStates} from './state.js';

const KINDS = {
    file: 'Taken from tunebox.toml',
    import: 'Imported from tunebox.toml',
    edit: 'Changed in the panel',
    restore: 'Restored',
};

function show(value) {
    return value === undefined ? '' : value;
}

function diffTable(changes) {
    if (!changes.length) {
        return el('p', {class: 'hint'}, 'No differences.');
    }
    return el('div', {class: 'scroll'}, el('table', {class: 'diff'},
        el('thead', null, el('tr', null, el('th', null, 'What'), el('th', null, 'Before'), el('th', null, 'After'))),
        el('tbody', null, changes.map((change) => el('tr', null,
            el('td', null, change.path),
            el('td', null, show(change.before)),
            el('td', null, show(change.after)))))));
}

async function list(root, ctx) {
    const items = el('ul', {class: 'history'});
    const more = button('history', 'Older', null, {class: 'quiet', hidden: true});
    let last = null;
    async function page() {
        const {ok, data} = await call('GET', last === null ? 'history' : `history?before=${last}`);
        if (!ok) {
            ctx.fail(data);
            return;
        }
        for (const entry of data.entries) {
            items.append(el('li', null, el('a', {href: `#history/${entry.id}`},
                el('span', {class: 'when'}, when(entry.at)),
                el('span', null, KINDS[entry.kind] || entry.kind),
                entry.target ? el('span', {class: 'id'}, entry.target) : null)));
            last = entry.id;
        }
        more.hidden = !data.more;
        if (!items.children.length) {
            items.append(el('li', {class: 'hint'}, 'No changes yet.'));
        }
    }
    more.addEventListener('click', () => page().catch(() => ctx.message('Could not reach tunebox.')));
    root.replaceChildren(el('h2', null, 'History'), items, more);
    await page();
}

async function detail(root, ctx, id) {
    const {ok, data} = await call('GET', `history/${id}`);
    if (!ok) {
        root.replaceChildren(el('p', null, 'That change is not in the history.'),
            button('history', 'All changes', () => ctx.go('#history'), {class: 'quiet'}));
        return;
    }
    const confirmBox = el('div', {class: 'restore'});
    const offer = button('undo', 'Restore the version before this change', async () => {
        // Compared with what is stored now, not with this entry: other
        // changes since then are undone too, and the page says which.
        await ctx.reload();
        const changes = diffStates(ctx.config.state, data.before);
        confirmBox.replaceChildren(
            el('p', null, changes.length ? 'Restoring changes this:' : 'The stations are already like this.'),
            diffTable(changes),
            changes.length ? el('div', {class: 'actions'},
                button('undo', 'Restore', async () => {
                    const result = await ctx.restore(data.entry.id);
                    if (result.ok) {
                        ctx.go('#history', true);
                    }
                }, {class: 'danger'}),
                button('close', 'Cancel', () => confirmBox.replaceChildren(), {class: 'quiet'})) : null);
    });
    root.replaceChildren(
        el('h2', null, KINDS[data.entry.kind] || data.entry.kind),
        el('p', {class: 'hint'}, when(data.entry.at), data.entry.target ? ` · ${data.entry.target}` : ''),
        diffTable(diffStates(data.before, data.after)),
        el('div', {class: 'actions'},
            offer,
            button('history', 'All changes', () => ctx.go('#history'), {class: 'quiet'})),
        confirmBox);
}

export function render(root, ctx, id) {
    const work = id ? detail(root, ctx, id) : list(root, ctx);
    work.catch(() => ctx.message('Could not reach tunebox.'));
}
