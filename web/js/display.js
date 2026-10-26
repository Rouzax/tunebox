import {createFlip} from './flip.js';
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
    const artBox = root.querySelector('.artwork-img');
    const photoImage = root.querySelector('img.artwork-photo');
    const flip = createFlip();
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

    // A tap turns the cover when there is a photo on its back; the photo is
    // loaded as it arrives, so the turn shows it at once.
    function showFlip(state) {
        if (state.photo === null) {
            photoImage.removeAttribute('src');
        } else if (photoImage.getAttribute('src') !== state.photo) {
            photoImage.src = state.photo;
        }
        artBox.classList.toggle('flipped', state.flipped);
        artBox.classList.toggle('can-flip', state.canFlip);
        if (state.canFlip) {
            artBox.setAttribute('role', 'button');
            artBox.setAttribute('tabindex', '0');
            artBox.setAttribute('aria-label', state.flipped ? 'Show the cover' : 'Show the artist photo');
        } else {
            artBox.removeAttribute('role');
            artBox.removeAttribute('tabindex');
            artBox.removeAttribute('aria-label');
        }
    }
    artBox.addEventListener('click', () => showFlip(flip.tap()));
    artBox.addEventListener('keydown', (event) => {
        if (artBox.classList.contains('can-flip') && (event.key === 'Enter' || event.key === ' ')) {
            event.preventDefault();
            showFlip(flip.tap());
        }
    });
    // A photo that will not load leaves the cover with nothing behind it.
    photoImage.addEventListener('error', () => {
        if (photoImage.getAttribute('src')) {
            showFlip(flip.photo(null));
        }
    });
    // The newest photo for the song shown or about to be shown. A photo
    // arriving while a new song's card is still sliding out belongs to that
    // song: the swap reads it here when it runs, rather than the value the
    // song came with, and the old song's back is left alone meanwhile.
    let photoNext = null;
    events.on('photo', ({photo}) => {
        photoNext = photo;
        if (swap === null) {
            showFlip(flip.photo(photo));
        }
    });

    events.on('track', ({lines, cover, photo, channel}) => {
        photoNext = photo;
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
            showFlip(flip.song(photoNext));
        });
    });
}
