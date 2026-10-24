import {paletteFromPixels, paletteFromRgb, parseHexColor} from './colors.js';

const SAMPLE = 32;
const FALLBACK_ACCENT = '#62a8ea';

function pixelsOf(img) {
    const canvas = document.createElement('canvas');
    canvas.width = SAMPLE;
    canvas.height = SAMPLE;
    const ctx = canvas.getContext('2d', {willReadFrequently: true});
    ctx.drawImage(img, 0, 0, SAMPLE, SAMPLE);
    return ctx.getImageData(0, 0, SAMPLE, SAMPLE).data;
}

// Covers, logos and default images all come from tunebox, so the canvas can
// read them; a read that fails anyway leaves the accent colours in place.
function readPalette(img) {
    try {
        return Promise.resolve(paletteFromPixels(pixelsOf(img)));
    } catch (error) {
        return Promise.resolve(null);
    }
}

// The canvas normalises any CSS colour to #rrggbb.
export function accentColor(root) {
    const accent = getComputedStyle(root).getPropertyValue('--accent-color').trim();
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.fillStyle = '#000001';
    ctx.fillStyle = accent || FALLBACK_ACCENT;
    return ctx.fillStyle === '#000001' ? FALLBACK_ACCENT : ctx.fillStyle;
}

function accentPalette(root) {
    const rgb = parseHexColor(accentColor(root));
    return rgb ? paletteFromRgb(rgb[0], rgb[1], rgb[2]) : null;
}

// Publishes the palette as --art-1..3 (solid) and --art-glow for car.css.
function publishPalette(root, palette) {
    palette.forEach(([h, s], i) => root.style.setProperty(`--art-${i + 1}`, `hsl(${h}, ${s}%, 55%)`));
    const [h, s] = palette[0];
    root.style.setProperty('--art-glow', `hsla(${h}, ${s}%, 55%, .5)`);
}

// The drifting background and the cover glow in the cover's colours. A
// colourless cover uses the accent colour and its hue neighbours.
export function bindPalette(app) {
    const {root, events} = app;
    const artwork = root.querySelector('img.artwork');
    const ambient = document.createElement('div');
    ambient.className = 'car-ambient';
    root.insertBefore(ambient, root.firstChild);
    root.classList.add('car-ambient-on', 'car-glow-on');

    let bass = 0;
    // Eased, so single kicks swell rather than flash.
    app.setBass = (level, immediate = false) => {
        bass = immediate ? level : bass + (level - bass) * 0.2;
        root.style.setProperty('--bass', bass.toFixed(3));
    };

    const fallback = accentPalette(root);
    if (fallback) {
        publishPalette(root, fallback);
    }
    artwork.addEventListener('load', () => {
        readPalette(artwork).then((palette) => {
            const shown = palette || fallback;
            if (shown) {
                publishPalette(root, shown);
            }
            events.emit('palette', palette);
        });
    });
}
