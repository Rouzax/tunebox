import {nextCover} from './tracks.js';

// Title, artist, Played-by and cover of what is shown as playing.
export function bindDisplay(app) {
    const {root, events} = app;
    const channelLine = root.querySelector('.current-channel');
    const title = root.querySelector('.car-title');
    const artist = root.querySelector('.car-artist');
    const played = root.querySelector('.car-played');
    const image = root.querySelector('img.artwork');
    let channelShown = {default_image: null, logo: null};

    // A cover that fails to load gives way to the channel's own images, then
    // to the built-in cover.
    image.addEventListener('error', () => {
        const next = nextCover(image.getAttribute('src'), channelShown);
        if (next) {
            image.src = next;
        }
    });

    events.on('track', ({lines, cover, channel}) => {
        channelShown = channel;
        channelLine.textContent = channel.name;
        title.textContent = lines.title;
        artist.textContent = lines.artist;
        played.textContent = lines.playedBy;
        played.parentNode.hidden = !lines.playedBy;
        if (image.getAttribute('src') !== cover) {
            image.src = cover;
        }
    });
}
