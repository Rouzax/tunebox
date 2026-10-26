import {POLL_MS, createApi, createPoller} from './api.js';
import {canPlay, keepSelection, listOutdated, nextChannel, pickChannel, pickStream} from './channels.js';
import {bindControls} from './controls.js';
import {bindDayNight} from './daynight.js';
import {bindDebug} from './debug.js';
import {bindDisplay} from './display.js';
import {createEmitter} from './events.js';
import {bindLayout} from './layout.js';
import {bindMedia} from './media.js';
import {bindPages} from './pages.js';
import {bindPalette} from './palette.js';
import {createPlayer, resumesAfterPause} from './player.js';
import {createSwitcher} from './schedule.js';
import {createStorage} from './storage.js';
import {bindTicker} from './ticker.js';
import {bindPinScreen, onceAsked} from './pin.js';
import {createToast} from './toast.js';
import {clockOffsetMs, coverFor, displayLines, trackKey} from './tracks.js';
import {bindVisualizer} from './visualizer.js';

function browserStorage() {
    try {
        return window.localStorage;
    } catch (error) {
        return null;
    }
}

const later = (fn, ms) => window.setTimeout(fn, ms);
const cancel = (id) => window.clearTimeout(id);

async function start(root) {
    const events = createEmitter();
    const storage = createStorage(browserStorage());
    const api = createApi((input, init) => window.fetch(input, init));
    const toast = createToast(root, window);
    const player = createPlayer({
        events,
        makeAudio: () => new Audio(),
        timers: window,
        now: () => Date.now(),
        resumeAfterPause: resumesAfterPause(navigator.userAgent),
    });
    // Set once the app's own stop exists; the keypad can open before that.
    let stopPlayer = null;
    const pin = bindPinScreen(root, api, {
        canStop: () => stopPlayer !== null && player.state() !== 'stopped',
        onStop: () => {
            if (stopPlayer) {
                stopPlayer();
            }
        },
    });
    const askToListen = () => pin.ask({title: 'Enter the PIN to listen', cancellable: false});
    let listing;
    let settings;
    for (;;) {
        try {
            [listing, settings] = await Promise.all([api.channels(), api.player()]);
            break;
        } catch (error) {
            if (error.status !== 401) {
                toast.show('Could not reach tunebox. Reload the page to try again.', 0);
                return;
            }
            // The site was locked after this page loaded: ask, then load again.
            await askToListen();
        }
    }
    const params = new URLSearchParams(window.location.search);
    const app = {
        root, events, storage, player, toast, settings,
        channels: listing.channels,
        version: listing.version,
        pinRevision: listing.pin,
        images: listing.images,
        morePin: listing.more,
        embed: params.get('embed') === '1',
        channel: null,
        stream: null,
        history: [],
        clockOffset: 0,
    };
    if (!app.channels.length && listing.more) {
        // Every station waits behind the PIN: there is nothing to show
        // without it, so ask straight away.
        await pin.ask({title: 'Enter the PIN for the stations', cancellable: false});
        window.location.reload();
        return;
    }
    if (!app.channels.length) {
        toast.show('No stations yet.', 0);
        return;
    }
    // First, so it sees the first play.
    if (params.get('debug') === '1') {
        bindDebug(app);
    }
    let shown = null;
    let shownPhoto = null;
    let unreachable = false;
    // Whether to resume after a reload. Only the full player keeps it: the
    // pop-out and embeds share this storage, and the pop-out resumes from
    // its own ?play=1, so its playing must not make the main window play too.
    const remember = (playing) => {
        if (!app.embed) {
            storage.set('playing', playing ? 1 : 0);
        }
    };

    // Only a change of what is shown is announced: rewriting the same text
    // would replay the card's change on every poll.
    // The artists' photo can arrive a poll after its song; it changes only
    // the cover's back, so it has an event of its own.
    function show(track) {
        const key = `${app.channel.id}\u0000${trackKey(track)}`;
        const photo = (track && track.artist_photo) || null;
        if (key === shown) {
            if (photo !== shownPhoto) {
                shownPhoto = photo;
                events.emit('photo', {photo});
            }
            return;
        }
        shown = key;
        shownPhoto = photo;
        events.emit('track', {
            track,
            lines: displayLines(track, app.channel.name),
            cover: coverFor(track, app.channel),
            photo,
            channel: app.channel,
        });
    }

    const switcher = createSwitcher({show, now: () => Date.now(), setTimer: later, clearTimer: cancel});
    // The site was locked, or its PIN changed, while listening. The audio
    // plays on (it does not pass through tunebox); the titles follow the PIN.
    let poller = null;
    const locked = onceAsked(askToListen, () => {
        refreshChannels();
        poller.refresh();
    });
    poller = createPoller({
        api,
        getBufferS: () => player.bufferS(),
        getStream: () => {
            const known = app.channel.streams.indexOf(app.stream) >= 0;
            return player.state() === 'playing' && known ? app.stream.name : null;
        },
        setTimer: later,
        clearTimer: cancel,
        onDate: (date) => {
            app.clockOffset = clockOffsetMs(date, Date.now());
        },
        onReached: () => {
            unreachable = false;
        },
        onBody: (body) => {
            if (listOutdated(body, app)) {
                refreshChannels();
            }
            switcher.update(body);
            app.history = body.history;
            events.emit('history', {history: body.history, channel: app.channel});
        },
        onMissing: () => refreshChannels(),
        onError: (error) => {
            if (error.status === 401) {
                locked();
                return;
            }
            if (!unreachable) {
                unreachable = true;
                toast.show('Lost contact with tunebox, still trying.');
            }
        },
    });

    // The configuration changed: take the new list and keep what plays. A
    // channel that is gone keeps playing without titles until another is picked.
    let refreshing = false;
    // The station on air left the list, so its titles stopped; they start
    // again if it comes back (a PIN typed for hidden stations).
    let lostChannel = false;
    let retry = null;
    async function refreshChannels() {
        if (refreshing) {
            return;
        }
        refreshing = true;
        try {
            const fresh = await api.channels();
            app.channels = fresh.channels;
            app.version = fresh.version;
            app.pinRevision = fresh.pin;
            app.images = fresh.images;
            app.morePin = fresh.more;
            const kept = keepSelection(app.channels, app.channel.id, app.stream && app.stream.name, playable);
            if (kept) {
                app.channel = kept.channel;
                app.stream = kept.stream;
                // Playing on would leave the old stream's audio under the
                // new stream's name, and its burst in the title timing.
                if (kept.replaced && player.state() !== 'stopped') {
                    app.play();
                }
                if (lostChannel) {
                    lostChannel = false;
                    poller.start(app.channel.id);
                }
            } else {
                poller.stop();
                lostChannel = true;
                if (fresh.more) {
                    // Most likely hidden behind a changed PIN: offer it back.
                    app.unlock();
                }
            }
            events.emit('channels', {channels: app.channels});
            events.emit('stream', {channel: app.channel, stream: app.stream});
        } catch (error) {
            // The next poll tries again; with the station on air gone from
            // the list there is no poll, so try again here.
            if (lostChannel && retry === null) {
                retry = later(() => {
                    retry = null;
                    refreshChannels();
                }, POLL_MS);
            }
        } finally {
            refreshing = false;
        }
    }

    const probe = document.createElement('audio');
    const playable = (stream) => canPlay(stream, (type) => probe.canPlayType(type));

    app.selectChannel = (id) => {
        // A station picked by hand has titles of its own again.
        lostChannel = false;
        if (retry !== null) {
            cancel(retry);
            retry = null;
        }
        app.channel = app.channels.find((channel) => channel.id === id);
        app.stream = pickStream(app.channel, storage.get(`stream.${id}`), playable);
        storage.set('channel', id);
        switcher.reset();
        shown = null;
        show(null);
        app.history = [];
        events.emit('channel', {channel: app.channel, stream: app.stream});
        events.emit('history', {history: [], channel: app.channel});
        poller.start(id);
        if (player.state() !== 'stopped') {
            app.play();
        }
    };
    app.selectStream = (name) => {
        app.stream = pickStream(app.channel, name);
        storage.set(`stream.${app.channel.id}`, app.stream.name);
        events.emit('stream', {channel: app.channel, stream: app.stream});
        if (player.state() !== 'stopped') {
            app.play();
        }
    };
    // Stations behind the PIN ("chosen stations" mode): the channel page
    // offers them; cancelling leaves the list as it is.
    app.unlock = () => pin.ask({title: 'Enter the PIN for more stations', cancellable: true})
        .then((accepted) => {
            if (accepted) {
                refreshChannels();
            }
        });
    app.step = (delta) => app.selectChannel(nextChannel(app.channels, app.channel.id, delta));
    app.play = () => {
        if (!playable(app.stream)) {
            // Never leave the previous station playing under this one's titles.
            player.stop();
            toast.show(`This browser cannot play ${app.stream.codec.toUpperCase()}. Pick another quality on the channel page.`, 8000);
            return;
        }
        remember(true);
        toast.show(`Loading ${app.channel.name}...`);
        player.play(app.stream.url);
    };
    app.stop = () => {
        remember(false);
        player.stop();
        toast.show('Player stopped.');
    };
    stopPlayer = app.stop;

    events.on('state', (state) => {
        if (state === 'playing') {
            toast.show(`Playing ${app.channel.name}...`);
            // The buffer is known now and decides when titles switch.
            poller.refresh();
        } else if (state === 'reconnecting') {
            toast.show('Reconnecting...', 0);
        }
    });
    events.on('autoplay-refused', ({resumed}) => {
        remember(false);
        // On a page load the play button says it all; mid-listen it does not.
        if (resumed) {
            toast.show('The browser stopped playback. Press play to continue.', 0);
        }
    });
    events.on('interrupted', () => {
        remember(false);
        toast.show('Paused by the system. Press play to continue.', 0);
    });
    window.addEventListener('online', () => player.online());
    document.addEventListener('visibilitychange', () => {
        if (document.hidden && player.state() === 'stopped') {
            poller.stop();
        } else if (!document.hidden) {
            poller.start(app.channel.id);
        }
    });

    if (app.embed) {
        root.dataset.embed = '';
    }
    bindDisplay(app);
    bindTicker(root);
    bindControls(app);
    bindLayout(app);
    bindDayNight(app);
    bindPalette(app);
    bindMedia(app);
    app.selectChannel(pickChannel(app.channels, params.get('channel'), storage.get('channel'), settings.default_channel));
    // The channel list marks the active channel, so it binds once one is selected.
    bindPages(app);
    bindVisualizer(app);
    // A refused autoplay (no user gesture yet) quietly leaves the play button.
    // play=1 comes from the main window when it hands playback to a small window.
    if (params.get('play') === '1' || (!app.embed && storage.flag('playing'))) {
        app.play();
    }
}

const root = document.querySelector('.tunebox');
if (root) {
    start(root);
}
