// The GPU test's own probe shaders. Each stands for a class of possible
// visualizer styles: light (a plasma field: plasma, aurora, a synthwave
// road), cover (a tunnel textured with an image over a feedback buffer,
// the structure of Milkdrop's warp and composite passes) and heavy
// (domain-warped six-octave noise in two passes, the field Liquid ink
// draws). They draw through gpurender.js, as the player's GPU styles do.
import {HEADER, createRenderer} from './gpurender.js';
import {NOISE, WARP_FIELD} from './gpushaders.js';

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
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 p = (gl_FragCoord.xy * 2.0 - uRes) / min(uRes.x, uRes.y);
    float r = length(p);
    float a = atan(p.y, p.x);
    vec2 tunnel = vec2(0.35 / (r + 0.05) + uTime * 0.4 + uBands.x * 0.3, a / 6.2831853 + uTime * 0.05);
    vec3 wall = texture(uCover, tunnel).rgb * smoothstep(0.0, 0.7, r);
    float turn = 0.01 + 0.03 * uBands.y;
    vec2 q = mat2(cos(turn), -sin(turn), sin(turn), cos(turn)) * (uv - 0.5) * (0.97 - 0.03 * uBands.x);
    vec3 previous = texture(uInput, q + 0.5).rgb * 0.94;
    outColor = vec4(max(previous, wall * (0.45 + uBands.x)), 1.0);
}`;

const TUNNEL_COMPOSITE = `${HEADER}
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec3 color = texture(uInput, uv).rgb;
    color += 0.25 * spectrum(uv.x) * smoothstep(0.25, 0.0, uv.y) * vec3(0.4, 0.7, 1.0);
    outColor = vec4(pow(color, vec3(0.9)) * (1.0 + 0.3 * uBands.z), 1.0);
}`;

// Light from the field's slope, colour from one more fbm.
const WARP_SHADE = `${HEADER}${NOISE}
void main() {
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 step = 1.0 / uRes;
    vec3 field = texture(uInput, uv).rgb;
    float dx = texture(uInput, uv + vec2(step.x, 0.0)).r - texture(uInput, uv - vec2(step.x, 0.0)).r;
    float dy = texture(uInput, uv + vec2(0.0, step.y)).r - texture(uInput, uv - vec2(0.0, step.y)).r;
    vec3 normal = normalize(vec3(-dx * 40.0, -dy * 40.0, 1.0));
    float light = clamp(dot(normal, normalize(vec3(0.4, 0.6, 0.7))), 0.0, 1.0);
    float detail = fbm(uv * 12.0 + field.gb * 3.0 + uTime * 0.1);
    vec3 color = mix(vec3(0.05, 0.1, 0.3), vec3(0.2, 0.8, 0.9), field.r);
    color = mix(color, vec3(1.0, 0.5, 0.2), field.b * field.b * (0.5 + uBands.y));
    color *= 0.5 + 0.7 * light + 0.3 * detail;
    color += 0.2 * spectrum(uv.x) * uBands.z;
    outColor = vec4(color, 1.0);
}`;

const PROBES = {
    light: {kind: 'single', sources: [PLASMA]},
    cover: {kind: 'feedback', sources: [TUNNEL_WARP, TUNNEL_COMPOSITE]},
    heavy: {kind: 'field', sources: [WARP_FIELD, WARP_SHADE]},
};

// kind: 'light', 'cover' or 'heavy'; cover: a canvas or image for the
// tunnel's walls. Throws when a shader does not compile or link.
export function createProbe(gl, kind, cover) {
    const probe = PROBES[kind];
    const renderer = createRenderer(gl, probe.kind, probe.sources);
    renderer.cover(cover);
    return renderer;
}
