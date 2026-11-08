// The GPU styles' pure parts: the cover texture's size and fade, the
// palette colours as the shaders take them, and the band levels the
// shaders move with.
import {parseHexColor} from './colors.js';

// Any cover, an SVG logo included, is drawn to this square first.
export const COVER_SIZE = 256;
export const COVER_FADE_S = 1;

// 0 when a new cover has just come, 1 once COVER_FADE_S has passed. Both
// in milliseconds on the same clock.
export function coverFade(now, since) {
    return Math.max(0, Math.min(1, (now - since) / (COVER_FADE_S * 1000)));
}

function hslToRgb(hue, saturation, lightness) {
    const h = ((hue % 360) + 360) % 360;
    const a = saturation * Math.min(lightness, 1 - lightness);
    const channel = (n) => {
        const k = (n + h / 30) % 12;
        return lightness - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    };
    return [channel(0), channel(8), channel(4)];
}

// A computed --art-* value: rgb() where the property is registered (it
// cross-fades), else the hsl() palette.js wrote, or the #rrggbb fallback.
// Each channel in 0..1.
export function parseCssColor(text) {
    const value = typeof text === 'string' ? text.trim().toLowerCase() : '';
    const hex = parseHexColor(value);
    if (hex) {
        return hex.map((channel) => channel / 255);
    }
    const match = /^(rgba?|hsla?)\(([^)]*)\)$/.exec(value);
    if (!match) {
        return null;
    }
    const parts = match[2].split(/[\s,/]+/).filter(Boolean).slice(0, 3).map(parseFloat);
    if (parts.length < 3 || !parts.every((part) => Number.isFinite(part))) {
        return null;
    }
    if (match[1][0] === 'r') {
        return parts.map((channel) => Math.max(0, Math.min(255, channel)) / 255);
    }
    return hslToRgb(parts[0], parts[1] / 100, parts[2] / 100);
}

// Bass, mids and highs against their own recent peak, eased: loud and
// quiet tracks both use the full range, and the floor keeps silence from
// being amplified into motion. The same rule as effects.js.
export function createLevels() {
    const peaks = {bass: 0.2, mid: 0.2, high: 0.2};
    const out = {bass: 0, mid: 0, high: 0};
    return {
        update(raw) {
            for (const name of ['bass', 'mid', 'high']) {
                peaks[name] = Math.max(raw[name], peaks[name] * 0.995, 0.2);
                out[name] += (raw[name] / peaks[name] - out[name]) * 0.3;
            }
            return out;
        },
    };
}
