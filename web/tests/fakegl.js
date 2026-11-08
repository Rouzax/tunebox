// A WebGL 2 context made of plain objects, for engine tests in Node. It
// keeps which texture each unit holds and which texture each framebuffer
// draws into, records every draw, upload and uniform, and says yes to
// every build unless told otherwise. Constants are their own names, but
// TEXTURE0 is 0, so TEXTURE0 + n names unit n.
export function fakeGl({buildFails = false} = {}) {
    let made = 0;
    let unit = 0;
    let framebuffer = null;
    const units = {};
    const attached = new Map();
    const record = {draws: [], uploads: new Map(), uniforms: {}, lost: false, programs: 0};
    const calls = {
        activeTexture(n) {
            unit = n;
        },
        bindTexture(target, texture) {
            units[unit] = texture;
        },
        bindFramebuffer(target, bound) {
            framebuffer = bound;
        },
        framebufferTexture2D(target, attachment, textarget, texture) {
            attached.set(framebuffer, texture);
        },
        texImage2D(...args) {
            const source = args[args.length - 1];
            if (source && typeof source === 'object' && !ArrayBuffer.isView(source)) {
                record.uploads.set(units[unit], source);
            }
        },
        drawArrays() {
            record.draws.push({writing: framebuffer === null ? null : attached.get(framebuffer), units: Object.assign({}, units)});
        },
        createProgram() {
            record.programs += 1;
            return {program: record.programs};
        },
        getProgramParameter: () => !buildFails,
        getShaderInfoLog: () => 'fake compile error',
        getProgramInfoLog: () => '',
        getUniformLocation: (program, name) => name,
        getExtension: (name) => (name === 'WEBGL_lose_context' ? {loseContext() {
            record.lost = true;
        }} : null),
        uniform1f(location, value) {
            record.uniforms[location] = value;
        },
        uniform2f(location, x, y) {
            record.uniforms[location] = [x, y];
        },
        uniform3fv(location, value) {
            record.uniforms[location] = Array.from(value);
        },
    };
    return new Proxy(record, {
        get(target, key) {
            if (key in calls) {
                return calls[key];
            }
            if (key in target) {
                return target[key];
            }
            if (key === 'TEXTURE0') {
                return 0;
            }
            if (typeof key === 'string' && /^[A-Z][A-Z0-9_]*$/.test(key)) {
                return key;
            }
            return () => {
                made += 1;
                return {made};
            };
        },
    });
}
