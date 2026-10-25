// Settings: what every station inherits, where the player starts and the
// location for day and night, saved like a station change; the listening
// PIN and the usage settings, which have saves of their own.
import {button, confirmed, el, field, icon} from './dom.js';
import {setSettings} from './state.js';

const NUMBERS = [
    ['delay_s', 'Title delay (seconds)', 'How long after a station\'s feed changes the song changes in its audio.'],
    ['pawtunes_delay_s', 'PawTunes delay (seconds)', 'The same for the PawTunes endpoint.'],
    ['poll_interval_s', 'Feed check (seconds)', 'How often tunebox asks a station what plays.'],
    ['history_size', 'Recently played (songs)', 'How many songs each station keeps.'],
];

function numberOrUndefined(input) {
    return input.value.trim() === '' ? undefined : Number(input.value);
}

function settingsForm(ctx) {
    const state = ctx.config.state;
    const builtIn = ctx.config.schema.defaults;
    const numbers = {};
    const rows = NUMBERS.map(([key, label, hint]) => {
        numbers[key] = el('input', {
            type: 'number',
            name: key,
            step: key === 'history_size' ? '1' : 'any',
            min: key === 'history_size' || key === 'poll_interval_s' ? '1' : '0',
            placeholder: String(builtIn[key]),
            value: state.defaults[key] === undefined ? '' : String(state.defaults[key]),
        });
        return field(label, numbers[key], `${hint} Empty uses ${builtIn[key]}; a station can set its own.`);
    });
    const enabled = state.channels.filter((row) => row.enabled);
    const station = el('select', {name: 'default_channel'},
        el('option', {value: ''}, 'The first station'),
        enabled.map((row) => el('option', {value: row.id}, row.doc.name || row.id)));
    station.value = state.player.default_channel || '';
    const coordinate = (key, limit) => el('input', {
        type: 'number', name: key, step: 'any', min: String(-limit), max: String(limit),
        value: state.player[key] === undefined ? '' : String(state.player[key]),
    });
    const latitude = coordinate('latitude', 90);
    const longitude = coordinate('longitude', 180);
    const error = el('p', {class: 'error', role: 'alert', hidden: true});

    function collect() {
        const defaults = {};
        for (const [key] of NUMBERS) {
            defaults[key] = numberOrUndefined(numbers[key]);
        }
        return {
            defaults,
            latitude: numberOrUndefined(latitude),
            longitude: numberOrUndefined(longitude),
            defaultChannel: station.value,
        };
    }

    const form = el('form', {class: 'settings'},
        el('h2', null, icon('settings'), ' Every station'),
        rows,
        el('h2', null, 'The player'),
        field('Starts on', station, 'For a device that has not picked a station yet.'),
        field('Latitude', latitude, 'With the longitude: where day and night are worked out for. Empty: the player uses the time of day.'),
        field('Longitude', longitude),
        error,
        el('div', {class: 'actions'}, el('button', {type: 'submit'}, icon('check_circle'), ' Save')));
    const opened = JSON.stringify(collect());
    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const values = collect();
        if ((values.latitude === undefined) !== (values.longitude === undefined)) {
            error.textContent = 'Give the latitude and the longitude together, or neither.';
            error.hidden = false;
            return;
        }
        error.hidden = true;
        await ctx.save(setSettings(state, values), 'settings', 'Settings saved.');
    });
    return {form, dirty: () => JSON.stringify(collect()) !== opened};
}

const MODES = [
    ['off', 'Off: everyone can listen'],
    ['site', 'The whole player: nothing shows before the PIN'],
    ['stations', 'Chosen stations: the ones marked "Needs the PIN" stay hidden until it is typed'],
];

// The form for the PIN. page.pinDirty follows the form currently on screen.
async function pinForm(ctx, page) {
    const box = el('form', {class: 'settings pin'},
        el('h2', null, icon('pin'), ' Listening PIN'),
        el('p', {class: 'hint'},
            'Keeps strangers who find the player\'s address out of tunebox. Each device types it once. ',
            'A new PIN, or switching it on, asks every device again. ',
            'It cannot protect the audio itself: the streams are the stations\' own public addresses. ',
            'Changing the PIN asks for a code from your authenticator app.'));
    page.pinDirty = () => false;
    const {ok, data} = await ctx.request('GET', 'pin');
    if (!ok) {
        if (data.error !== undefined) {
            ctx.fail(data);
        }
        return box;
    }
    const {pin_min: min, pin_max: max} = ctx.limits;
    const mode = el('select', {name: 'pin_mode'}, MODES.map(([value, label]) => el('option', {value}, label)));
    mode.value = data.mode;
    const code = el('input', {
        type: 'password', name: 'pin', inputmode: 'numeric', autocomplete: 'new-password',
        pattern: `[0-9]{${min},${max}}`, minlength: String(min), maxlength: String(max),
        placeholder: data.set ? 'Unchanged' : `${min} to ${max} digits`,
    });
    page.pinDirty = () => mode.value !== data.mode || code.value !== '';
    const codeBox = el('div');
    box.append(
        field('Ask for the PIN on', mode, 'With the whole player locked, embeds on other sites stop working.'),
        field(data.set ? 'New PIN' : 'PIN', code, data.set ? 'Leave empty to keep the current PIN.' : null),
        el('div', {class: 'actions'}, el('button', {type: 'submit'}, icon('check_circle'), ' Save the PIN')),
        codeBox);
    box.addEventListener('submit', async (event) => {
        event.preventDefault();
        const body = {mode: mode.value, based_on: data.revision};
        if (code.value) {
            body.pin = code.value;
        }
        const answer = await confirmed(ctx, codeBox, () => ctx.request('POST', 'pin', body));
        code.value = '';
        if (answer.ok) {
            const said = {
                off: 'PIN saved. The player is open to everyone.',
                site: 'PIN saved. The player asks for it now.',
                stations: 'PIN saved. Stations marked "Needs the PIN" are hidden until it is typed.',
            };
            ctx.message(said[answer.data.mode]);
            box.replaceWith(await pinForm(ctx, page));
        } else if (answer.status === 409) {
            ctx.message('The PIN settings changed elsewhere; the form now shows them.');
            box.replaceWith(await pinForm(ctx, page));
        } else if (answer.status === 400 && answer.data.message) {
            ctx.message(`The PIN was not saved: ${answer.data.message}.`);
        } else if (answer.status === 0) {
            ctx.message('The PIN was not saved: changing it needs a code from your authenticator app.');
        } else if (answer.status !== 401) {
            ctx.fail(answer.data);
        }
    });
    return box;
}

// The form for the usage settings. page.usageDirty follows the form
// currently on screen. Only changed fields are sent: the server asks for a
// code only when storing addresses is switched on.
async function usageForm(ctx, page) {
    const box = el('form', {class: 'settings usage'},
        el('h2', null, icon('devices'), ' Usage'),
        el('p', {class: 'hint'},
            'tunebox counts which devices listen to which station, and for how long. ',
            'Storing addresses also keeps the network address of each visit and listening session; ',
            'switching it on asks for a code from your authenticator app.'));
    page.usageDirty = () => false;
    const {ok, status, data} = await ctx.request('GET', 'usage/settings');
    if (!ok) {
        if (status === 503 && data.error === 'usage_unavailable') {
            box.append(el('p', {class: 'warning'}, icon('warning'), ' tunebox cannot read its usage data right now.'));
        } else if (status !== 401) {
            ctx.fail(data);
        }
        return box;
    }
    const days = el('input', {
        type: 'number', name: 'retention_days', step: '1', required: true,
        min: String(data.retention_min), max: String(data.retention_max), value: String(data.retention_days),
    });
    const addresses = el('input', {type: 'checkbox', name: 'store_addresses', checked: data.store_addresses});
    function changes() {
        const body = {};
        if (Number(days.value) !== data.retention_days) {
            body.retention_days = Number(days.value);
        }
        if (addresses.checked !== data.store_addresses) {
            body.store_addresses = addresses.checked;
        }
        return body;
    }
    page.usageDirty = () => Object.keys(changes()).length > 0;
    const codeBox = el('div');
    box.append(
        field('Keep details for (days)', days, 'Shortening it deletes older details at the next daily run; daily totals stay.'),
        el('label', {class: 'check'}, addresses, 'Store addresses'),
        el('div', {class: 'actions'}, el('button', {type: 'submit'}, icon('check_circle'), ' Save the usage settings')),
        codeBox);
    box.addEventListener('submit', async (event) => {
        event.preventDefault();
        const body = changes();
        if (!Object.keys(body).length) {
            ctx.message('Nothing to save.');
            return;
        }
        const answer = await confirmed(ctx, codeBox, () => ctx.request('POST', 'usage/settings', body));
        if (answer.ok) {
            ctx.message(answer.data.saved === false
                ? 'Addresses are off now; tunebox will clear the stored ones once it can write its usage data.'
                : 'Usage settings saved.');
            box.replaceWith(await usageForm(ctx, page));
        } else if (answer.status === 0) {
            ctx.message('Storing addresses was not switched on: it needs a code from your authenticator app.');
        } else if (answer.status === 503) {
            // A combined change stores the retention before the addresses,
            // so it may be half saved: show what is stored.
            ctx.fail(answer.data);
            box.replaceWith(await usageForm(ctx, page));
        } else if (answer.status !== 401) {
            ctx.fail(answer.data);
        }
    });
    return box;
}

export async function render(root, ctx) {
    const page = {pinDirty: () => false, usageDirty: () => false};
    const settings = settingsForm(ctx);
    const question = el('div', {class: 'confirm'});
    ctx.unsaved = {
        dirty: () => settings.dirty() || page.pinDirty() || page.usageDirty(),
        ask(proceed) {
            const stay = button('edit', 'Keep editing', () => question.remove());
            question.replaceChildren(
                el('span', null, 'This page has changes that are not saved.'),
                button('close', 'Leave without saving', proceed, {class: 'danger'}),
                stay);
            settings.form.querySelector('h2').after(question);
            stay.focus();
        },
    };
    const current = ctx.stillOpen;
    root.replaceChildren(settings.form);
    const pin = await pinForm(ctx, page);
    // The page may have been left while the PIN status loaded.
    if (!current()) {
        return;
    }
    root.append(pin);
    const usage = await usageForm(ctx, page);
    if (current()) {
        root.append(usage);
    }
}
