// The station list as data: pure functions that return a new state. The
// page renders from these and sends the whole state on save.

const clone = (value) => JSON.parse(JSON.stringify(value));

export function validId(id) {
    return /^[a-z0-9][a-z0-9-]*$/.test(id);
}

export function newStation(id, name, group) {
    return {
        id,
        group,
        enabled: true,
        doc: {
            id,
            name,
            source: {type: 'triton', mount: ''},
            streams: [{name: 'Default', url: 'https://', codec: 'aac', kbps: 128}],
        },
    };
}

function rank(state, group) {
    return group === null ? -1 : state.groups.indexOf(group);
}

export function stationsByGroup(state) {
    const names = [null, ...state.groups];
    return names
        .map((name) => ({name, rows: state.channels.filter((row) => row.group === name)}))
        .filter((group) => group.name !== null || group.rows.length);
}

// Rows are kept in listing order, so the stored positions match the page.
function ordered(state) {
    const channels = state.channels
        .map((row, index) => ({row, index}))
        .sort((x, y) => rank(state, x.row.group) - rank(state, y.row.group) || x.index - y.index)
        .map((item) => item.row);
    return {...state, channels};
}

export function addStation(state, row) {
    return ordered({...clone(state), channels: [...clone(state.channels), clone(row)]});
}

// A station that changes group goes to the end of its new group.
export function replaceStation(state, oldId, row) {
    const next = clone(state);
    const old = next.channels.find((item) => item.id === oldId);
    if (old && old.group !== row.group) {
        next.channels = [...next.channels.filter((item) => item !== old), clone(row)];
    } else {
        next.channels = next.channels.map((item) => (item.id === oldId ? clone(row) : item));
    }
    return ordered(next);
}

function freeId(state, wanted) {
    const taken = new Set(state.channels.map((row) => row.id));
    if (!taken.has(wanted)) {
        return wanted;
    }
    let n = 2;
    while (taken.has(`${wanted}-${n}`)) {
        n += 1;
    }
    return `${wanted}-${n}`;
}

export function duplicateStation(state, id) {
    const next = clone(state);
    const index = next.channels.findIndex((row) => row.id === id);
    const copy = clone(next.channels[index]);
    copy.id = freeId(next, `${id}-copy`);
    copy.doc.id = copy.id;
    copy.doc.name = `${copy.doc.name} (copy)`;
    next.channels.splice(index + 1, 0, copy);
    return {state: next, id: copy.id};
}

function withoutDefault(state, id) {
    if (state.player.default_channel !== id) {
        return {state, clearedDefault: false};
    }
    const player = {...state.player};
    delete player.default_channel;
    return {state: {...state, player}, clearedDefault: true};
}

export function deleteStation(state, id) {
    const next = clone(state);
    next.channels = next.channels.filter((row) => row.id !== id);
    return withoutDefault(next, id);
}

export function setEnabled(state, id, enabled) {
    const next = clone(state);
    next.channels.find((row) => row.id === id).enabled = enabled;
    return enabled ? {state: next, clearedDefault: false} : withoutDefault(next, id);
}

export function moveStation(state, id, step) {
    const next = clone(state);
    const row = next.channels.find((item) => item.id === id);
    const peers = next.channels.filter((item) => item.group === row.group);
    const at = peers.indexOf(row);
    const other = peers[at + step];
    if (!other) {
        return next;
    }
    const i = next.channels.indexOf(row);
    const j = next.channels.indexOf(other);
    next.channels[i] = other;
    next.channels[j] = row;
    return next;
}

export function addGroup(state, name) {
    const next = clone(state);
    if (name && next.groups.indexOf(name) < 0) {
        next.groups.push(name);
    }
    return next;
}

export function renameGroup(state, old, name) {
    const next = clone(state);
    next.groups = next.groups.map((group) => (group === old ? name : group));
    next.channels.forEach((row) => {
        if (row.group === old) {
            row.group = name;
        }
    });
    return next;
}

export function deleteGroup(state, name) {
    const next = clone(state);
    if (!next.channels.some((row) => row.group === name)) {
        next.groups = next.groups.filter((group) => group !== name);
    }
    return next;
}

export function moveGroup(state, name, step) {
    const next = clone(state);
    const at = next.groups.indexOf(name);
    const to = at + step;
    if (at < 0 || to < 0 || to >= next.groups.length) {
        return next;
    }
    next.groups.splice(at, 1);
    next.groups.splice(to, 0, name);
    return ordered(next);
}

function leaves(value, path, out) {
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
        for (const key of Object.keys(value)) {
            leaves(value[key], path ? `${path}.${key}` : key, out);
        }
    } else {
        out.set(path, JSON.stringify(value));
    }
    return out;
}

// Stations keyed by id, so a moved station is not reported as changed
// everywhere; groups and streams compare as whole lists.
function keyed(state) {
    const channels = {};
    for (const row of state.channels) {
        channels[row.id] = row;
    }
    return {defaults: state.defaults, player: state.player, groups: state.groups, channels};
}

export function diffStates(before, after) {
    const kb = keyed(before);
    const ka = keyed(after);
    const a = leaves(kb, '', new Map());
    const b = leaves(ka, '', new Map());
    const paths = new Set([...a.keys(), ...b.keys()]);
    const changes = [];
    for (const path of paths) {
        if (a.get(path) !== b.get(path)) {
            changes.push({path, before: a.get(path), after: b.get(path)});
        }
    }
    // A whole station added or removed reads better as one line.
    const whole = new Set();
    for (const change of changes) {
        const id = change.path.split('.')[1];
        if (change.path.startsWith('channels.') && (!kb.channels[id] || !ka.channels[id])) {
            whole.add(id);
        }
    }
    const rest = changes.filter((c) => !(c.path.startsWith('channels.') && whole.has(c.path.split('.')[1])));
    for (const id of whole) {
        rest.push({
            path: `channels.${id}`,
            before: kb.channels[id] ? 'present' : undefined,
            after: ka.channels[id] ? 'present' : undefined,
        });
    }
    // Keyed by id, a reorder has no changed leaf of its own.
    const order = (state) => JSON.stringify(state.channels.map((row) => row.id));
    if (order(before) !== order(after) && !whole.size) {
        rest.push({path: 'order', before: order(before), after: order(after)});
    }
    return rest.sort((x, y) => (x.path < y.path ? -1 : 1));
}

// The state with new defaults and player settings. An empty default falls
// back to tunebox's own; the location goes in only as a pair (the page
// refuses half of one before it gets here).
export function setSettings(state, values) {
    const defaults = {};
    for (const [key, value] of Object.entries(values.defaults)) {
        if (value !== undefined) {
            defaults[key] = value;
        }
    }
    const player = {};
    if (values.latitude !== undefined && values.longitude !== undefined) {
        player.latitude = values.latitude;
        player.longitude = values.longitude;
    }
    if (values.defaultChannel) {
        player.default_channel = values.defaultChannel;
    }
    return {defaults, player, groups: state.groups, channels: state.channels};
}

// A hash part as text; null when it is not valid percent-encoding, so a
// hand-typed address cannot throw out of the router.
export function decodeHashPart(text) {
    try {
        return decodeURIComponent(text);
    } catch (error) {
        if (error instanceof URIError) {
            return null;
        }
        throw error;
    }
}

// What a successful save tells the user. A stored change that is not live
// says so also when this save added nothing (a re-save retries the apply);
// otherwise nothing stored when the state equalled the stored one.
export function saveMessage(data, done) {
    if (data.applied === false) {
        return data.changed === false
            ? 'Nothing new to save, but the stored change is still not live. The log says why.'
            : 'Saved, but not live yet: tunebox could not start the change. The log says why.';
    }
    if (data.changed === false) {
        return 'No changes to save.';
    }
    return done;
}

// Every route takes a ticket; a page that draws after waiting for an answer
// draws only while its ticket is the latest. A slow answer for a page the
// owner has left must not draw over the page now open.
export function tickets() {
    let latest = 0;
    return {
        take() {
            latest += 1;
            const mine = latest;
            return () => mine === latest;
        },
    };
}

const BLOB = /^blob:([0-9a-f]{64})$/;

// Where the Logo field's value can be shown from. The panel's CSP allows
// images from itself only, so an uploaded logo is drawn from the panel and
// a remote URL is not drawn at all.
export function logoPreview(value) {
    const blob = BLOB.exec(value);
    if (blob) {
        return {kind: 'blob', src: `/admin/api/logos/${blob[1]}`};
    }
    if (/^https?:\/\//.test(value)) {
        return {kind: 'url', src: null};
    }
    return {kind: 'none', src: null};
}

// The lines above an import's differences.
export function importSummary(changes, notes) {
    let first = 'The file matches what is stored now.';
    if (changes.length) {
        first = `${changes.length} ${changes.length === 1 ? 'difference' : 'differences'} with what is stored now.`;
    }
    return [first, ...notes];
}

const HISTORY_LABELS = {
    file: 'Taken from tunebox.toml',
    edit: 'Changed in the panel',
    restore: 'Restored',
};

// An import from the panel names a file; the first-start import has no target.
// Where an import came from is in its label; any other change names the
// station or page it touched.
export function historyTarget(kind, target) {
    return kind === 'import' ? null : target;
}

// The name the server gave an exported file, for saving it.
export function exportName(disposition) {
    const found = /filename="([^"]+)"/.exec(disposition || '');
    return found ? found[1] : 'tunebox.toml';
}

export function historyLabel(kind, target) {
    if (kind === 'import') {
        return target === 'panel' ? 'Imported from a file' : 'Imported from tunebox.toml';
    }
    return HISTORY_LABELS[kind] || kind;
}
