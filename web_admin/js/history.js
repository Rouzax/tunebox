// Every change, newest first, with what it changed and a way back.
import {askCode, button, el, icon, when} from './dom.js';
import {diffStates, importSummary, tickets} from './state.js';

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

// A click handler whose network failure becomes a message, not nothing.
// A refused reload (ctx.reload throws 'config') has already said why.
function guarded(ctx, handler) {
    return () => handler().catch((error) => {
        if (error.message !== 'config') {
            ctx.message('Could not reach tunebox.');
        }
    });
}

// Import: a file is read by the server, shown as differences with what is
// stored, and stored only after a click (and a fresh code when the server
// wants one). The page never reads the file itself.
function importPart(ctx, refreshList) {
    const current = ctx.stillOpen;
    const area = el('div', {class: 'restore'});
    const chooser = el('input', {type: 'file', name: 'import-file', accept: '.toml,text/plain,application/toml', hidden: true});
    const codeArea = el('div');

    function cancel() {
        area.replaceChildren();
    }

    function showPreview(toml, previewed, extraNote) {
        const changes = diffStates(ctx.config.state, previewed.state);
        const store = guarded(ctx, async () => {
            let result = await ctx.importFile(toml, previewed.state);
            // A code that went stale before the import arrived asks again.
            while (result.reauth && current() && await askCode(ctx, codeArea)) {
                result = await ctx.importFile(toml, previewed.state);
            }
            if (!current()) {
                return;
            }
            if (result.stale) {
                refreshList();
                showPreview(toml, previewed, 'The stations changed in the meantime. Check the differences again.');
            }
        });
        // replaceChildren turns null and arrays into text, so the parts
        // are listed flat.
        area.replaceChildren(
            ...(extraNote ? [el('p', {class: 'message', role: 'alert'}, extraNote)] : []),
            ...importSummary(changes, previewed.notes || []).map((line) => el('p', null, line)),
            diffTable(changes),
            el('div', {class: 'actions'},
                button('arrow_upward', 'Replace the stations with this file', store, {class: 'danger'}),
                button('close', 'Cancel', cancel, {class: 'quiet'})),
            codeArea);
    }

    const choose = guarded(ctx, async () => {
        const file = chooser.files[0];
        // Cleared at once so choosing the same file again (after fixing it)
        // fires a change; the File stays readable.
        chooser.value = '';
        if (!file) {
            return;
        }
        const toml = await file.text();
        const answer = await ctx.request('POST', 'import/preview', {toml});
        if (!current()) {
            return;
        }
        if (answer.status === 401) {
            return;
        }
        if (answer.status === 400 && answer.data.error === 'invalid') {
            area.replaceChildren(el('p', {class: 'message', role: 'alert'}, answer.data.message),
                button('close', 'Cancel', cancel, {class: 'quiet'}));
        } else if (answer.ok) {
            showPreview(toml, answer.data);
        } else {
            ctx.fail(answer.data);
        }
    });
    chooser.addEventListener('change', choose);
    return el('div', {class: 'transfer'},
        el('div', {class: 'actions'},
            el('a', {class: 'button quiet', href: '/admin/api/export', download: ''}, icon('arrow_downward'), ' Export'),
            button('arrow_upward', 'Import', () => chooser.click(), {class: 'quiet'}),
            chooser),
        area);
}

async function list(root, ctx) {
    const current = ctx.stillOpen;
    const items = el('ul', {class: 'history'});
    const more = button('history', 'Older', null, {class: 'quiet', hidden: true});
    let last = null;
    // Only the latest list request draws: a refresh empties the list while
    // an older page may still be on its way.
    const take = tickets();
    async function page() {
        const mine = take.take();
        const {ok, status, data} = await ctx.request('GET', last === null ? 'history' : `history?before=${last}`);
        if (!current() || !mine()) {
            return;
        }
        if (!ok) {
            if (status !== 401) {
                ctx.fail(data);
            }
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
    function refresh() {
        items.replaceChildren();
        last = null;
        page().catch(() => ctx.message('Could not reach tunebox.'));
    }
    root.replaceChildren(el('h2', null, 'History'), importPart(ctx, refresh), items, more);
    await page();
}

async function detail(root, ctx, id) {
    const current = ctx.stillOpen;
    const {ok, status, data} = await ctx.request('GET', `history/${id}`);
    if (!current()) {
        return;
    }
    if (status === 401) {
        return;
    }
    if (status === 404) {
        root.replaceChildren(el('p', null, 'That change is not in the history.'),
            button('history', 'All changes', () => ctx.go('#history'), {class: 'quiet'}));
        return;
    }
    if (!ok) {
        ctx.fail(data);
        return;
    }
    const confirmBox = el('div', {class: 'restore'});
    const offer = button('undo', 'Restore the version before this change', guarded(ctx, async () => {
        // Compared with what is stored now, not with this entry: other
        // changes since then are undone too, and the page says which.
        await ctx.reload();
        const changes = diffStates(ctx.config.state, data.before);
        confirmBox.replaceChildren(
            el('p', null, changes.length ? 'Restoring changes this:' : 'The stations are already like this.'),
            diffTable(changes),
            changes.length ? el('div', {class: 'actions'},
                button('undo', 'Restore', guarded(ctx, async () => {
                    const result = await ctx.restore(data.entry.id, data.before);
                    if (result.ok) {
                        ctx.go('#history', true);
                    }
                }), {class: 'danger'}),
                button('close', 'Cancel', () => confirmBox.replaceChildren(), {class: 'quiet'})) : null);
    }));
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
