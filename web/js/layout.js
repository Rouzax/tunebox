// Real height. Car and mobile browsers can report 100vh as the screen
// height without their address bar, so pages sized in vh run taller than
// the window and centre the title under the control bar while the bar is
// shown. car.css sizes the pages and the cover from --car-vh, the measured
// height of the player box, which follows the real window.
export function bindLayout(app) {
    const {root, toast} = app;
    const box = root.querySelector('.main-container');
    // Wide windows (the car's 2/3 and full-width split) put the text beside
    // the cover; the narrow third, and every embed, keeps it stacked.
    const update = () => {
        root.style.setProperty('--car-vh', `${box.clientHeight}px`);
        root.dataset.layout = !app.embed && box.clientWidth > box.clientHeight * 1.25 ? 'wide' : 'stacked';
    };
    update();
    if (window.ResizeObserver) {
        new ResizeObserver(update).observe(box);
    } else {
        window.addEventListener('resize', update);
    }
    if (app.embed) {
        return;
    }
    bindFullscreen(root);

    const popup = root.querySelector('.popup-toggle');
    popup.classList.remove('hidden');
    popup.addEventListener('click', () => {
        const playing = app.player.state() !== 'stopped';
        const url = `/?channel=${encodeURIComponent(app.channel.id)}&embed=1${playing ? '&play=1' : ''}`;
        const opened = window.open(url, 'tunebox', 'popup=yes,width=420,height=640');
        if (!opened) {
            toast.show('The browser blocked the small window.');
            return;
        }
        // One player at a time: the small window takes over.
        if (playing) {
            app.stop();
        }
    });
}

function bindFullscreen(root) {
    const toggle = root.querySelector('.fullscreen-toggle');
    const page = document.documentElement;
    if (!(document.fullscreenEnabled || document.webkitFullscreenEnabled)) {
        return;
    }
    const current = () => document.fullscreenElement || document.webkitFullscreenElement;
    const sync = () => toggle.classList.toggle('active', Boolean(current()));
    toggle.classList.remove('hidden');
    toggle.addEventListener('click', () => {
        const request = current()
            ? (document.exitFullscreen || document.webkitExitFullscreen).call(document)
            : (page.requestFullscreen || page.webkitRequestFullscreen).call(page);
        // Browsers may refuse (no user gesture, policy); the button just stays as it was.
        if (request && request.catch) {
            request.catch(() => {});
        }
    });
    document.addEventListener('fullscreenchange', sync);
    document.addEventListener('webkitfullscreenchange', sync);
}
