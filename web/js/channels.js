// Channel and stream choices.

export function groupChannels(channels) {
    const groups = [];
    const byName = new Map();
    const ungrouped = {name: null, channels: []};
    for (const channel of channels) {
        if (!channel.group) {
            ungrouped.channels.push(channel);
            continue;
        }
        if (!byName.has(channel.group)) {
            const group = {name: channel.group, channels: []};
            byName.set(channel.group, group);
            groups.push(group);
        }
        byName.get(channel.group).channels.push(channel);
    }
    return ungrouped.channels.length ? [ungrouped, ...groups] : groups;
}

// The channel page's first view when there is a choice of groups: one tile
// per ungrouped station, then one per group, in listing order. null keeps
// the plain list (fewer than two groups).
export function tilesFor(channels) {
    const groups = groupChannels(channels).filter((group) => group.name !== null);
    if (groups.length < 2) {
        return null;
    }
    const tiles = [];
    for (const channel of channels) {
        if (!channel.group) {
            tiles.push({kind: 'station', id: channel.id, name: channel.name, image: channel.logo || null});
        }
    }
    for (const group of groups) {
        const first = group.channels[0];
        const image = first.group_image_small || first.group_image || null;
        tiles.push({
            kind: 'group',
            name: group.name,
            count: group.channels.length,
            image,
            mosaic: image ? null : mosaicFor(group.channels),
        });
    }
    return tiles;
}

// The group the channel page keeps open after the station list changed: the
// same one while it still has a station and there are tiles to return to;
// null shows the tiles, or the plain list when there are fewer than two
// groups. A renamed group counts as gone.
export function openGroupAfter(channels, openGroup) {
    if (openGroup === null || !tilesFor(channels)) {
        return null;
    }
    return channels.some((channel) => channel.group === openGroup) ? openGroup : null;
}

// A group's tile without an image of its own: its first four stations' logos.
export function mosaicFor(channels) {
    return channels.slice(0, 4).map((c) => c.logo_small || c.logo).filter(Boolean);
}

export function nextChannel(channels, currentId, step) {
    const index = channels.findIndex((channel) => channel.id === currentId);
    if (index < 0) {
        return channels[0].id;
    }
    return channels[(index + step + channels.length) % channels.length].id;
}

export function streamLabel(stream) {
    const parts = [];
    if (stream.codec) {
        parts.push(stream.codec.toUpperCase());
    }
    if (stream.kbps) {
        parts.push(String(stream.kbps));
    }
    return parts.length ? `${stream.name} (${parts.join(' ')})` : stream.name;
}

// The remembered stream if this browser can play it, else the first it can
// play, else the first (it will report that it cannot play).
export function pickStream(channel, savedName, playable = () => true) {
    const saved = channel.streams.find((stream) => stream.name === savedName);
    if (saved && playable(saved)) {
        return saved;
    }
    return channel.streams.find(playable) || channel.streams[0];
}

// After the channel list changed: the same channel and stream by id and
// name, another stream when that one is gone (replaced: the audio must
// switch too), null when the channel is gone.
export function keepSelection(channels, channelId, streamName, playable = () => true) {
    const channel = channels.find((candidate) => candidate.id === channelId);
    if (!channel) {
        return null;
    }
    const same = channel.streams.find((stream) => stream.name === streamName);
    return {channel, stream: same || pickStream(channel, null, playable), replaced: !same};
}

export function pickChannel(channels, requested, saved, fallback) {
    for (const id of [requested, saved, fallback]) {
        if (id && channels.some((channel) => channel.id === id)) {
            return id;
        }
    }
    return channels[0].id;
}

const MIME_TYPES = {aac: 'audio/aac', mp3: 'audio/mpeg'};

// Whether this browser can play the stream's codec; streams without a codec
// in the config are tried.
export function canPlay(stream, canPlayType) {
    const type = stream.codec ? MIME_TYPES[stream.codec] : null;
    return !type || canPlayType(type) !== '';
}

// The station list is out of date when /now names another configuration
// version, PIN revision or logo and default image revision than the list
// was loaded with.
export function listOutdated(body, app) {
    return (body.version !== undefined && body.version !== app.version)
        || (body.pin !== undefined && body.pin !== app.pinRevision)
        || (body.images !== undefined && body.images !== app.images);
}
