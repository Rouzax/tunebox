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
