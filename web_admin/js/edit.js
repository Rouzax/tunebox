// The station form, for a new station and for an existing one.
import {button, el, field, section, withHelp} from './dom.js';
import {HELP} from './help.js';
import {addStation, logoPreview, newStation, replaceStation, validId} from './state.js';

const NUMBERS = {
    delay_s: ['Delay (seconds)', 'How far the audio runs behind the feed.'],
    pawtunes_delay_s: ['PawTunes delay (seconds)', null],
    poll_interval_s: ['Poll interval (seconds)', null],
    history_size: ['History size', 'Tracks kept in the recent list.'],
};
const SOURCE_LABELS = {mount: 'Mount', station: 'Qmusic station', site: 'NPO site'};
const RULES = {
    station_id: ['Station-ID pattern', 'Titles matching this are jingles, not tracks.'],
    played_by: ['Played-by pattern', 'Needs a group: the part to keep, e.g. \\s*\\(Played by ([^)]*)\\)\\s*$'],
    badge: ['Badge pattern', 'Needs a group: the first group that matches is shown before the title, and the match leaves it, e.g. ^(#\\d+)\\s+ for a chart position.'],
    artist_separator: ['Artist separator', 'Between artists in the feed; a comma when empty.'],
};

function text(name, value, extra) {
    return el('input', Object.assign({name, value: value === undefined || value === null ? '' : String(value)}, extra || {}));
}

function select(name, options, value, extra) {
    return el('select', Object.assign({name}, extra || {}),
        options.map(([optionValue, label]) => el('option', {value: optionValue, selected: optionValue === value}, label)));
}

// Streams as edited: the stored doc has either stream_url (one stream,
// http allowed) or a streams list.
function streamsOf(doc) {
    if (doc.stream_url) {
        return [{name: 'Default', url: doc.stream_url}];
    }
    return (doc.streams || []).map((stream) => Object.assign({}, stream));
}

function streamsTo(doc, streams, hadUrl) {
    const single = streams.length === 1 && streams[0].name === 'Default' && !streams[0].codec && !streams[0].kbps;
    if (hadUrl && single) {
        doc.stream_url = streams[0].url;
        return;
    }
    doc.streams = streams.map((stream) => {
        const out = {name: stream.name, url: stream.url};
        if (stream.codec) {
            out.codec = stream.codec;
        }
        if (stream.kbps) {
            out.kbps = stream.kbps;
        }
        return out;
    });
}

function streamEditor(schema, streams) {
    const box = el('div', {class: 'streams'});
    const codecs = [['', 'unknown'], ...schema.codecs.map((codec) => [codec, codec])];
    function draw() {
        box.replaceChildren(...streams.map((stream, index) => el('fieldset', {class: 'stream'},
            el('legend', null, `Stream ${index + 1}`),
            field('Name', text('', stream.name, {required: true, oninput: (e) => { stream.name = e.target.value; }})),
            field('URL', text('', stream.url, {type: 'url', required: true, oninput: (e) => { stream.url = e.target.value.trim(); }})),
            field('Codec', select('', codecs, stream.codec || '', {onchange: (e) => { stream.codec = e.target.value || undefined; }}), null, HELP.codec),
            field('kbps', text('', stream.kbps, {type: 'number', min: 1, step: 1,
                oninput: (e) => { stream.kbps = e.target.value ? Number(e.target.value) : undefined; }}), null, HELP.kbps),
            el('div', {class: 'actions'},
                button('arrow_upward', null, () => { streams.splice(index - 1, 0, streams.splice(index, 1)[0]); draw(); },
                    {title: `Move stream ${index + 1} up`, disabled: index === 0}),
                button('arrow_downward', null, () => { streams.splice(index + 1, 0, streams.splice(index, 1)[0]); draw(); },
                    {title: `Move stream ${index + 1} down`, disabled: index === streams.length - 1}),
                button('delete', null, () => { streams.splice(index, 1); draw(); },
                    {title: `Remove stream ${index + 1}`, disabled: streams.length === 1})))),
        button('add', 'Add stream', () => {
            streams.push({name: `Stream ${streams.length + 1}`, url: 'https://'});
            draw();
        }, {class: 'quiet'}));
    }
    draw();
    return box;
}

function sourceEditor(schema, source) {
    const box = el('div', {class: 'source'});
    const types = Object.keys(schema.sources).map((type) => [type, type]);
    const settings = Object.assign({}, source);
    function draw() {
        const type = settings.type;
        const inputs = (schema.sources[type] || []).map((key) => {
            const choices = (schema.choices[type] || {})[key];
            const onchange = (e) => { settings[key] = e.target.value.trim(); };
            if (!choices && settings[key] === undefined) {
                settings[key] = '';
            }
            if (choices && choices.indexOf(settings[key]) < 0) {
                settings[key] = choices[0];
            }
            const input = choices
                ? select(`source.${key}`, choices.map((c) => [c, c]), settings[key], {onchange})
                : text(`source.${key}`, settings[key], {required: true, oninput: onchange});
            return field(SOURCE_LABELS[key] || key, input, null, HELP[`source.${key}`]);
        });
        box.replaceChildren(
            field('Now-playing source', select('source.type', types, type, {onchange: (e) => {
                Object.keys(settings).forEach((key) => { delete settings[key]; });
                settings.type = e.target.value;
                draw();
                box.dispatchEvent(new Event('input', {bubbles: true}));
            }}), null, HELP['source.type']),
            ...inputs);
    }
    draw();
    return {box, value: () => {
        const out = {type: settings.type};
        (schema.sources[settings.type] || []).forEach((key) => { out[key] = settings[key]; });
        return out;
    }};
}

// The types the server accepts for an upload; the image itself is judged by
// its bytes there.
const LOGO_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/svg+xml'];

// An uploaded logo is shown from the panel at the size of the page and of a
// list row; a URL is only downloaded after saving. kept() gives the form's
// unsaved change, which an upload that meets an ended session hands to the
// login so it can be offered again.
function logoPart(ctx, input, kept) {
    const current = ctx.stillOpen;
    const preview = el('div', {class: 'logo-preview'});
    const picker = el('input', {type: 'file', name: 'logo-file', hidden: true,
        accept: LOGO_TYPES.join(',')});
    const note = el('span', {class: 'hint', role: 'alert'});
    function draw() {
        const shown = logoPreview(input.value.trim());
        if (shown.kind === 'blob') {
            preview.replaceChildren(
                el('img', {class: 'logo-full', src: shown.src, alt: 'Logo'}),
                el('img', {class: 'logo-small', src: shown.src, alt: 'Logo at list size'}));
        } else if (shown.kind === 'url') {
            preview.replaceChildren(el('span', {class: 'hint'}, 'Shown after saving.'));
        } else {
            preview.replaceChildren();
        }
    }
    input.addEventListener('input', draw);
    input.addEventListener('change', draw);
    picker.addEventListener('change', async () => {
        const file = picker.files[0];
        picker.value = '';
        if (!file) {
            return;
        }
        if (!LOGO_TYPES.includes(file.type)) {
            note.textContent = 'Upload a PNG, JPEG, WebP or SVG image.';
            return;
        }
        if (file.size > ctx.limits.logo_max) {
            note.textContent = `The image is larger than ${ctx.limits.logo_max / 1000000} MB.`;
            return;
        }
        note.textContent = '';
        let answer;
        try {
            answer = await ctx.upload(file, kept);
        } catch (error) {
            note.textContent = 'Could not reach tunebox.';
            return;
        }
        if (!current()) {
            return;
        }
        if (answer.status === 401) {
            return;
        }
        if (!answer.ok) {
            ctx.fail(answer.data);
            return;
        }
        input.value = answer.data.logo;
        draw();
    });
    draw();
    return {
        preview,
        controls: el('div', {class: 'actions'}, button('arrow_upward', 'Upload', () => picker.click(), {class: 'quiet'}), picker, note),
    };
}

export function render(root, ctx, id) {
    const {state, schema} = ctx.config;
    const existing = id ? state.channels.find((row) => row.id === id) : null;
    if (id && !existing) {
        root.replaceChildren(el('p', null, `There is no station ${id}.`),
            button('close', 'Back to the stations', () => ctx.go('#stations')));
        return;
    }
    const row = existing || newStation('', '', null);
    const doc = row.doc;
    const streams = streamsOf(doc);
    const source = sourceEditor(schema, doc.source || {type: 'triton'});
    const error = el('p', {class: 'message', role: 'alert', hidden: true});
    const groups = [['', 'None'], ...state.groups.map((name) => [name, name])];
    const rules = doc.rules || {};
    const logoInput = text('logo', doc.logo, {type: 'url', placeholder: 'https://'});
    const logo = logoPart(ctx, logoInput, () => {
        const change = pendingChange();
        return change.problem ? null : change;
    });
    // Folded: the parts a station rarely changes after it is set up. A new
    // station still needs its feed, so Now playing starts open there.
    const sections = {
        basics: section('Station', true),
        streams: section('Streams', true),
        source: section('Now playing', !existing),
        rules: section('Title rules', false),
        timing: section('Timing', false),
        options: section('Options', false),
    };
    sections.basics.node.append(
        field('Name', text('name', doc.name, {required: true}), null, HELP.name),
        existing
            ? withHelp(el('p', {class: 'hint'}, `Id: ${row.id} (fixed; duplicate the station to change it)`), HELP.id, 'the id')
            : field('Id', text('id', '', {required: true, pattern: '[a-z0-9][a-z0-9\\-]*', autocapitalize: 'off'}),
                'Lower-case letters, digits and -; used in links, fixed once saved.', HELP.id),
        field('Group', select('group', groups, row.group || ''), null, HELP.group),
        field('Logo URL', logoInput, 'Downloaded and served by tunebox, or an image uploaded here.', HELP.logo),
        logo.controls,
        logo.preview,
        field('Default image URL', text('default_image', doc.default_image, {type: 'url'}), 'Shown when no cover is found.', HELP.default_image));
    sections.streams.node.append(
        withHelp(el('p', {class: 'hint'}, 'The quality choices listeners see; the first is the default.'), HELP.streams, 'streams'),
        streamEditor(schema, streams));
    sections.source.node.append(source.box);
    sections.rules.node.append(...Object.keys(RULES).map((key) => field(RULES[key][0],
        text(`rules.${key}`, rules[key], {autocapitalize: 'off', spellcheck: 'false'}), RULES[key][1], HELP[`rules.${key}`])));
    sections.timing.node.append(...Object.keys(schema.defaults).map((key) => {
        const [label, hint] = NUMBERS[key] || [key, null];
        return field(label, text(key, doc[key], {type: 'number', step: key === 'history_size' ? 1 : 'any', min: 0,
            placeholder: `${(state.defaults || {})[key] !== undefined ? state.defaults[key] : schema.defaults[key]} (default)`}), hint, HELP[key]);
    }));
    sections.options.node.append(
        withHelp(el('label', {class: 'check'}, el('input', {type: 'checkbox', name: 'always', checked: Boolean(doc.always)}),
            'Always poll the feed, also when nobody listens'), HELP.always, 'always polling'),
        withHelp(el('label', {class: 'check'}, el('input', {type: 'checkbox', name: 'needs_pin', checked: Boolean(doc.needs_pin)}),
            'Needs the PIN: hidden until a device types it, when the PIN asks on chosen stations. Unticking it asks for a code from your authenticator app.'),
        HELP.needs_pin, 'needs the PIN'));
    const form = el('form', {class: 'edit', novalidate: true},
        el('h2', null, existing ? `Edit ${doc.name || id}` : 'New station'),
        Object.keys(sections).map((key) => sections[key].node),
        error,
        el('div', {class: 'actions'},
            el('button', {type: 'submit'}, 'Save'),
            button('close', 'Cancel', () => ctx.go('#stations'), {class: 'quiet'})));

    // What a folded section holds, in its header.
    function describe() {
        const values = form.elements;
        const setting = source.value();
        const detail = Object.keys(setting).filter((key) => key !== 'type').map((key) => setting[key]).filter(Boolean);
        sections.source.status([setting.type, ...detail].join(', '));
        sections.streams.status(streams.length === 1 ? '1 stream' : `${streams.length} streams`);
        const ruleCount = Object.keys(RULES).filter((key) => values[`rules.${key}`].value).length;
        sections.rules.status(ruleCount ? `${ruleCount} set` : 'none set');
        const changed = Object.keys(schema.defaults).filter((key) => values[key].value !== '').length;
        sections.timing.status(changed ? `${changed} changed` : 'defaults');
        const options = [values.always.checked ? 'always polls' : null, values.needs_pin.checked ? 'needs the PIN' : null].filter(Boolean);
        sections.options.status(options.length ? options.join(', ') : 'none');
    }
    form.addEventListener('input', describe);
    form.addEventListener('change', describe);
    form.addEventListener('click', describe);

    // A problem in a folded section opens it: the fields the browser finds
    // invalid, and the section a message from the server names.
    const NAMED = [['source', 'source'], ['rules', 'rules'], ['stream', 'streams'],
        ['delay', 'timing'], ['poll_interval', 'timing'], ['history_size', 'timing']];
    function reveal(message) {
        // The server names the field it refused, as in
        // "channels[3] (owr).rules.played_by: ...": the form's own field names.
        const path = /channels\[\d+\](?: \([^)]*\))?\.([\w.]+)/.exec(message || '');
        const named = path && form.elements.namedItem(path[1]);
        const invalid = named instanceof HTMLElement && named.name ? named : form.querySelector(':invalid');
        Object.keys(sections).forEach((key) => {
            const node = sections[key].node;
            if (node.contains(invalid) || node.querySelector(':invalid') || NAMED.some(([word, target]) => target === key && (message || '').indexOf(word) >= 0)) {
                node.open = true;
            }
        });
        if (invalid) {
            invalid.focus();
        }
    }

    // The station as Save would store it, from what the form holds now.
    function collect() {
        const values = form.elements;
        const newId = existing ? row.id : values.id.value.trim();
        const out = {id: newId, name: values.name.value.trim(), source: source.value()};
        streamsTo(out, streams, Boolean(doc.stream_url));
        ['logo', 'default_image'].forEach((key) => {
            if (values[key].value.trim()) {
                out[key] = values[key].value.trim();
            }
        });
        if (values.always.checked) {
            out.always = true;
        }
        if (values.needs_pin.checked) {
            out.needs_pin = true;
        }
        const outRules = {};
        Object.keys(RULES).forEach((key) => {
            const value = values[`rules.${key}`].value;
            if (value) {
                outRules[key] = value;
            }
        });
        if (Object.keys(outRules).length) {
            out.rules = outRules;
        }
        Object.keys(schema.defaults).forEach((key) => {
            if (values[key].value !== '') {
                out[key] = Number(values[key].value);
            }
        });
        return {id: newId, group: values.group.value || null, enabled: existing ? row.enabled : true, doc: out};
    }

    const question = el('div', {class: 'confirm', role: 'alert'});
    let opened = null;
    ctx.unsaved = {
        dirty: () => JSON.stringify(collect()) !== opened,
        ask(proceed) {
            const stay = button('edit', 'Keep editing', () => question.remove());
            question.replaceChildren(
                el('span', null, 'This station has changes that are not saved.'),
                button('close', 'Leave without saving', proceed, {class: 'danger'}),
                stay);
            form.querySelector('h2').after(question);
            stay.focus();
        },
    };

    // The save the form asks for: {state, target, done}, or {problem} with
    // the reason it cannot be made.
    function pendingChange() {
        const next = collect();
        const newId = next.id;
        if (!existing && !validId(newId)) {
            return {problem: 'The id needs lower-case letters, digits and - only, and must not start with -.'};
        }
        // The stored state at the time of saving, which a stale answer
        // reloads in between.
        const current = ctx.config.state;
        if (existing && !current.channels.some((item) => item.id === row.id)) {
            return {problem: `${row.id} was deleted elsewhere in the meantime; nothing was saved.`};
        }
        if (!existing && current.channels.some((item) => item.id === newId)) {
            return {problem: `There is already a station with the id ${newId}.`};
        }
        return {
            state: existing ? replaceStation(current, row.id, next) : addStation(current, next),
            target: newId,
            done: `Saved ${next.doc.name}.`,
        };
    }

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const show = (textValue) => {
            error.textContent = textValue;
            error.hidden = !textValue;
        };
        const change = pendingChange();
        if (change.problem) {
            show(change.problem);
            reveal(change.problem);
            return;
        }
        show('');
        const result = await ctx.save(change.state, change.target, change.done);
        if (result.ok) {
            ctx.unsaved = null;
            ctx.go('#stations', true);
        } else if (result.message) {
            show(result.message);
            reveal(result.message);
        }
    });
    root.replaceChildren(form);
    describe();
    opened = JSON.stringify(collect());
    const first = form.querySelector('input');
    if (first) {
        first.focus();
    }
}
