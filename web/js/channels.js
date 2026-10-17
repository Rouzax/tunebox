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

export function pickStream(channel, savedName) {
    return channel.streams.find((stream) => stream.name === savedName) || channel.streams[0];
}

export function pickChannel(channels, requested, saved, fallback) {
    for (const id of [requested, saved, fallback]) {
        if (id && channels.some((channel) => channel.id === id)) {
            return id;
        }
    }
    return channels[0].id;
}
