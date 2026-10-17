import {coverFor} from './tracks.js';

// Title, artist, Played-by and cover of what is shown as playing.
export function bindDisplay(app) {
    const {root, events} = app;
    const channelLine = root.querySelector('.current-channel');
    const title = root.querySelector('.car-title');
    const artist = root.querySelector('.car-artist');
    const played = root.querySelector('.car-played');
    const image = root.querySelector('img.artwork');
    let fallback = coverFor(null, {default_image: null, logo: null});

    // A cover that fails to load shows the channel's own image instead.
    image.addEventListener('error', () => {
        if (image.getAttribute('src') !== fallback) {
            image.src = fallback;
        }
    });

    events.on('track', ({lines, cover, channel}) => {
        fallback = coverFor(null, channel);
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
