// The Ambient group's GPU styles: one WebGL 2 canvas drawing a program of
// gpushaders.js through gpurender.js, fed by an analyser of its own on the
// sources audioMotion reads, textured with the cover the player shows and
// coloured from its palette. The context exists only while a GPU style
// shows: stop() gives it back, so a phone never holds it beside
// MilkDrop's.
import {bands, shrink} from './gpucore.js';
import {MAX_SCALE, createPace, renderSize, startScale} from './gpuframe.js';
import {SPECTRUM_BINS, createRenderer} from './gpurender.js';
import {SHADERS} from './gpushaders.js';
import {COVER_SIZE, coverFade, createLevels, parseCssColor} from './gpustylecore.js';

const FFT_SIZE = 512;
// The palette changes only in a two-second cross-fade, and each read is a
// style lookup: every few frames is enough.
const PALETTE_EVERY = 6;
const GL_OPTIONS = {alpha: false, antialias: false, depth: false, stencil: false};
const PALETTE_PROPERTIES = ['--art-1', '--art-2', '--art-3'];
// The drive (uDrive) runs at real time in silence and up to this much
// faster at full bass. A frame after a pause counts at most MAX_STEP_S, so
// the picture does not leap on return.
const DRIVE_BASS = 2.5;
const MAX_STEP_S = 0.1;

// onLevel(glow) drives the cover glow; onFail('gpu') when a style cannot
// start or its context is lost; held() true keeps the size (lab mode).
export function createGpuStyles({motion, root, onLevel, onFail, held = () => false}) {
    const host = root.querySelector('#analyzer');
    // Every cover is drawn to one square first, so an SVG logo or a cover
    // of any size becomes the same texture.
    const coverCanvas = document.createElement('canvas');
    coverCanvas.width = COVER_SIZE;
    coverCanvas.height = COVER_SIZE;
    const coverCtx = coverCanvas.getContext('2d');
    const spectrum = new Uint8Array(SPECTRUM_BINS);
    const levels = createLevels();
    const palette = new Float32Array(9);
    const started = performance.now();
    const look = {palette, fade: 1, drive: 0};
    let lastFrame = null;
    let coverImage = null;
    let coverSince = 0;
    let canvas = null;
    let gl = null;
    let renderer = null;
    let shader = null;
    let analyser = null;
    let bins = null;
    let connected = [];
    let frame = 0;
    let frames = 0;
    let scale = 1;
    // The smallest size a step-down has reached in this page view.
    let floor = 1;
    let pace = createPace();
    let size = null;
    // The style and scale showing; asking for the same again (a new cover
    // re-applies the style) changes nothing.
    let showing = null;

    function connect() {
        if (analyser) {
            return;
        }
        const audioCtx = motion.audioCtx;
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = FFT_SIZE;
        bins = new Uint8Array(analyser.frequencyBinCount);
        // An analyser with no path to the destination may never be pulled.
        const mute = audioCtx.createGain();
        mute.gain.value = 0;
        analyser.connect(mute).connect(audioCtx.destination);
        connected = motion.connectedSources.slice();
        for (const source of connected) {
            source.connect(analyser);
        }
    }

    function disconnect() {
        for (const source of connected) {
            try {
                source.disconnect(analyser);
            } catch (error) {
                // A source already gone with its audio element.
            }
        }
        analyser.disconnect();
        analyser = null;
        connected = [];
    }

    function upload() {
        if (!renderer || !coverImage) {
            return;
        }
        try {
            coverCtx.clearRect(0, 0, COVER_SIZE, COVER_SIZE);
            coverCtx.drawImage(coverImage, 0, 0, COVER_SIZE, COVER_SIZE);
        } catch (error) {
            // An image that cannot be drawn keeps the cover before it.
            return;
        }
        renderer.cover(coverCanvas);
        coverSince = performance.now();
    }

    function halt() {
        cancelAnimationFrame(frame);
        frame = 0;
        showing = null;
    }

    function release() {
        if (renderer) {
            renderer.dispose();
            renderer = null;
            shader = null;
        }
        if (canvas) {
            // Before the loss, so giving the context back is not reported.
            canvas.removeEventListener('webglcontextlost', lost);
        }
        if (gl) {
            const lose = gl.getExtension('WEBGL_lose_context');
            if (lose) {
                lose.loseContext();
            }
            gl = null;
        }
        if (canvas) {
            canvas.remove();
            canvas = null;
        }
        size = null;
    }

    function lost() {
        halt();
        release();
        onFail('gpu');
    }

    function ensureContext() {
        if (gl) {
            return true;
        }
        canvas = document.createElement('canvas');
        // car-fx: placed, faded and dimmed like the other own-canvas styles.
        canvas.className = 'car-fx car-gpu';
        host.parentNode.insertBefore(canvas, host);
        gl = canvas.getContext('webgl2', GL_OPTIONS);
        if (!gl) {
            return false;
        }
        canvas.addEventListener('webglcontextlost', lost);
        return true;
    }

    function build(id) {
        if (shader === id) {
            return true;
        }
        if (renderer) {
            renderer.dispose();
            renderer = null;
            shader = null;
        }
        try {
            renderer = createRenderer(gl, SHADERS[id].kind, SHADERS[id].sources);
        } catch (error) {
            return false;
        }
        shader = id;
        size = null;
        upload();
        return true;
    }

    function resize() {
        const cssWidth = canvas.clientWidth;
        const cssHeight = canvas.clientHeight;
        if (size && size.cssWidth === cssWidth && size.cssHeight === cssHeight && size.scale === scale) {
            return;
        }
        size = {cssWidth, cssHeight, scale};
        const {width, height} = renderSize(cssWidth, cssHeight, window.devicePixelRatio || 1, scale);
        canvas.width = width;
        canvas.height = height;
        renderer.resize(width, height);
        canvas.dataset.gpuScale = String(scale);
    }

    function readPalette() {
        const styles = getComputedStyle(root);
        PALETTE_PROPERTIES.forEach((name, i) => {
            const rgb = parseCssColor(styles.getPropertyValue(name));
            if (rgb) {
                palette.set(rgb, i * 3);
            }
        });
    }

    function loop(now) {
        frame = requestAnimationFrame(loop);
        if (pace.frame(now) && !held() && scale < MAX_SCALE) {
            scale += 1;
            floor = scale;
            showing.scale = scale;
        }
        resize();
        analyser.getByteFrequencyData(bins);
        shrink(bins, spectrum);
        const level = levels.update(bands(bins, motion.audioCtx.sampleRate, FFT_SIZE));
        if (frames % PALETTE_EVERY === 0) {
            readPalette();
        }
        const step = lastFrame === null ? 0 : Math.min(MAX_STEP_S, Math.max(0, now - lastFrame) / 1000);
        lastFrame = now;
        look.drive += step * (1 + DRIVE_BASS * level.bass);
        look.fade = coverFade(now, coverSince);
        renderer.draw((now - started) / 1000, level, spectrum, look);
        frames += 1;
        canvas.dataset.gpuFrames = String(frames);
        // Relative levels peak at 1; the glow is tuned for audioMotion's lower energy values.
        onLevel(level.bass * 0.35);
    }

    return {
        start(style, lowPower) {
            const wanted = startScale(style.scale, lowPower, floor);
            if (showing && showing.id === style.id && showing.scale === wanted) {
                return;
            }
            halt();
            connect();
            if (!ensureContext() || !build(style.shader)) {
                release();
                onFail('gpu');
                return;
            }
            scale = wanted;
            showing = {id: style.id, scale};
            pace = createPace();
            frame = requestAnimationFrame(loop);
        },
        stop() {
            halt();
            release();
        },
        // image: the cover element; drawn now if a style shows, else when one starts.
        cover(image) {
            coverImage = image;
            upload();
        },
        // A new audio element came in; read from it from now on.
        reconnect() {
            if (analyser) {
                disconnect();
                connect();
            }
        },
    };
}
