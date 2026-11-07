// The page in full screen and back, from one button. Chromium 120 has the
// unprefixed API; the webkit names keep older WebViews working.
export function isFullscreen() {
    return Boolean(document.fullscreenElement || document.webkitFullscreenElement);
}

// Shows the button only where the browser allows full screen; false when
// it does not.
export function bindFullscreen(toggle) {
    if (!(document.fullscreenEnabled || document.webkitFullscreenEnabled)) {
        return false;
    }
    const page = document.documentElement;
    const sync = () => toggle.classList.toggle('active', isFullscreen());
    toggle.classList.remove('hidden');
    toggle.addEventListener('click', () => {
        const request = isFullscreen()
            ? (document.exitFullscreen || document.webkitExitFullscreen).call(document)
            : (page.requestFullscreen || page.webkitRequestFullscreen).call(page);
        // Browsers may refuse (no user gesture, policy); the button just stays as it was.
        if (request && request.catch) {
            request.catch(() => {});
        }
    });
    document.addEventListener('fullscreenchange', sync);
    document.addEventListener('webkitfullscreenchange', sync);
    return true;
}
