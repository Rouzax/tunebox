import {createApi, createPoller} from './api.js';
import {canPlay, nextChannel, pickChannel, pickStream} from './channels.js';
import {bindControls} from './controls.js';
import {bindDayNight} from './daynight.js';
import {bindDebug} from './debug.js';
import {bindDisplay} from './display.js';
import {createEmitter} from './events.js';
import {bindLayout} from './layout.js';
import {bindMedia} from './media.js';
import {bindPages} from './pages.js';
import {bindPalette} from './palette.js';
import {createPlayer} from './player.js';
import {createSwitcher} from './schedule.js';
import {createStorage} from './storage.js';
import {bindTicker} from './ticker.js';
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
    const player = createPlayer({events, makeAudio: () => new Audio(), timers: window, now: () => Date.now()});
    let channels;
    let settings;
    try {
        [channels, settings] = await Promise.all([api.channels(), api.player()]);
    } catch (error) {
        toast.show('Could not reach tunebox. Reload the page to try again.', 0);
        return;
    }
    const params = new URLSearchParams(window.location.search);
    const app = {
        root, events, storage, player, toast, channels, settings,
        embed: params.get('embed') === '1',
        channel: null,
        stream: null,
        history: [],
        clockOffset: 0,
    };
    // First, so it sees the first play.
    if (params.get('debug') === '1') {
        bindDebug(app);
    }
    let shown = null;
    let unreachable = false;

    // Only a change of what is shown is announced: rewriting the same text
    // would restart the ticker on every poll.
    function show(track) {
        const key = `${app.channel.id}\u0000${trackKey(track)}`;
        if (key === shown) {
            return;
        }
        shown = key;
        events.emit('track', {
            track,
            lines: displayLines(track, app.channel.name),
            cover: coverFor(track, app.channel),
            channel: app.channel,
        });
    }

    const switcher = createSwitcher({show, now: () => Date.now(), setTimer: later, clearTimer: cancel});
    const poller = createPoller({
        api,
        getBufferS: () => player.bufferS(),
        getStream: () => {
            const index = app.channel.streams.indexOf(app.stream);
            return player.state() === 'playing' && index >= 0 ? index : null;
        },
        setTimer: later,
        clearTimer: cancel,
        onDate: (date) => {
            app.clockOffset = clockOffsetMs(date, Date.now());
        },
        onBody: (body) => {
            unreachable = false;
            switcher.update(body);
            app.history = body.history;
            events.emit('history', {history: body.history, channel: app.channel});
        },
        onError: () => {
            if (!unreachable) {
                unreachable = true;
                toast.show('Lost contact with tunebox, still trying.');
            }
        },
    });

    const probe = document.createElement('audio');
    const playable = (stream) => canPlay(stream, (type) => probe.canPlayType(type));

    app.selectChannel = (id) => {
        app.channel = channels.find((channel) => channel.id === id);
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
    app.step = (delta) => app.selectChannel(nextChannel(channels, app.channel.id, delta));
    app.play = () => {
        if (!playable(app.stream)) {
            // Never leave the previous station playing under this one's titles.
            player.stop();
            toast.show(`This browser cannot play ${app.stream.codec.toUpperCase()}. Pick another quality on the channel page.`, 8000);
            return;
        }
        storage.set('playing', 1);
        toast.show(`Loading ${app.channel.name}...`);
        player.play(app.stream.url);
    };
    app.stop = () => {
        storage.set('playing', 0);
        player.stop();
        toast.show('Player stopped.');
    };

    events.on('state', (state) => {
        if (state === 'playing') {
            toast.show(`Playing ${app.channel.name}...`);
            // The buffer is known now and decides when titles switch.
            poller.refresh();
        } else if (state === 'reconnecting') {
            toast.show('Reconnecting...', 0);
        }
    });
    events.on('autoplay-refused', () => storage.set('playing', 0));
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
    bindControls(app);
    bindTicker(root);
    bindLayout(app);
    bindDayNight(app);
    bindPalette(app);
    bindMedia(app);
    app.selectChannel(pickChannel(channels, params.get('channel'), storage.get('channel'), settings.default_channel));
    // The channel list marks the active channel, so it binds once one is selected.
    bindPages(app);
    bindVisualizer(app);
    // A refused autoplay (no user gesture yet) quietly leaves the play button.
    // play=1 comes from the main window when it hands playback to a small window.
    if (params.get('play') === '1' || storage.flag('playing')) {
        app.play();
    }
}

const root = document.querySelector('.tunebox');
if (root) {
    start(root);
}
