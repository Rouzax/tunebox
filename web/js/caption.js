// The scrim behind the track text (car.css) spans the widest line as drawn.
// Lines wrap, so a line's box is as wide as the caption; the text's own
// line boxes give the width the eye sees.
function widest(onair) {
    let width = 0;
    const range = document.createRange();
    for (const line of onair.querySelectorAll('.car-line')) {
        if (line.hidden) {
            continue;
        }
        range.selectNodeContents(line);
        for (const rect of range.getClientRects()) {
            width = Math.max(width, rect.width);
        }
    }
    return Math.min(Math.ceil(width), onair.clientWidth);
}

export function bindCaption(root) {
    const onair = root.querySelector('.onair');
    let pending = 0;
    const schedule = () => {
        cancelAnimationFrame(pending);
        pending = requestAnimationFrame(() => {
            onair.style.setProperty('--car-scrim-w', `${widest(onair)}px`);
        });
    };
    // Text changes, and the Played-by line appears and disappears through
    // its hidden attribute.
    new MutationObserver(schedule).observe(onair, {
        childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ['hidden'],
    });
    if (window.ResizeObserver) {
        new ResizeObserver(schedule).observe(onair);
    } else {
        window.addEventListener('resize', schedule);
    }
    // Poppins loads after first paint and changes the text width.
    if (document.fonts) {
        document.fonts.ready.then(schedule);
    }
    schedule();
}
