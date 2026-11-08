// The shaders of the player's GPU styles, in GLSL ES 3.00 on gpurender.js's
// HEADER. Liquid ink's field pass is the GPU test's heavy probe, so the
// car's numbers for that probe are the numbers for Liquid ink.
import {HEADER} from './gpurender.js';

export const NOISE = `
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

// A domain-warped field, three six-octave fbm calls deep: the height in r,
// the first warp's length in g, the second warp in b.
export const WARP_FIELD = `${HEADER}${NOISE}
void main() {
    vec2 p = gl_FragCoord.xy / min(uRes.x, uRes.y) * 3.0;
    float t = uTime * 0.15;
    vec2 q = vec2(fbm(p + vec2(0.0, t)), fbm(p + vec2(5.2, 1.3 - t)));
    vec2 r = vec2(fbm(p + 4.0 * q + vec2(1.7, 9.2) + uBands.x), fbm(p + 4.0 * q + vec2(8.3, 2.8) - t));
    float f = fbm(p + 4.0 * r);
    outColor = vec4(f, length(q), r.x, 1.0);
}`;

// Cover tunnel. The cover lines a tunnel that rushes forward with the
// bass; the frame before, turned and pulled toward the centre, leaves
// trails behind it.
const TUNNEL_WARP = `${HEADER}
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 p = (gl_FragCoord.xy * 2.0 - uRes) / min(uRes.x, uRes.y);
    float r = length(p);
    float a = atan(p.y, p.x);
    vec2 wall = vec2(a / 3.14159265, 0.4 / (r + 0.05) + uTime * 0.3 + uBands.x * 0.2);
    vec3 color = cover(wall) * smoothstep(0.05, 0.8, r);
    float turn = 0.004 + 0.02 * uBands.y;
    vec2 q = mat2(cos(turn), -sin(turn), sin(turn), cos(turn)) * (uv - 0.5) * (0.975 - 0.02 * uBands.x);
    vec3 previous = texture(uInput, q + 0.5).rgb * 0.92;
    outColor = vec4(max(previous, color * (0.5 + 0.6 * uBands.x)), 1.0);
}`;

// The spectrum as a ring of light round the tunnel's mouth, in the cover's
// first colour; the corners darken.
const TUNNEL_SHOW = `${HEADER}
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 p = (gl_FragCoord.xy * 2.0 - uRes) / min(uRes.x, uRes.y);
    vec3 color = texture(uInput, uv).rgb;
    float level = spectrum(abs(atan(p.y, p.x)) / 3.14159265);
    float ring = 1.0 - smoothstep(0.0, 0.04, abs(length(p) - 0.85 - 0.3 * level));
    color += ring * uPal[0] * (0.35 + 0.8 * uBands.z);
    color *= 1.0 - 0.4 * smoothstep(0.9, 1.8, length(p));
    outColor = vec4(color, 1.0);
}`;

// Cover kaleidoscope. The angle folds into six mirrored segments of the
// cover, turning slowly; the bass zooms in and blends the new image in
// harder over the fading one before.
const KALEIDO_WARP = `${HEADER}
const float SEGMENT = 6.2831853 / 6.0;
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 p = (gl_FragCoord.xy * 2.0 - uRes) / min(uRes.x, uRes.y);
    float r = length(p);
    float a = mod(atan(p.y, p.x) + uTime * 0.05, SEGMENT);
    a = abs(a - SEGMENT * 0.5);
    float zoom = 0.55 + 0.1 * sin(uTime * 0.13) - 0.12 * uBands.x;
    float spin = uTime * 0.02;
    vec2 k = mat2(cos(spin), -sin(spin), sin(spin), cos(spin)) * (vec2(cos(a), sin(a)) * r * zoom) + 0.5;
    vec3 glass = cover(k);
    vec2 q = (uv - 0.5) * (0.985 + 0.01 * uBands.y) + 0.5;
    vec3 previous = texture(uInput, q).rgb * 0.85;
    outColor = vec4(mix(previous, glass, 0.35 + 0.4 * uBands.x), 1.0);
}`;

// A glow in the cover's second colour at the centre with the highs; the
// corners darken.
const KALEIDO_SHOW = `${HEADER}
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 p = (gl_FragCoord.xy * 2.0 - uRes) / min(uRes.x, uRes.y);
    vec3 color = texture(uInput, uv).rgb;
    color += uPal[1] * 0.3 * uBands.z * (1.0 - smoothstep(0.0, 0.5, length(p)));
    color *= 1.0 - 0.45 * smoothstep(0.7, 1.7, length(p));
    outColor = vec4(color, 1.0);
}`;

// Synthwave road. A grid road runs to a low sun: sky and sun from the
// cover's colours, mountains shaped by the spectrum, the grid faster with
// the bass (through uDrive: scaling uTime by the bass would jump the grid
// further on every beat the longer the page is open). Every part is worked out for every pixel and one is picked at
// the end: fwidth needs the grid in uniform control flow.
const SYNTHWAVE = `${HEADER}
const float HORIZON = -0.1;
void main() {
    vec2 p = (gl_FragCoord.xy * 2.0 - uRes) / min(uRes.x, uRes.y);
    float above = p.y - HORIZON;

    vec3 sky = mix(uPal[1] * 0.8, uPal[2] * 0.15, smoothstep(0.0, 1.2, above));
    vec2 s = p - vec2(0.0, HORIZON + 0.42);
    float radius = 0.36 + 0.05 * uBands.x;
    float sun = 1.0 - smoothstep(radius - 0.01, radius, length(s));
    // Slits across the sun's lower half, wider toward the bottom, drifting down.
    float slits = s.y < 0.0 ? step(fract(s.y * 12.0 + uTime * 0.3), clamp(1.0 + s.y * 2.2, 0.0, 1.0)) : 1.0;
    vec3 sunColor = mix(uPal[0], vec3(1.0, 0.92, 0.6), smoothstep(-0.3, 0.4, s.y));
    sky = mix(sky, sunColor, sun * slits);

    // Mountains from the spectrum, none in the middle where the road runs.
    float x = abs(p.x);
    float side = smoothstep(0.15, 0.9, x);
    float ridge = HORIZON + (0.03 + 0.28 * spectrum(clamp(x * 0.5, 0.0, 1.0))) * side;
    float mountain = 1.0 - smoothstep(ridge - 0.004, ridge + 0.004, p.y);
    float rim = (1.0 - smoothstep(0.0, 0.012, abs(p.y - ridge))) * side;
    sky = mix(sky, uPal[2] * 0.12, mountain) + rim * uPal[0] * 0.8;

    // The grid in perspective: distance grows toward the horizon.
    float below = max(HORIZON - p.y, 0.002);
    vec2 grid = vec2(p.x / below, 1.0 / below + uDrive * 1.2) * 0.35;
    vec2 toLine = 0.5 - abs(fract(grid) - 0.5);
    vec2 lines = 1.0 - smoothstep(vec2(0.0), fwidth(grid) * 1.5 + 0.0001, toLine);
    float line = max(lines.x, lines.y) * smoothstep(0.0, 0.3, below);
    vec3 ground = uPal[2] * 0.06 + line * uPal[0] * (0.6 + 0.9 * uBands.x);
    ground += uPal[0] * 0.6 * exp(-below * 14.0);

    outColor = vec4(above > 0.0 ? sky : ground, 1.0);
}`;

// Liquid ink: the heavy probe's field, lit from its slope and coloured from
// the cover.
const INK_SHADE = `${HEADER}${NOISE}
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 texel = 1.0 / uRes;
    vec3 field = texture(uInput, uv).rgb;
    float dx = texture(uInput, uv + vec2(texel.x, 0.0)).r - texture(uInput, uv - vec2(texel.x, 0.0)).r;
    float dy = texture(uInput, uv + vec2(0.0, texel.y)).r - texture(uInput, uv - vec2(0.0, texel.y)).r;
    vec3 normal = normalize(vec3(-dx * 40.0, -dy * 40.0, 1.0));
    float light = clamp(dot(normal, normalize(vec3(0.4, 0.6, 0.7))), 0.0, 1.0);
    float detail = fbm(uv * 12.0 + field.gb * 3.0 + uTime * 0.1);
    vec3 color = mix(uPal[2] * 0.2, uPal[0], field.r);
    color = mix(color, uPal[1], field.b * field.b * (0.5 + uBands.y));
    color *= 0.5 + 0.7 * light + 0.3 * detail;
    color += 0.15 * spectrum(uv.x) * uBands.z * uPal[1];
    outColor = vec4(color, 1.0);
}`;

export const SHADERS = {
    tunnel: {kind: 'feedback', sources: [TUNNEL_WARP, TUNNEL_SHOW]},
    kaleido: {kind: 'feedback', sources: [KALEIDO_WARP, KALEIDO_SHOW]},
    synthwave: {kind: 'single', sources: [SYNTHWAVE]},
    ink: {kind: 'field', sources: [WARP_FIELD, INK_SHADE]},
};
