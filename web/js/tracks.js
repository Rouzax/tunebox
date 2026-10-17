// What to show for a track. The server has already split the Played-by
// credit off the title and recognised station IDs (current is null then).
export const DEFAULT_COVER = 'icons/cover.svg';

export function displayLines(track, channelName) {
    if (!track || !track.title) {
        return {title: channelName, artist: '', playedBy: ''};
    }
    return {
        title: track.title,
        // A trailing separator is left on some multi-artist entries.
        artist: (track.artist || '').replace(/[\s,]+$/, ''),
        playedBy: track.played_by ? `Played by ${track.played_by}` : '',
    };
}

export function trackKey(track) {
    return track ? [track.artist, track.title, track.started_at].join('\u0000') : 'station';
}

export function pageTitle(lines, channelName) {
    return lines.artist ? `${lines.artist} - ${lines.title} | ${channelName}` : channelName;
}

export function timeAgo(startedAtMs, nowMs) {
    const minutes = Math.floor((nowMs - startedAtMs) / 60000);
    // Also catches a start in the future and NaN.
    if (!(minutes >= 1)) {
        return 'just now';
    }
    if (minutes < 60) {
        return `${minutes} min ago`;
    }
    const hours = Math.floor(minutes / 60);
    return hours < 24 ? `${hours} h ago` : `${Math.floor(hours / 24)} d ago`;
}

// The server's Date header against the device clock, so "x min ago" stays
// right on a device whose clock is off.
export function clockOffsetMs(dateHeader, localNowMs) {
    const server = dateHeader ? Date.parse(dateHeader) : Number.NaN;
    return Number.isFinite(server) ? server - localNowMs : 0;
}

export function coverFor(track, channel) {
    return (track && track.cover) || channel.default_image || channel.logo || DEFAULT_COVER;
}
