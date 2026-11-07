// The visualizer's styles as data: id, label, the group whose tile holds
// it, the engine that draws it (audioMotion, effects.js, MilkDrop or none)
// and audioMotion's options; plus which of them a device can show and
// which one a stored choice means.
import {PRESETS, SHUFFLE_ID} from './milkdropcore.js';

export const ART_GRADIENT = 'car-art';
export const ACCENT_GRADIENT = 'accent';
export const BACKGROUND = '#011a35';
// Never changed by a style: no scale, no background fill, drawn over the page.
export const BASE = {showScaleX: false, showBgColor: false, overlay: true};

// Every style sets every option any other style touches, so switching never
// inherits leftovers. Wave reproduces the stock template's own settings.
export const RESET = {
    alphaBars: false, barSpace: 0.3, channelLayout: 'dual-combined', fadePeaks: false, fillAlpha: 0.4,
    frequencyScale: 'linear', ledBars: false, linearAmplitude: true, linearBoost: 2, lineWidth: 1,
    lumiBars: false, maxFreq: 16000, minFreq: 20, mirror: 0, mode: 10, outlineBars: false, peakLine: false,
    radial: false, radius: 0.3, reflexAlpha: 0.3, reflexBright: 1, reflexFit: true, reflexRatio: 0.2,
    roundBars: false, showPeaks: false, smoothing: 0.92, spinSpeed: 0, trueLeds: false,
    // The stock template's quality; LOW_POWER replaces these on slow hardware.
    fftSize: 8192, loRes: false, maxFPS: 0,
};

// Half-resolution canvas, a quarter of the FFT work per frame, 30 fps.
export const LOW_POWER = {fftSize: 4096, loRes: true, maxFPS: 30};
// Below this the analyzer is struggling (a car head unit, not a phone).
export const LOW_POWER_FPS = 40;

export const STYLES = [
    {id: 'wave', group: 'waves', engine: 'motion', label: 'Wave', art: true, options: {}},
    {
        id: 'bars', group: 'bars', engine: 'motion', label: 'Spectrum bars', art: true, options: {
            mode: 4, frequencyScale: 'log', minFreq: 30, roundBars: true, barSpace: 0.35, showPeaks: true,
            fadePeaks: true, linearAmplitude: false, smoothing: 0.7, reflexRatio: 0.25, reflexAlpha: 0.25,
            channelLayout: 'single',
        },
    },
    {
        id: 'led', group: 'bars', engine: 'motion', label: 'LED meter', art: false, gradient: 'classic', options: {
            mode: 5, frequencyScale: 'log', minFreq: 30, ledBars: true, trueLeds: true, showPeaks: true,
            barSpace: 0.25, linearAmplitude: false, smoothing: 0.6, reflexRatio: 0, channelLayout: 'single',
        },
    },
    {
        id: 'butterfly', group: 'bars', engine: 'motion', label: 'Butterfly', art: true, options: {
            mode: 6, frequencyScale: 'log', minFreq: 30, roundBars: true, barSpace: 0.3, alphaBars: true,
            linearAmplitude: false, smoothing: 0.7, reflexRatio: 0.3, reflexAlpha: 0.35,
            channelLayout: 'dual-horizontal', mirror: -1,
        },
    },
    {
        // radius is replaced by placeRing() once the canvas sits around the cover.
        id: 'ring', group: 'bars', engine: 'motion', label: 'Ring', art: true, ring: true, options: {
            radial: true, mode: 5, frequencyScale: 'log', minFreq: 30, barSpace: 0.2, spinSpeed: 1,
            linearAmplitude: false, smoothing: 0.7, reflexRatio: 0, channelLayout: 'single',
        },
    },
    {
        id: 'neon', group: 'bars', engine: 'motion', label: 'Neon', art: true, options: {
            mode: 6, frequencyScale: 'log', minFreq: 30, outlineBars: true, fillAlpha: 0.1, lineWidth: 2,
            barSpace: 0.3, showPeaks: true, linearAmplitude: false, smoothing: 0.7, reflexRatio: 0.25,
            reflexAlpha: 0.25, channelLayout: 'single',
        },
    },
    {
        id: 'ambient', group: 'bars', engine: 'motion', label: 'Ambient wall', art: true, options: {
            mode: 5, frequencyScale: 'log', minFreq: 30, lumiBars: true, barSpace: 0.08, linearAmplitude: false,
            smoothing: 0.85, reflexRatio: 0, channelLayout: 'single',
        },
    },
    {id: 'scope', group: 'waves', engine: 'effects', label: 'Oscilloscope', effect: 'scope', options: {}},
    {id: 'lava', group: 'ambient', engine: 'effects', label: 'Lava lamp', effect: 'lava', options: {}},
    {id: 'aurora', group: 'ambient', engine: 'effects', label: 'Aurora', effect: 'aurora', options: {}},
    {id: 'swirl', group: 'ambient', engine: 'effects', label: 'Swirl', effect: 'swirl', options: {}},
    {id: SHUFFLE_ID, group: 'milkdrop', engine: 'milkdrop', label: 'Shuffle', shuffle: true, options: {}},
].concat(PRESETS.map((preset) => ({
    id: preset.id, group: 'milkdrop', engine: 'milkdrop', label: preset.label, preset: preset.file, options: {},
})), [
    {id: 'off', group: null, engine: 'off', label: 'Off', off: true, options: {}},
]);

export const GROUPS = [
    {id: 'bars', label: 'Bars'},
    {id: 'waves', label: 'Waves'},
    {id: 'ambient', label: 'Ambient'},
    {id: 'milkdrop', label: 'MilkDrop'},
].map((group) => Object.assign({image: `icons/viz/${group.id}.webp`}, group));

// MilkDrop needs WebGL 2; without it its tile and styles are left out.
export function availableStyles(gl2) {
    return gl2 ? STYLES : STYLES.filter((style) => style.engine !== 'milkdrop');
}

export function groupsFor(gl2) {
    return gl2 ? GROUPS : GROUPS.filter((group) => group.id !== 'milkdrop');
}

export function groupOf(id) {
    const style = STYLES.filter((candidate) => candidate.id === id)[0];
    return style ? style.group : null;
}

// The 2D effect once called Milkdrop is Swirl. A MilkDrop preset no
// longer shipped falls back to Shuffle; anything else unknown, or MilkDrop
// without WebGL 2, to Wave.
export function chooseStyle(stored, gl2) {
    const id = stored === 'milkdrop' ? 'swirl' : stored;
    if (availableStyles(gl2).some((style) => style.id === id)) {
        return id;
    }
    return gl2 && typeof id === 'string' && id.indexOf('md-') === 0 ? SHUFFLE_ID : 'wave';
}
