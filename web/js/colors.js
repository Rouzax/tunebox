// Colours for the album-art palette: up to three hues from a cover's pixels
// (or from the accent colour for a colourless cover), as [hue, saturation%].

export function rgbToHsl(r, g, b) {
    r /= 255;
    g /= 255;
    b /= 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const l = (max + min) / 2;
    if (max === min) {
        return [0, 0, l];
    }
    const d = max - min;
    const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    let h;
    if (max === r) {
        h = (g - b) / d + (g < b ? 6 : 0);
    } else if (max === g) {
        h = (b - r) / d + 2;
    } else {
        h = (r - g) / d + 4;
    }
    return [h / 6, s, l];
}

export function hueDistance(a, b) {
    const d = Math.abs(a - b) % 360;
    return d > 180 ? 360 - d : d;
}

// Returns three [hue, saturation%] pairs, the cover's most prominent colour
// first, or null for a near-greyscale cover. data is RGBA pixels.
export function paletteFromPixels(data) {
    const HUE_BINS = 24;
    // Hue is averaged as a vector: red sits at both 0 and 1, and a plain mean of
    // the two lands on cyan.
    const bins = Array.from({length: HUE_BINS}, () => ({weight: 0, x: 0, y: 0, s: 0, n: 0}));
    let total = 0;
    for (let i = 0; i < data.length; i += 4) {
        const [h, s, l] = rgbToHsl(data[i], data[i + 1], data[i + 2]);
        // Near-grey, near-black and near-white pixels carry no usable hue.
        if (s < 0.25 || l < 0.12 || l > 0.92) {
            continue;
        }
        const bin = bins[Math.floor(h * HUE_BINS) % HUE_BINS];
        const weight = s * (1 - Math.abs(l - 0.5));
        bin.weight += weight;
        bin.x += Math.cos(h * 2 * Math.PI);
        bin.y += Math.sin(h * 2 * Math.PI);
        bin.s += s;
        bin.n += 1;
        total += weight;
    }
    if (!total) {
        return null;
    }

    // A colour straddling a bin edge splits its weight, so each bin is judged
    // and averaged together with its two neighbours.
    const around = (i) => [-1, 0, 1].map((d) => bins[(i + d + HUE_BINS) % HUE_BINS]);
    const colourAt = (i) => {
        const group = around(i);
        const sum = (key) => group.reduce((acc, b) => acc + b[key], 0);
        return [(Math.atan2(sum('y'), sum('x')) * 180 / Math.PI + 360) % 360, sum('s') / sum('n')];
    };
    const ranked = bins
        .map((_, i) => [around(i).reduce((acc, b) => acc + b.weight, 0), i])
        .sort((a, b) => b[0] - a[0]);

    // Accents count from a small share, but only as a different colour, not
    // as another shade of one already picked.
    const picks = [];
    for (const [weight, i] of ranked) {
        if (picks.length === 3 || weight < total * 0.03) {
            break;
        }
        const colour = colourAt(i);
        if (picks.every(([hue]) => hueDistance(hue, colour[0]) >= 60)) {
            picks.push(colour);
        }
    }
    // A one-colour cover gets its neighbours on the hue circle, so the
    // three-colour effects keep the cover's mood.
    const [baseHue, baseSat] = picks[0];
    for (const shift of [30, -30]) {
        if (picks.length < 3) {
            picks.push([(baseHue + shift + 360) % 360, baseSat]);
        }
    }
    // Saturation floored so every colour reads on the dark background.
    return picks.map(([hue, sat]) => [Math.round(hue), Math.round(Math.max(sat, 0.6) * 100)]);
}

// The same shape as paletteFromPixels for one colour (the skin accent):
// the colour and its two neighbours on the hue circle.
export function paletteFromRgb(r, g, b) {
    const [h, sat] = rgbToHsl(r, g, b);
    const hue = Math.round(h * 360);
    const saturation = Math.round(Math.max(sat, 0.6) * 100);
    return [[hue, saturation], [(hue + 30) % 360, saturation], [(hue + 330) % 360, saturation]];
}

export function parseHexColor(text) {
    const hex = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(text.trim());
    return hex ? hex.slice(1).map((v) => parseInt(v, 16)) : null;
}
