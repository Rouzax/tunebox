import {pageTitle} from './tracks.js';

const ACTIONS = ['play', 'pause', 'stop', 'nexttrack', 'previoustrack'];

// The tab title, and the lock screen and media keys through Media Session.
// Next and previous switch channel, so steering-wheel and headset keys
// change station.
export function bindMedia(app) {
    const {events} = app;
    const session = 'mediaSession' in navigator ? navigator.mediaSession : null;
    let lines = null;
    let channel = null;
    let cover = null;
    let state = 'stopped';

    function update() {
        if (!channel) {
            return;
        }
        document.title = state === 'stopped' ? channel.name : pageTitle(lines, channel.name);
        if (!session || !window.MediaMetadata) {
            return;
        }
        session.metadata = new MediaMetadata({
            title: lines.title,
            artist: lines.artist,
            album: channel.name,
            artwork: [{src: new URL(cover, window.location.href).href}],
        });
        // Paused, not none: the lock screen keeps its play button after a stop.
        session.playbackState = state === 'stopped' ? 'paused' : 'playing';
    }

    events.on('track', (shown) => {
        lines = shown.lines;
        channel = shown.channel;
        cover = shown.cover;
        update();
    });
    events.on('state', (next) => {
        state = next;
        update();
    });
    if (!session) {
        return;
    }
    const handlers = {
        play: () => app.play(),
        pause: () => app.stop(),
        stop: () => app.stop(),
        nexttrack: () => app.step(1),
        previoustrack: () => app.step(-1),
    };
    for (const action of ACTIONS) {
        try {
            session.setActionHandler(action, handlers[action]);
        } catch (error) {
            // This browser does not know the action; the others still work.
        }
    }
}
