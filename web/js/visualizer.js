import AudioMotionAnalyzer from '../vendor/audiomotion-analyzer-4.5.4/audioMotion-analyzer.js';
import {createEffects} from './effects.js';
import {keepAudioRunning} from './reconnect.js';
import {accentColor} from './palette.js';

const ART_GRADIENT = 'car-art';
const ACCENT_GRADIENT = 'accent';
const BACKGROUND = '#011a35';
// Never changed by a style: no scale, no background fill, drawn over the page.
const BASE = {showScaleX: false, showBgColor: false, overlay: true};

// Every style sets every option any other style touches, so switching never
// inherits leftovers. Wave reproduces the stock template's own settings.
const RESET = {
    alphaBars: false, barSpace: 0.3, channelLayout: 'dual-combined', fadePeaks: false, fillAlpha: 0.4,
    frequencyScale: 'linear', ledBars: false, linearAmplitude: true, linearBoost: 2, lineWidth: 1,
    lumiBars: false, maxFreq: 16000, minFreq: 20, mirror: 0, mode: 10, outlineBars: false, peakLine: false,
    radial: false, radius: 0.3, reflexAlpha: 0.3, reflexBright: 1, reflexFit: true, reflexRatio: 0.2,
    roundBars: false, showPeaks: false, smoothing: 0.92, spinSpeed: 0, trueLeds: false,
    // The stock template's quality; LOW_POWER replaces these on slow hardware.
    fftSize: 8192, loRes: false, maxFPS: 0,
};

// Half-resolution canvas, a quarter of the FFT work per frame, 30 fps.
const LOW_POWER = {fftSize: 4096, loRes: true, maxFPS: 30};
// Below this the analyzer is struggling (a car head unit, not a phone).
const LOW_POWER_FPS = 40;

const STYLES = [
    {id: 'wave', label: 'Wave', art: true, options: {}},
    {
        id: 'bars', label: 'Spectrum bars', art: true, options: {
            mode: 4, frequencyScale: 'log', minFreq: 30, roundBars: true, barSpace: 0.35, showPeaks: true,
            fadePeaks: true, linearAmplitude: false, smoothing: 0.7, reflexRatio: 0.25, reflexAlpha: 0.25,
            channelLayout: 'single',
        },
    },
    {
        id: 'led', label: 'LED meter', art: false, gradient: 'classic', options: {
            mode: 5, frequencyScale: 'log', minFreq: 30, ledBars: true, trueLeds: true, showPeaks: true,
            barSpace: 0.25, linearAmplitude: false, smoothing: 0.6, reflexRatio: 0, channelLayout: 'single',
        },
    },
    {
        id: 'butterfly', label: 'Butterfly', art: true, options: {
            mode: 6, frequencyScale: 'log', minFreq: 30, roundBars: true, barSpace: 0.3, alphaBars: true,
            linearAmplitude: false, smoothing: 0.7, reflexRatio: 0.3, reflexAlpha: 0.35,
            channelLayout: 'dual-horizontal', mirror: -1,
        },
    },
    {
        // radius is replaced by placeRing() once the canvas sits around the cover.
        id: 'ring', label: 'Ring', art: true, ring: true, options: {
            radial: true, mode: 5, frequencyScale: 'log', minFreq: 30, barSpace: 0.2, spinSpeed: 1,
            linearAmplitude: false, smoothing: 0.7, reflexRatio: 0, channelLayout: 'single',
        },
    },
    {
        id: 'neon', label: 'Neon', art: true, options: {
            mode: 6, frequencyScale: 'log', minFreq: 30, outlineBars: true, fillAlpha: 0.1, lineWidth: 2,
            barSpace: 0.3, showPeaks: true, linearAmplitude: false, smoothing: 0.7, reflexRatio: 0.25,
            reflexAlpha: 0.25, channelLayout: 'single',
        },
    },
    {
        id: 'ambient', label: 'Ambient wall', art: true, options: {
            mode: 5, frequencyScale: 'log', minFreq: 30, lumiBars: true, barSpace: 0.08, linearAmplitude: false,
            smoothing: 0.85, reflexRatio: 0, channelLayout: 'single',
        },
    },
    {id: 'scope', label: 'Oscilloscope', effect: 'scope', options: {}},
    {id: 'lava', label: 'Lava lamp', effect: 'lava', options: {}},
    {id: 'milkdrop', label: 'Milkdrop', effect: 'milkdrop', options: {}},
    {id: 'aurora', label: 'Aurora', effect: 'aurora', options: {}},
    {id: 'off', label: 'Off', off: true, options: {}},
];

const STYLE_IDS = STYLES.map((style) => style.id);
const LOW_POWER_CHECK_MS = 3000;

// The spectrum styles come from audioMotion, the effects from effects.js.
// Web Audio starts only after the listener's first tap or key press: an
// AudioContext made earlier stays suspended, and a stream routed through a
// suspended context plays silence. Until then, and with Off, the stream
// plays straight from the audio element.
export function bindVisualizer(app) {
    const {root, events, storage, player, toast} = app;
    const toggle = root.querySelector('.viz-toggle');
    const menu = root.querySelector('.viz-list');
    const analyzer = root.querySelector('#analyzer');
    const artBox = root.querySelector('.artwork-img');
    let index = STYLE_IDS.indexOf(storage.choice('visualizer', STYLE_IDS, 'wave'));
    let motion = null;
    let effects = null;
    let artStops = null;
    let lowPower = false;
    let gestured = false;
    let available = true;
    let resumeAudio = () => {};

    function ensureMotion() {
        if (motion || !gestured || !available || !player.element()) {
            return motion;
        }
        motion = new AudioMotionAnalyzer(analyzer, {...BASE, ...RESET, source: player.element()});
        motion.registerGradient(ACCENT_GRADIENT, {bgColor: BACKGROUND, colorStops: [accentColor(root)]});
        if (artStops) {
            motion.registerGradient(ART_GRADIENT, {bgColor: BACKGROUND, colorStops: artStops});
        }
        motion.setOptions({onCanvasDraw: (instance) => app.setBass(instance.getEnergy('bass'))});
        resumeAudio = keepAudioRunning(motion.audioCtx, () => player.state() !== 'stopped');
        effects = createEffects(motion, root, app.setBass);
        checkPower();
        return motion;
    }

    // Moves the canvas to a square around the cover and sizes the ring's
    // hole to the cover; audioMotion's inner radius is 0.375 x radius x the
    // canvas's shorter side. The cover is measured against its own page,
    // because the pages slide sideways (translate) while the menu is open.
    function placeRing() {
        if (!motion) {
            return;
        }
        const host = analyzer.offsetParent;
        const page = root.querySelector('.page.main');
        const container = root.querySelector('.main-container > .container');
        if (!host || !artBox || !page || !container) {
            return;
        }
        const hostRect = host.getBoundingClientRect();
        const art = artBox.getBoundingClientRect();
        const pageRect = page.getBoundingClientRect();
        const resting = container.getBoundingClientRect();
        const side = Math.min(art.width * 2.3, hostRect.width);
        const centerX = art.left - pageRect.left + resting.left + art.width / 2 - hostRect.left;
        const centerY = art.top - pageRect.top + resting.top + art.height / 2 - hostRect.top;
        Object.assign(analyzer.style, {
            top: `${centerY - side / 2}px`, left: `${centerX - side / 2}px`,
            width: `${side}px`, height: `${side}px`, right: 'auto', bottom: 'auto',
        });
        motion.setOptions({radius: (art.width / 2) / (0.375 * side)});
    }

    function clearRing() {
        for (const prop of ['top', 'left', 'width', 'height', 'right', 'bottom']) {
            analyzer.style.removeProperty(prop);
        }
    }

    function apply() {
        const style = STYLES[index];
        root.dataset.viz = style.id;
        root.toggleAttribute('data-effect', Boolean(style.effect));
        markActive();
        if (style.off) {
            if (motion) {
                motion.toggleAnalyzer(false);
                effects.stop();
            }
            clearRing();
            app.setBass(0, true);
            return;
        }
        if (!ensureMotion()) {
            // Applied again once Web Audio can start.
            return;
        }
        if (style.effect) {
            motion.toggleAnalyzer(false);
            clearRing();
            effects.start(style.effect);
            return;
        }
        effects.stop();
        let gradient = ACCENT_GRADIENT;
        if (style.gradient) {
            gradient = style.gradient;
        } else if (style.art && artStops) {
            gradient = ART_GRADIENT;
        }
        motion.setOptions({...RESET, ...style.options, ...(lowPower ? LOW_POWER : {}), gradient});
        if (style.ring) {
            placeRing();
        } else {
            clearRing();
        }
        motion.toggleAnalyzer(true);
    }

    // Two slow samples in a row while the analyzer runs switch it to low
    // power for the rest of the visit; three fast ones end the check.
    function checkPower() {
        let slow = 0;
        let fast = 0;
        const check = window.setInterval(() => {
            if (!motion.isOn || !motion.fps) {
                return;
            }
            if (motion.fps < LOW_POWER_FPS) {
                fast = 0;
                slow += 1;
                if (slow >= 2) {
                    window.clearInterval(check);
                    lowPower = true;
                    root.dataset.power = 'low';
                    apply();
                }
            } else {
                slow = 0;
                fast += 1;
                if (fast >= 3) {
                    window.clearInterval(check);
                }
            }
        }, LOW_POWER_CHECK_MS);
    }

    const items = STYLES.map((style, i) => {
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = style.label;
        item.appendChild(button);
        button.addEventListener('click', () => {
            index = i;
            storage.set('visualizer', style.id);
            apply();
            app.showPage('main');
        });
        menu.appendChild(item);
        return item;
    });
    function markActive() {
        items.forEach((item, i) => item.classList.toggle('active', i === index));
    }

    toggle.classList.remove('hidden');
    toggle.addEventListener('click', () => app.showPage(app.page() === 'viz' ? 'main' : 'viz'));

    const onGesture = () => {
        if (gestured) {
            return;
        }
        gestured = true;
        if (player.state() !== 'stopped') {
            apply();
        }
    };
    for (const type of ['pointerdown', 'keydown', 'touchstart']) {
        document.addEventListener(type, onGesture, {capture: true, passive: true});
    }

    events.on('state', (state) => {
        if (state === 'loading' || state === 'playing') {
            if (motion) {
                resumeAudio();
            } else {
                apply();
            }
        }
    });
    events.on('audio', ({element, cors}) => {
        available = cors;
        if (!motion) {
            return;
        }
        motion.disconnectInput();
        if (cors) {
            motion.connectInput(element);
            effects.reconnect();
            apply();
        } else {
            motion.toggleAnalyzer(false);
            effects.stop();
            toast.show('No visualizer for this stream.');
        }
    });
    events.on('palette', (palette) => {
        artStops = palette ? palette.map(([h, s]) => `hsl(${h}, ${s}%, 60%)`) : null;
        if (motion && artStops) {
            motion.registerGradient(ART_GRADIENT, {bgColor: BACKGROUND, colorStops: artStops});
        }
        apply();
    });
    if (window.ResizeObserver && artBox) {
        new ResizeObserver(() => {
            if (STYLES[index].ring) {
                placeRing();
            }
        }).observe(root);
    }
    apply();
}
