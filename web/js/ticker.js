const SPEED_PX_PER_S = 40;
// Share of each sweep spent moving; the rest is the hold in car.css's keyframes.
const MOVING_SHARE = 0.7;
const MIN_SWEEP_S = 4;

// Stacked, the card's lines stay one line each (car.css); one that does not
// fit scrolls to its end and back. Beside the cover the lines wrap instead,
// and with reduced motion a long line ends in an ellipsis.
export function bindTicker(root) {
    const lines = root.querySelectorAll('.onair .car-line');
    const reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

    function measure(line) {
        const text = line.firstElementChild;
        line.classList.remove('scrolling');
        if (root.dataset.layout === 'wide' || (reduce && reduce.matches)) {
            return;
        }
        if (text.offsetWidth <= line.clientWidth) {
            return;
        }
        // Measured with the fade padding in place, so the last letter clears the fade.
        line.classList.add('scrolling');
        const shift = line.clientWidth - text.offsetWidth;
        const sweep = -shift / SPEED_PX_PER_S / MOVING_SHARE;
        line.style.setProperty('--ticker-shift', `${shift}px`);
        line.style.setProperty('--ticker-duration', `${Math.max(sweep, MIN_SWEEP_S)}s`);
        // Removing the class and forcing a reflow restarts the sweep from the start.
        line.classList.remove('scrolling');
        void line.offsetWidth;
        line.classList.add('scrolling');
    }

    let pending = 0;
    function schedule() {
        cancelAnimationFrame(pending);
        pending = requestAnimationFrame(() => {
            for (const line of lines) {
                measure(line);
            }
        });
    }

    for (const line of lines) {
        new MutationObserver(schedule).observe(line.firstElementChild, {childList: true, characterData: true, subtree: true});
        if (window.ResizeObserver) {
            new ResizeObserver(schedule).observe(line);
        }
    }
    new MutationObserver(schedule).observe(root, {attributes: true, attributeFilter: ['data-layout']});
    if (reduce && reduce.addEventListener) {
        reduce.addEventListener('change', schedule);
    }
    // Poppins loads after the first paint and changes the text width.
    if (document.fonts) {
        document.fonts.ready.then(schedule);
    }
    schedule();
}
