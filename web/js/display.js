import {nextCover} from './tracks.js';

// Title, artist, Played-by and cover of what is shown as playing.
// Matches the card's slide-out in car.css.
const CARD_OUT_MS = 300;

export function bindDisplay(app) {
    const {root, events} = app;
    const channelLine = root.querySelector('.current-channel');
    const logo = root.querySelector('.car-station-logo');
    const title = root.querySelector('.car-title');
    const badge = root.querySelector('.car-badge');
    const artist = root.querySelector('.car-artist');
    const played = root.querySelector('.car-played');
    const image = root.querySelector('img.artwork');
    let channelShown = {default_image: null, logo: null};
    const card = root.querySelector('.onair');
    const reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    let swap = null;
    let first = true;

    // A new song slides the card out, swaps the text while it is out of
    // sight and slides it back (car.css). The first song, a hidden page
    // and reduced motion swap at once; a change during the slide replaces
    // the pending one.
    function change(apply) {
        if (swap !== null) {
            clearTimeout(swap);
            swap = null;
        }
        if (first || document.hidden || (reduce && reduce.matches)) {
            first = false;
            apply();
            return;
        }
        card.classList.remove('card-in');
        card.classList.add('card-out');
        swap = setTimeout(() => {
            swap = null;
            apply();
            card.classList.remove('card-out');
            card.classList.add('card-in');
            // The reflow places the card at its start before it slides in.
            void card.offsetWidth;
            card.classList.remove('card-in');
        }, CARD_OUT_MS);
    }

    // A cover that fails to load gives way to the channel's own images, then
    // to the built-in cover.
    logo.addEventListener('error', () => {
        logo.hidden = true;
    });
    image.addEventListener('error', () => {
        const next = nextCover(image.getAttribute('src'), channelShown);
        if (next) {
            image.src = next;
        }
    });

    events.on('track', ({lines, cover, channel}) => {
        channelLine.textContent = channel.name;
        // The small copy where tunebox made one: the header shows it at
        // text size.
        const logoUrl = channel.logo_small || channel.logo;
        if (!logoUrl) {
            logo.hidden = true;
            logo.removeAttribute('src');
        } else if (logo.getAttribute('src') !== logoUrl) {
            logo.hidden = false;
            logo.src = logoUrl;
        }
        change(() => {
            channelShown = channel;
            title.textContent = lines.title;
            badge.textContent = lines.badge;
            badge.hidden = !lines.badge;
            artist.textContent = lines.artist;
            played.textContent = lines.playedBy;
            played.closest('.car-line').hidden = !lines.playedBy;
            if (image.getAttribute('src') !== cover) {
                image.src = cover;
            }
        });
    });
}
