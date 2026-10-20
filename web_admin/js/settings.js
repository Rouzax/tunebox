// Settings: what every station inherits, where the player starts and the
// location for day and night, saved like a station change; and the
// listening PIN, which has its own save and stays out of the history.
import {button, el, field, icon} from './dom.js';
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
            'It cannot protect the audio itself: the streams are the stations\' own public addresses.'));
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
    box.append(
        field('Ask for the PIN on', mode, 'With the whole player locked, embeds on other sites stop working.'),
        field(data.set ? 'New PIN' : 'PIN', code, data.set ? 'Leave empty to keep the current PIN.' : null),
        el('div', {class: 'actions'}, el('button', {type: 'submit'}, icon('check_circle'), ' Save the PIN')));
    box.addEventListener('submit', async (event) => {
        event.preventDefault();
        const body = {mode: mode.value, based_on: data.revision};
        if (code.value) {
            body.pin = code.value;
        }
        const answer = await ctx.request('POST', 'pin', body);
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
        } else if (answer.status !== 401) {
            ctx.fail(answer.data);
        }
    });
    return box;
}

export async function render(root, ctx) {
    const page = {pinDirty: () => false};
    const settings = settingsForm(ctx);
    const question = el('div', {class: 'confirm'});
    ctx.unsaved = {
        dirty: () => settings.dirty() || page.pinDirty(),
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
    if (current()) {
        root.append(pin);
    }
}
