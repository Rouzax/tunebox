// The now-playing source a stream address gives away, so a new station's
// feed settings can fill themselves in. Only patterns seen on the tested
// stations (examples/stations-nl.toml) count; anything else gives null
// rather than a guess.

const QMUSIC = {live: 'qmusic_nl', nonstop: 'qnl_nonstop', fouteuur: 'qnl_foute_uur', top40: 'qnl_top40', thema: 'qnl_thema'};
const NPO = {radio1: 'nporadio1', radio2: 'nporadio2', '3fm': 'npo3fm'};

// Triton's feed knows a station by its mount. The .mp3 stream carries the
// mount's own name; AAC streams add AAC or _ADP (OWR_INTERNATIONALAAC,
// OWR_INTERNATIONAL_ADP), and SLAM!'s pair is SLAM_MP3 and SLAM_AAC.
function tritonMount(file) {
    const match = /^([A-Za-z0-9_]+)\.(mp3|aac)$/.exec(file);
    if (!match) {
        return null;
    }
    const [, name, extension] = match;
    if (extension === 'mp3') {
        return name;
    }
    if (/_AAC$/.test(name)) {
        return name.replace(/_AAC$/, '_MP3');
    }
    return name.replace(/_ADP$/, '').replace(/AAC$/, '') || null;
}

export function sourceFromStream(address) {
    let url;
    try {
        url = new URL(address);
    } catch (error) {
        return null;
    }
    const host = url.hostname.toLowerCase();
    const file = url.pathname.split('/').pop() || '';
    if (host === 'playerservices.streamtheworld.com' || host.endsWith('.live.streamtheworld.com')) {
        const mount = tritonMount(file);
        return mount ? {type: 'triton', mount} : null;
    }
    if (host === 'audio-streaming.qmusic.nl') {
        const match = /^Qmusic_nl_([a-z0-9]+?)(?:_high)?\.(?:aac|mp3)$/i.exec(file);
        const station = match && QMUSIC[match[1].toLowerCase()];
        return station ? {type: 'qmusic', station} : null;
    }
    if (host === 'icecast.omroep.nl') {
        const match = /^(radio1|radio2|3fm)-bb-(?:mp3|aac)$/.exec(file);
        return match ? {type: 'npo', site: NPO[match[1]]} : null;
    }
    return null;
}
