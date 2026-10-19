// Play/stop, volume and mute in the control bar.
import {pageVolume} from './player.js';

export function bindControls(app) {
    const {root, events, player, storage, toast} = app;
    const bar = root.querySelector('.player');
    const slider = root.querySelector('.volume-slider');
    const muteButton = root.querySelector('.volume-icon');

    root.querySelector('.play').addEventListener('click', () => app.play());
    root.querySelector('.stop').addEventListener('click', () => app.stop());

    // iOS ignores the volume property; the bar hides the control there.
    const probe = document.createElement('audio');
    probe.volume = 0.5;
    if (probe.volume !== 0.5) {
        bar.classList.add('no-volume');
    }

    // The same width at which base.css hides the slider.
    const narrow = window.matchMedia ? window.matchMedia('(max-width: 560px)') : null;
    const sliderShown = () => !(narrow && narrow.matches);
    const apply = () => player.setVolume(pageVolume(Number(slider.value), sliderShown()));
    slider.value = String(storage.number('volume', 50, 0, 100));
    apply();
    if (narrow && narrow.addEventListener) {
        narrow.addEventListener('change', apply);
    }
    slider.addEventListener('input', () => {
        apply();
        storage.set('volume', Number(slider.value));
        toast.show(`Volume: ${slider.value}`, 1000);
    });

    let muted = false;
    muteButton.addEventListener('click', () => {
        muted = !muted;
        player.setMuted(muted);
        muteButton.classList.toggle('muted', muted);
        toast.show(muted ? 'Player muted.' : sliderShown() ? `Volume: ${slider.value}` : 'Sound on.', 1000);
    });

    events.on('state', (state) => {
        root.dataset.state = state;
    });
}
