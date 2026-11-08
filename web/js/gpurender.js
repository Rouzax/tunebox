// Draws one shader program of one or two passes on a WebGL 2 context. The
// GPU test's probes and the player's GPU styles both draw through it, so
// what the test measures is what the player runs. Every program gets the
// same inputs: time, a drive that runs faster with the bass, the bass,
// mids and highs, the spectrum as a texture, two covers to fade between,
// the cover palette and, in a second pass, what the first pass drew.

export const SPECTRUM_BINS = 128;

// Fixed for every program; a framebuffer's own texture is never bound to
// one of these while that framebuffer is drawn into.
const UNIT = {spectrum: 0, cover: 1, coverOld: 2, input: 3};

// One triangle that covers the viewport, from gl_VertexID alone: no
// vertex buffer to bind.
const VERTEX = `#version 300 es
void main() {
    vec2 corner = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(corner * 2.0 - 1.0, 0.0, 1.0);
}`;

export const HEADER = `#version 300 es
precision highp float;
uniform vec2 uRes;
uniform float uTime;
uniform float uDrive;
uniform vec3 uBands;
uniform sampler2D uSpec;
uniform sampler2D uCover;
uniform sampler2D uCoverOld;
uniform float uFade;
uniform sampler2D uInput;
uniform vec3 uPal[3];
out vec4 outColor;
float spectrum(float x) { return texture(uSpec, vec2(x, 0.5)).r; }
vec3 cover(vec2 uv) { return mix(texture(uCoverOld, uv).rgb, texture(uCover, uv).rgb, uFade); }
`;

// The skin's accent and its two neighbours (car.css's --art-* initial
// values), for a caller without a palette of its own.
const DEFAULT_LOOK = {
    palette: new Float32Array([98 / 255, 168 / 255, 234 / 255, 155 / 255, 123 / 255, 240 / 255, 60 / 255, 198 / 255, 192 / 255]),
    fade: 1,
    drive: 0,
};

function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    return shader;
}

// Compiles and links every program, then asks for each link status, which
// waits for the driver to finish: performance.now() around this is the
// whole compile and link time.
function buildPrograms(gl, sources) {
    const built = sources.map((source) => {
        const program = gl.createProgram();
        const vertex = compile(gl, gl.VERTEX_SHADER, VERTEX);
        const fragment = compile(gl, gl.FRAGMENT_SHADER, source);
        gl.attachShader(program, vertex);
        gl.attachShader(program, fragment);
        gl.linkProgram(program);
        return {program, vertex, fragment};
    });
    for (const {program, fragment} of built) {
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
            const log = gl.getShaderInfoLog(fragment) || gl.getProgramInfoLog(program) || 'link failed';
            throw new Error(log);
        }
    }
    return built.map(({program, vertex, fragment}) => {
        gl.deleteShader(vertex);
        gl.deleteShader(fragment);
        return program;
    });
}

function texture(gl, unit, width, height, wrap, internal, format) {
    const made = gl.createTexture();
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(gl.TEXTURE_2D, made);
    gl.texImage2D(gl.TEXTURE_2D, 0, internal, width, height, 0, format, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, wrap);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, wrap);
    return made;
}

// A colour texture with its framebuffer, cleared to black, left unbound.
function target(gl, width, height) {
    const color = texture(gl, UNIT.input, width, height, gl.CLAMP_TO_EDGE, gl.RGBA8, gl.RGBA);
    const framebuffer = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, color, 0);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.bindTexture(gl.TEXTURE_2D, null);
    return {color, framebuffer};
}

function dropTarget(gl, made) {
    gl.deleteFramebuffer(made.framebuffer);
    gl.deleteTexture(made.color);
}

// kind: 'single' (one pass to the screen), 'feedback' (pass one draws over
// its own previous frame, pass two shows it) or 'field' (pass one fills a
// field, pass two shades it). sources: one fragment shader per pass, each
// starting with HEADER. Throws when one does not build.
export function createRenderer(gl, kind, sources) {
    const started = performance.now();
    const programs = buildPrograms(gl, sources);
    const setupMs = performance.now() - started;

    const vao = gl.createVertexArray();
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    const spectrumTexture = texture(gl, UNIT.spectrum, SPECTRUM_BINS, 1, gl.CLAMP_TO_EDGE, gl.R8, gl.RED);
    // The newest cover and the one before it, black until the first comes.
    // Mirrored, so a tunnel's seam and a kaleidoscope's edges meet.
    const covers = [
        texture(gl, UNIT.cover, 1, 1, gl.MIRRORED_REPEAT, gl.RGBA8, gl.RGBA),
        texture(gl, UNIT.coverOld, 1, 1, gl.MIRRORED_REPEAT, gl.RGBA8, gl.RGBA),
    ];
    let newest = 0;
    let targets = [];
    let width = 0;
    let height = 0;
    let flip = 0;

    const uniforms = programs.map((program) => {
        const at = (name) => gl.getUniformLocation(program, name);
        return {res: at('uRes'), time: at('uTime'), drive: at('uDrive'), bands: at('uBands'), spec: at('uSpec'), cover: at('uCover'),
            coverOld: at('uCoverOld'), fade: at('uFade'), input: at('uInput'), pal: at('uPal')};
    });

    function bind(unit, made) {
        gl.activeTexture(gl.TEXTURE0 + unit);
        gl.bindTexture(gl.TEXTURE_2D, made);
    }

    function pass(index, input, framebuffer, time, bands, look) {
        bind(UNIT.input, input);
        const u = uniforms[index];
        gl.useProgram(programs[index]);
        gl.uniform2f(u.res, width, height);
        gl.uniform1f(u.time, time);
        gl.uniform1f(u.drive, look.drive);
        gl.uniform3f(u.bands, bands.bass, bands.mid, bands.high);
        gl.uniform1f(u.fade, look.fade);
        gl.uniform3fv(u.pal, look.palette);
        gl.uniform1i(u.spec, UNIT.spectrum);
        gl.uniform1i(u.cover, UNIT.cover);
        gl.uniform1i(u.coverOld, UNIT.coverOld);
        gl.uniform1i(u.input, UNIT.input);
        gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
        gl.viewport(0, 0, width, height);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    return {
        setupMs,
        resize(w, h) {
            width = w;
            height = h;
            targets.forEach((made) => dropTarget(gl, made));
            targets = kind === 'feedback' ? [target(gl, w, h), target(gl, w, h)]
                : kind === 'field' ? [target(gl, w, h)] : [];
        },
        // source: an image or canvas. It becomes the newest cover; the
        // caller fades it in by moving look.fade from 0 to 1.
        cover(source) {
            newest = 1 - newest;
            bind(UNIT.cover, covers[newest]);
            gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, source);
            gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        },
        // time in seconds; bands {bass, mid, high} in 0..1; spectrum a
        // Uint8Array of SPECTRUM_BINS; look {palette, fade, drive}.
        draw(time, bands, spectrum, look = DEFAULT_LOOK) {
            gl.bindVertexArray(vao);
            bind(UNIT.spectrum, spectrumTexture);
            gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, SPECTRUM_BINS, 1, gl.RED, gl.UNSIGNED_BYTE, spectrum);
            bind(UNIT.cover, covers[newest]);
            bind(UNIT.coverOld, covers[1 - newest]);
            if (kind === 'single') {
                pass(0, null, null, time, bands, look);
                return;
            }
            if (kind === 'feedback') {
                // Ping-pong: read last frame's target, write the other.
                const from = targets[flip];
                const to = targets[1 - flip];
                flip = 1 - flip;
                pass(0, from.color, to.framebuffer, time, bands, look);
                pass(1, to.color, null, time, bands, look);
                return;
            }
            pass(0, null, targets[0].framebuffer, time, bands, look);
            pass(1, targets[0].color, null, time, bands, look);
        },
        dispose() {
            targets.forEach((made) => dropTarget(gl, made));
            targets = [];
            programs.forEach((program) => gl.deleteProgram(program));
            gl.deleteTexture(spectrumTexture);
            covers.forEach((made) => gl.deleteTexture(made));
            gl.deleteVertexArray(vao);
        },
    };
}
