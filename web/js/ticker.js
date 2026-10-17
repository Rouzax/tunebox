const TICKER_SPEED_PX_PER_S = 40;
// Share of each sweep spent moving; the rest is the hold in car.css's keyframes.
const TICKER_MOVING_SHARE = 0.7;

function measure(ticker) {
    const text = ticker.firstElementChild;
    ticker.classList.remove('scrolling');
    if (ticker.clientWidth - text.offsetWidth >= 0) {
        return;
    }
    // A scrolling line carries edge-fade padding (car.css); the distance
    // is measured with it in place, so the last letter clears the fade.
    ticker.classList.add('scrolling');
    const shift = ticker.clientWidth - text.offsetWidth;
    const duration = -shift / TICKER_SPEED_PX_PER_S / TICKER_MOVING_SHARE;
    ticker.style.setProperty('--ticker-shift', `${shift}px`);
    ticker.style.setProperty('--ticker-duration', `${Math.max(duration, 4)}s`);
    // Removing the class and forcing a reflow restarts the sweep from the start.
    ticker.classList.remove('scrolling');
    void ticker.offsetWidth;
    ticker.classList.add('scrolling');
}

// The scrim behind the text (car.css) spans the widest visible line,
// capped at the line's box when that line scrolls.
function sizeScrim(onair) {
    let widest = 0;
    for (const ticker of onair.querySelectorAll('.ticker')) {
        if (!ticker.hidden) {
            widest = Math.max(widest, Math.min(ticker.firstElementChild.offsetWidth, ticker.clientWidth));
        }
    }
    onair.style.setProperty('--car-scrim-w', `${widest}px`);
}

// Lines that do not fit scroll back and forth instead of wrapping.
export function bindTicker(root) {
    const onair = root.querySelector('.onair');
    for (const ticker of onair.querySelectorAll('.ticker')) {
        let pending = 0;
        const schedule = () => {
            cancelAnimationFrame(pending);
            pending = requestAnimationFrame(() => {
                measure(ticker);
                sizeScrim(onair);
            });
        };
        new MutationObserver(schedule).observe(ticker.firstElementChild, {childList: true, characterData: true, subtree: true});
        // The Played-by line appears and disappears through its hidden attribute.
        new MutationObserver(schedule).observe(ticker, {attributes: true, attributeFilter: ['hidden']});
        if (window.ResizeObserver) {
            new ResizeObserver(schedule).observe(ticker);
        }
        // Poppins loads after first paint and changes the text width.
        if (document.fonts) {
            document.fonts.ready.then(schedule);
        }
        schedule();
    }
}
