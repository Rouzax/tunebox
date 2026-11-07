// The GPU test's own probe shaders, WebGL 2 and GLSL ES 3.00. Each stands
// for a class of possible visualizer styles: light (a plasma field: plasma,
// aurora, a synthwave road), cover (a tunnel textured with an image over a
// feedback buffer, the structure of Milkdrop's warp and composite passes)
// and heavy (domain-warped six-octave noise in two passes: liquid and
// fluid styles, the heaviest presets). All of them follow the analyser:
// bass, mids and highs as uniforms and the spectrum as a texture.

export const SPECTRUM_BINS = 128;

// One triangle that covers the viewport, from gl_VertexID alone: no
// vertex buffer to bind.
const VERTEX = `#version 300 es
void main() {
    vec2 corner = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(corner * 2.0 - 1.0, 0.0, 1.0);
}`;

const HEADER = `#version 300 es
precision highp float;
uniform vec2 uRes;
uniform float uTime;
uniform vec3 uBands;
uniform sampler2D uSpec;
out vec4 outColor;
float spectrum(float x) { return texture(uSpec, vec2(x, 0.5)).r; }
`;

const PLASMA = `${HEADER}
void main() {
    vec2 p = (gl_FragCoord.xy * 2.0 - uRes) / min(uRes.x, uRes.y);
    float t = uTime * 0.6;
    float v = sin(p.x * 3.0 + t)
        + sin(p.y * 4.0 - t * 1.3)
        + sin((p.x + p.y) * 2.5 + t * 0.7)
        + sin(length(p) * 6.0 - t * 2.0 - uBands.x * 4.0);
    vec3 color = 0.5 + 0.5 * cos(v * 1.6 + vec3(0.0, 2.1, 4.2) + uBands.y * 2.0);
    float level = spectrum(fract(abs(p.x) * 0.5));
    outColor = vec4(color * (0.55 + 0.5 * level + 0.4 * uBands.x), 1.0);
}`;

// The previous frame, turned and pulled toward the centre, under a tunnel
// of the cover image.
const TUNNEL_WARP = `${HEADER}
uniform sampler2D uPrev;
uniform sampler2D uCover;
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 p = (gl_FragCoord.xy * 2.0 - uRes) / min(uRes.x, uRes.y);
    float r = length(p);
    float a = atan(p.y, p.x);
    vec2 tunnel = vec2(0.35 / (r + 0.05) + uTime * 0.4 + uBands.x * 0.3, a / 6.2831853 + uTime * 0.05);
    vec3 wall = texture(uCover, tunnel).rgb * smoothstep(0.0, 0.7, r);
    float turn = 0.01 + 0.03 * uBands.y;
    vec2 q = mat2(cos(turn), -sin(turn), sin(turn), cos(turn)) * (uv - 0.5) * (0.97 - 0.03 * uBands.x);
    vec3 previous = texture(uPrev, q + 0.5).rgb * 0.94;
    outColor = vec4(max(previous, wall * (0.45 + uBands.x)), 1.0);
}`;

const TUNNEL_COMPOSITE = `${HEADER}
uniform sampler2D uFrame;
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec3 color = texture(uFrame, uv).rgb;
    color += 0.25 * spectrum(uv.x) * smoothstep(0.25, 0.0, uv.y) * vec3(0.4, 0.7, 1.0);
    outColor = vec4(pow(color, vec3(0.9)) * (1.0 + 0.3 * uBands.z), 1.0);
}`;

const NOISE = `
float hash(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}
float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
               mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
}
float fbm(vec2 p) {
    float value = 0.0;
    float amplitude = 0.5;
    mat2 turn = mat2(1.6, 1.2, -1.2, 1.6);
    for (int i = 0; i < 6; i++) {
        value += amplitude * noise(p);
        p = turn * p;
        amplitude *= 0.5;
    }
    return value;
}
`;

// Pass one: the warped field, three six-octave fbm calls deep.
const WARP_FIELD = `${HEADER}${NOISE}
void main() {
    vec2 p = gl_FragCoord.xy / min(uRes.x, uRes.y) * 3.0;
    float t = uTime * 0.15;
    vec2 q = vec2(fbm(p + vec2(0.0, t)), fbm(p + vec2(5.2, 1.3 - t)));
    vec2 r = vec2(fbm(p + 4.0 * q + vec2(1.7, 9.2) + uBands.x), fbm(p + 4.0 * q + vec2(8.3, 2.8) - t));
    float f = fbm(p + 4.0 * r);
    outColor = vec4(f, length(q), r.x, 1.0);
}`;

// Pass two: light from the field's slope, colour from one more fbm.
const WARP_SHADE = `${HEADER}${NOISE}
uniform sampler2D uField;
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 step = 1.0 / uRes;
    vec3 field = texture(uField, uv).rgb;
    float dx = texture(uField, uv + vec2(step.x, 0.0)).r - texture(uField, uv - vec2(step.x, 0.0)).r;
    float dy = texture(uField, uv + vec2(0.0, step.y)).r - texture(uField, uv - vec2(0.0, step.y)).r;
    vec3 normal = normalize(vec3(-dx * 40.0, -dy * 40.0, 1.0));
    float light = clamp(dot(normal, normalize(vec3(0.4, 0.6, 0.7))), 0.0, 1.0);
    float detail = fbm(uv * 12.0 + field.gb * 3.0 + uTime * 0.1);
    vec3 color = mix(vec3(0.05, 0.1, 0.3), vec3(0.2, 0.8, 0.9), field.r);
    color = mix(color, vec3(1.0, 0.5, 0.2), field.b * field.b * (0.5 + uBands.y));
    color *= 0.5 + 0.7 * light + 0.3 * detail;
    color += 0.2 * spectrum(uv.x) * uBands.z;
    outColor = vec4(color, 1.0);
}`;

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

function texture(gl, unit, width, height, filter, wrap) {
    const made = gl.createTexture();
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(gl.TEXTURE_2D, made);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, wrap);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, wrap);
    return made;
}

// A colour texture with its framebuffer, at the render size.
function target(gl, unit, width, height) {
    const color = texture(gl, unit, width, height, gl.LINEAR, gl.CLAMP_TO_EDGE);
    const framebuffer = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, color, 0);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return {color, framebuffer};
}

function dropTarget(gl, made) {
    if (made) {
        gl.deleteFramebuffer(made.framebuffer);
        gl.deleteTexture(made.color);
    }
}

// kind: 'light', 'cover' or 'heavy'; cover: a canvas or image for the
// tunnel's walls. Throws when a shader does not compile or link.
export function createProbe(gl, kind, cover) {
    const sources = {light: [PLASMA], cover: [TUNNEL_WARP, TUNNEL_COMPOSITE], heavy: [WARP_FIELD, WARP_SHADE]}[kind];
    const started = performance.now();
    const programs = buildPrograms(gl, sources);
    const setupMs = performance.now() - started;

    const vao = gl.createVertexArray();
    // Unit 0: the spectrum, one row of SPECTRUM_BINS bytes.
    const spectrumTexture = gl.createTexture();
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, spectrumTexture);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, SPECTRUM_BINS, 1, 0, gl.RED, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    // Unit 1: the cover, repeated along the tunnel.
    let coverTexture = null;
    if (kind === 'cover') {
        coverTexture = texture(gl, 1, 1, 1, gl.LINEAR, gl.REPEAT);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, cover);
    }
    // Units 2 and 3: the feedback pair, or the noise field.
    let targets = [];
    let width = 0;
    let height = 0;
    let flip = 0;

    const uniforms = programs.map((program) => {
        const at = (name) => gl.getUniformLocation(program, name);
        return {res: at('uRes'), time: at('uTime'), bands: at('uBands'), spec: at('uSpec'),
            prev: at('uPrev'), cover: at('uCover'), frame: at('uFrame'), field: at('uField')};
    });

    function use(index, time, bands) {
        const u = uniforms[index];
        gl.useProgram(programs[index]);
        gl.uniform2f(u.res, width, height);
        gl.uniform1f(u.time, time);
        gl.uniform3f(u.bands, bands.bass, bands.mid, bands.high);
        gl.uniform1i(u.spec, 0);
        return u;
    }

    function pass(framebuffer) {
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
            targets = kind === 'cover' ? [target(gl, 2, w, h), target(gl, 3, w, h)]
                : kind === 'heavy' ? [target(gl, 2, w, h)] : [];
        },
        // time in seconds; bands {bass, mid, high} in 0..1; spectrum a
        // Uint8Array of SPECTRUM_BINS.
        draw(time, bands, spectrum) {
            gl.bindVertexArray(vao);
            gl.activeTexture(gl.TEXTURE0);
            gl.bindTexture(gl.TEXTURE_2D, spectrumTexture);
            gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, SPECTRUM_BINS, 1, gl.RED, gl.UNSIGNED_BYTE, spectrum);
            if (kind === 'light') {
                use(0, time, bands);
                pass(null);
                return;
            }
            if (kind === 'cover') {
                // Ping-pong: read last frame's target, write the other.
                const from = targets[flip];
                const to = targets[1 - flip];
                flip = 1 - flip;
                gl.activeTexture(gl.TEXTURE1);
                gl.bindTexture(gl.TEXTURE_2D, coverTexture);
                gl.activeTexture(gl.TEXTURE2);
                gl.bindTexture(gl.TEXTURE_2D, from.color);
                let u = use(0, time, bands);
                gl.uniform1i(u.cover, 1);
                gl.uniform1i(u.prev, 2);
                pass(to.framebuffer);
                gl.activeTexture(gl.TEXTURE2);
                gl.bindTexture(gl.TEXTURE_2D, to.color);
                u = use(1, time, bands);
                gl.uniform1i(u.frame, 2);
                pass(null);
                return;
            }
            let u = use(0, time, bands);
            pass(targets[0].framebuffer);
            gl.activeTexture(gl.TEXTURE2);
            gl.bindTexture(gl.TEXTURE_2D, targets[0].color);
            u = use(1, time, bands);
            gl.uniform1i(u.field, 2);
            pass(null);
        },
        dispose() {
            targets.forEach((made) => dropTarget(gl, made));
            targets = [];
            programs.forEach((program) => gl.deleteProgram(program));
            gl.deleteTexture(spectrumTexture);
            if (coverTexture) {
                gl.deleteTexture(coverTexture);
            }
            gl.deleteVertexArray(vao);
        },
    };
}
