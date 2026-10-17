// Own-canvas effects. audioMotion only draws spectra, so these read the
// sources through an AnalyserNode of their own. The ambient programs
// render at AMBIENT_RES on the long side and let CSS stretch the canvas:
// the stretch is both the soft look and what keeps them cheap.

const AMBIENT_RES = 128;
const AMBIENT_FRAME_MS = 1000 / 30;
// [x speed, y speed, phase] per blob, in cycles per drift unit.
const LAVA_BLOBS = [[0.13, 0.17, 0], [0.11, 0.07, 1.7], [0.07, 0.13, 3.1], [0.17, 0.09, 4.6]];

export function createEffects(motion, root, onLevel) {
    const host = root.querySelector('#analyzer');
    const canvas = document.createElement('canvas');
    canvas.className = 'car-fx';
    host.parentNode.insertBefore(canvas, host);
    const ctx = canvas.getContext('2d');
    // Milkdrop's previous frame; drawing a canvas onto itself is not reliable.
    const buffer = document.createElement('canvas');
    const bufferCtx = buffer.getContext('2d');
    const bands = {bass: 0, mid: 0, treble: 0};
    // Recent peak per band; levels are read relative to it, so loud and
    // quiet tracks both use the full range. The floor keeps silence from
    // being amplified into motion.
    const peaks = {bass: 0.2, mid: 0.2, treble: 0.2};
    let analyser = null;
    let wave = null;
    let spectrum = null;
    let program = null;
    let frame = 0;
    let last = 0;
    let clock = 0;
    let drift = 0;

    function connect() {
        if (analyser) {
            return;
        }
        const audioCtx = motion.audioCtx;
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 2048;
        wave = new Float32Array(analyser.fftSize);
        spectrum = new Uint8Array(analyser.frequencyBinCount);
        for (const source of motion.connectedSources) {
            source.connect(analyser);
        }
        // An analyser with no path to the destination may never be pulled.
        const mute = audioCtx.createGain();
        mute.gain.value = 0;
        analyser.connect(mute).connect(audioCtx.destination);
    }

    function bandLevel(fromHz, toHz) {
        const binHz = motion.audioCtx.sampleRate / analyser.fftSize;
        const from = Math.max(1, Math.round(fromHz / binHz));
        const to = Math.round(toHz / binHz);
        let sum = 0;
        for (let i = from; i <= to; i++) {
            sum += spectrum[i];
        }
        return sum / ((to - from + 1) * 255);
    }

    function readAudio() {
        analyser.getFloatTimeDomainData(wave);
        analyser.getByteFrequencyData(spectrum);
        for (const [name, from, to] of [['bass', 20, 250], ['mid', 250, 2000], ['treble', 2000, 8000]]) {
            const level = bandLevel(from, to);
            peaks[name] = Math.max(level, peaks[name] * 0.995, 0.2);
            bands[name] += (level / peaks[name] - bands[name]) * 0.3;
        }
    }

    // Read every frame: the --art-* properties cross-fade on a track change.
    function colors() {
        const styles = getComputedStyle(root);
        const first = styles.getPropertyValue('--art-1').trim() || '#62a8ea';
        return [first, styles.getPropertyValue('--art-2').trim() || first, styles.getPropertyValue('--art-3').trim() || first];
    }

    function drawScope() {
        const {width, height} = canvas;
        const dpr = pixelRatio();

        // Phosphor trail: fade the previous frame instead of clearing it.
        ctx.globalCompositeOperation = 'destination-out';
        ctx.fillStyle = 'rgba(0, 0, 0, .35)';
        ctx.fillRect(0, 0, width, height);
        ctx.globalCompositeOperation = 'source-over';

        // Start at a rising zero crossing so the trace stands still on steady tones.
        let start = 0;
        for (let i = 1; i < wave.length / 2; i++) {
            if (wave[i - 1] < 0 && wave[i] >= 0) {
                start = i;
                break;
            }
        }
        const span = wave.length / 2;
        const onair = root.querySelector('.onair').getBoundingClientRect();
        const mid = (onair.top + onair.height / 2 - canvas.getBoundingClientRect().top) * dpr;
        const amp = height * 0.36;

        const [c1, c2] = colors();
        const gradient = ctx.createLinearGradient(0, 0, width, 0);
        gradient.addColorStop(0, c1);
        gradient.addColorStop(0.5, c2);
        gradient.addColorStop(1, c1);

        ctx.beginPath();
        for (let i = 0; i < span; i++) {
            const x = (i / (span - 1)) * width;
            const y = mid - wave[start + i] * amp;
            if (i === 0) {
                ctx.moveTo(x, y);
            } else {
                ctx.lineTo(x, y);
            }
        }
        ctx.lineWidth = 2.5 * dpr;
        ctx.lineJoin = 'round';
        ctx.strokeStyle = gradient;
        ctx.shadowColor = c1;
        ctx.shadowBlur = 12 * dpr;
        ctx.stroke();
        ctx.shadowBlur = 0;
    }

    // Soft blobs drifting on Lissajous paths: bigger with the bass, faster
    // when the mids are busy.
    function drawLava(dt) {
        const {width, height} = canvas;
        const palette = colors();
        drift += dt * (0.25 + bands.mid * 1.5);
        ctx.clearRect(0, 0, width, height);
        ctx.globalCompositeOperation = 'lighter';
        ctx.globalAlpha = 0.55;
        const radius = Math.max(width, height) * (0.32 + bands.bass * 0.3);
        LAVA_BLOBS.forEach(([speedX, speedY, phase], i) => {
            const x = width * (0.5 + 0.38 * Math.sin(drift * speedX * 2 * Math.PI + phase));
            const y = height * (0.5 + 0.38 * Math.cos(drift * speedY * 2 * Math.PI + phase * 1.3));
            const gradient = ctx.createRadialGradient(x, y, 0, x, y, radius);
            gradient.addColorStop(0, palette[i % 3]);
            gradient.addColorStop(1, 'transparent');
            ctx.fillStyle = gradient;
            ctx.fillRect(0, 0, width, height);
        });
        ctx.globalAlpha = 1;
        ctx.globalCompositeOperation = 'source-over';
    }

    // MilkDrop's basic trick: redraw the last frame slightly zoomed, turned
    // and faded, then add a ring shaped by the waveform, so rings flow
    // outward and swirl. Bass drives the zoom, mids the turn.
    function drawMilkdrop(dt) {
        const {width, height} = canvas;
        if (buffer.width !== width || buffer.height !== height) {
            buffer.width = width;
            buffer.height = height;
        }
        bufferCtx.clearRect(0, 0, width, height);
        bufferCtx.drawImage(canvas, 0, 0);

        ctx.clearRect(0, 0, width, height);
        ctx.save();
        ctx.translate(width / 2, height / 2);
        ctx.rotate((0.15 + bands.mid * 0.8) * dt * Math.sin(clock * 0.07 + 1));
        const zoom = 1 + (0.25 + bands.bass * 1.2) * dt;
        ctx.scale(zoom, zoom);
        ctx.globalAlpha = Math.pow(0.35, dt);
        ctx.drawImage(buffer, -width / 2, -height / 2);
        ctx.restore();

        const points = 96;
        const step = Math.floor(wave.length / points);
        const radius = Math.min(width, height) * 0.16;
        ctx.beginPath();
        for (let i = 0; i <= points; i++) {
            const angle = (i / points) * 2 * Math.PI;
            const r = radius * (1 + wave[(i % points) * step] * 1.5);
            const x = width / 2 + Math.cos(angle) * r;
            const y = height / 2 + Math.sin(angle) * r;
            if (i === 0) {
                ctx.moveTo(x, y);
            } else {
                ctx.lineTo(x, y);
            }
        }
        ctx.lineWidth = 2;
        ctx.strokeStyle = colors()[Math.floor(clock / 4) % 3];
        ctx.stroke();
    }

    // Three slow curtains; each one's height and sway follows one band.
    function drawAurora() {
        const {width, height} = canvas;
        const palette = colors();
        const levels = [bands.bass, bands.mid, bands.treble];
        ctx.clearRect(0, 0, width, height);
        ctx.globalCompositeOperation = 'lighter';
        ctx.globalAlpha = 0.5;
        for (let i = 0; i < 3; i++) {
            const base = height * (0.25 + i * 0.2);
            const amp = height * (0.06 + levels[i] * 0.25);
            const gradient = ctx.createLinearGradient(0, base - amp * 2, 0, base + height * 0.35);
            gradient.addColorStop(0, 'transparent');
            gradient.addColorStop(0.3, palette[i]);
            gradient.addColorStop(1, 'transparent');
            ctx.fillStyle = gradient;
            ctx.beginPath();
            ctx.moveTo(0, height);
            for (let x = 0; x <= width; x += 4) {
                const sway = Math.sin((x / width) * 2 * Math.PI * (1 + i * 0.5) + clock * (0.3 + i * 0.17))
                    + 0.5 * Math.sin((x / width) * 3 - clock * 0.21);
                ctx.lineTo(x, base + sway * amp - amp);
            }
            ctx.lineTo(width, height);
            ctx.closePath();
            ctx.fill();
        }
        ctx.globalAlpha = 1;
        ctx.globalCompositeOperation = 'source-over';
    }

    const PROGRAMS = {
        scope: {ambient: false, draw: drawScope},
        lava: {ambient: true, draw: drawLava},
        milkdrop: {ambient: true, draw: drawMilkdrop},
        aurora: {ambient: true, draw: drawAurora},
    };

    // Low power (set by setupVisualizer) draws the oscilloscope at CSS pixels.
    const pixelRatio = () => (root.dataset.power === 'low' ? 1 : window.devicePixelRatio || 1);

    function resize() {
        const cssWidth = canvas.clientWidth || 1;
        const cssHeight = canvas.clientHeight || 1;
        let width;
        let height;
        if (program.ambient) {
            const scale = AMBIENT_RES / Math.max(cssWidth, cssHeight);
            width = Math.max(1, Math.round(cssWidth * scale));
            height = Math.max(1, Math.round(cssHeight * scale));
        } else {
            const dpr = pixelRatio();
            width = cssWidth * dpr | 0;
            height = cssHeight * dpr | 0;
        }
        if (canvas.width !== width || canvas.height !== height) {
            canvas.width = width;
            canvas.height = height;
        }
    }

    function loop(now) {
        frame = requestAnimationFrame(loop);
        if (program.ambient && now - last < AMBIENT_FRAME_MS) {
            return;
        }
        const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
        last = now;
        clock += dt;
        resize();
        readAudio();
        program.draw(dt);
        // Relative levels peak at 1; the glow is tuned for audioMotion's lower energy values.
        onLevel(bands.bass * 0.35);
    }

    return {
        start(name) {
            connect();
            if (program !== PROGRAMS[name]) {
                program = PROGRAMS[name];
                ctx.clearRect(0, 0, canvas.width, canvas.height);
            }
            if (!frame) {
                last = 0;
                frame = requestAnimationFrame(loop);
            }
        },
        stop() {
            cancelAnimationFrame(frame);
            frame = 0;
            program = null;
            ctx.clearRect(0, 0, canvas.width, canvas.height);
        },
        // A new audio element came in; read from it from now on.
        reconnect() {
            if (analyser) {
                analyser.disconnect();
                analyser = null;
            }
            if (program) {
                connect();
            }
        },
    };
}
