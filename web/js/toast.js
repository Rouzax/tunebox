// Short status messages over the player.
export function createToast(root, timers) {
    const el = root.querySelector('.toast');
    let timer = null;
    return {
        show(text, ms = 2500) {
            el.textContent = text;
            el.classList.remove('hidden');
            if (timer !== null) {
                timers.clearTimeout(timer);
                timer = null;
            }
            if (ms > 0) {
                timer = timers.setTimeout(() => {
                    timer = null;
                    el.classList.add('hidden');
                }, ms);
            }
        },
    };
}
