// Play/stop, volume and mute in the control bar.
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

    const level = storage.number('volume', 50, 0, 100);
    slider.value = String(level);
    player.setVolume(level / 100);
    slider.addEventListener('input', () => {
        const value = Number(slider.value);
        player.setVolume(value / 100);
        storage.set('volume', value);
        toast.show(`Volume: ${value}`, 1000);
    });

    let muted = false;
    muteButton.addEventListener('click', () => {
        muted = !muted;
        player.setMuted(muted);
        muteButton.classList.toggle('muted', muted);
        toast.show(muted ? 'Player muted.' : `Volume: ${slider.value}`, 1000);
    });

    events.on('state', (state) => {
        root.dataset.state = state;
    });
}
