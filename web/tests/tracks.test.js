import assert from 'node:assert/strict';
import {test} from 'node:test';
import {
    DEFAULT_COVER, clockOffsetMs, coverFor, displayLines, nextCover, pageTitle, timeAgo, trackKey,
} from '../js/tracks.js';

const TRACK = {artist: 'ADRIATIQUE, WHOMADEWHO', title: 'Miracle', played_by: 'WhoMadeWho Crystal Garden TML 24', started_at: 1000, cover: 'https://example.invalid/c.jpg'};

test('a track gets three lines', () => {
    assert.deepEqual(displayLines(TRACK, 'One World Radio'), {
        title: 'Miracle',
        artist: 'ADRIATIQUE, WHOMADEWHO',
        playedBy: 'WhoMadeWho Crystal Garden TML 24',
        badge: '',
    });
});

test('a badge comes with the title', () => {
    const chart = {...TRACK, title: 'Sad Girls', played_by: null, badge: '#8'};
    assert.deepEqual(displayLines(chart, 'OWR'), {title: 'Sad Girls', artist: TRACK.artist, playedBy: '', badge: '#8'});
});

test('no current track shows the channel name', () => {
    assert.deepEqual(displayLines(null, 'Daybreak Sessions'), {title: 'Daybreak Sessions', artist: '', playedBy: '', badge: ''});
});

test('a trailing separator on the artist is trimmed', () => {
    assert.equal(displayLines({...TRACK, artist: 'MR. BELT & WEZOL, ', played_by: null}, 'x').artist, 'MR. BELT & WEZOL');
});

test('track keys separate tracks and station IDs', () => {
    assert.notEqual(trackKey(TRACK), trackKey({...TRACK, started_at: 2000}));
    assert.equal(trackKey(null), 'station');
});

test('page title', () => {
    assert.equal(pageTitle(displayLines(TRACK, 'OWR'), 'OWR'), 'ADRIATIQUE, WHOMADEWHO - Miracle | OWR');
    assert.equal(pageTitle(displayLines(null, 'OWR'), 'OWR'), 'OWR');
});

test('time ago, including a device clock that runs behind', () => {
    const now = 10 * 3600 * 1000;
    assert.equal(timeAgo(now - 30 * 1000, now), 'just now');
    assert.equal(timeAgo(now + 5 * 60 * 1000, now), 'just now');
    assert.equal(timeAgo(now - 3 * 60 * 1000, now), '3 min ago');
    assert.equal(timeAgo(now - 2 * 3600 * 1000, now), '2 h ago');
    assert.equal(timeAgo(now - 50 * 3600 * 1000, now), '2 d ago');
    assert.equal(timeAgo(Number.NaN, now), 'just now');
});

test('the server Date header corrects a wrong device clock', () => {
    const server = Date.parse('Sat, 17 Oct 2026 13:00:00 GMT');
    assert.equal(clockOffsetMs('Sat, 17 Oct 2026 13:00:00 GMT', server - 3 * 3600 * 1000), 3 * 3600 * 1000);
    assert.equal(clockOffsetMs(null, server), 0);
    assert.equal(clockOffsetMs('garbage', server), 0);
});

test('cover falls back to the channel image, then the logo, then the default', () => {
    const channel = {default_image: null, logo: '/logos/owr.png'};
    assert.equal(coverFor(TRACK, channel), TRACK.cover);
    assert.equal(coverFor({...TRACK, cover: null}, channel), '/logos/owr.png');
    assert.equal(coverFor(null, {default_image: 'https://example.invalid/d.png', logo: '/logos/owr.png'}), 'https://example.invalid/d.png');
    assert.equal(coverFor(null, {default_image: null, logo: null}), DEFAULT_COVER);
});

test('a cover that fails gives way to the next image down to the built-in one', () => {
    const channel = {default_image: 'https://example.invalid/d.png', logo: '/logos/owr.png'};
    assert.equal(nextCover(TRACK.cover, channel), channel.default_image);
    assert.equal(nextCover(channel.default_image, channel), channel.logo);
    assert.equal(nextCover(channel.logo, channel), DEFAULT_COVER);
    assert.equal(nextCover(DEFAULT_COVER, channel), null);
    assert.equal(nextCover(TRACK.cover, {default_image: null, logo: null}), DEFAULT_COVER);
});
