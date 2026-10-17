import {DAYNIGHT_MODES, isNight} from './sun.js';

const LABELS = {auto: 'Day/night: automatic', day: 'Day mode', night: 'Night mode'};
const CHECK_MS = 60000;

// The button cycles automatic, day and night. Automatic follows the sun
// where the server has a location configured, else the browser's dark mode
// setting. The car's cabin ranges from very light to very dark.
export function bindDayNight(app) {
    const {root, storage, settings} = app;
    const toggle = root.querySelector('.car-daynight');
    const location = settings.latitude === null || settings.longitude === null
        ? null
        : {latitude: settings.latitude, longitude: settings.longitude};
    const dark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    let mode = storage.choice('daynight', DAYNIGHT_MODES, 'auto');

    const update = () => {
        const night = isNight(mode, new Date(Date.now() + app.clockOffset), location, Boolean(dark && dark.matches));
        root.classList.toggle('car-night', night);
        root.classList.toggle('car-day', !night);
        toggle.dataset.mode = mode;
        toggle.title = LABELS[mode];
        toggle.setAttribute('aria-label', LABELS[mode]);
    };
    toggle.addEventListener('click', () => {
        mode = DAYNIGHT_MODES[(DAYNIGHT_MODES.indexOf(mode) + 1) % DAYNIGHT_MODES.length];
        storage.set('daynight', mode);
        update();
    });
    if (dark && dark.addEventListener) {
        dark.addEventListener('change', update);
    }
    update();
    window.setInterval(update, CHECK_MS);
}
