// What the browser offers for drawing: WebGL, the GPU it reports, workers'
// canvases, cores, memory and the screen. A classic script in old syntax,
// shared by check.html and the player's ?lab=1 mode, so it runs in any
// browser the car might have.
(function () {
    function readGl(kind) {
        var canvas = document.createElement('canvas');
        var gl = null;
        try {
            gl = canvas.getContext(kind) || (kind === 'webgl' ? canvas.getContext('experimental-webgl') : null);
        } catch (error) {
            gl = null;
        }
        if (!gl) {
            return null;
        }
        var info = {renderer: null, vendor: null, maxTexture: null};
        try {
            var debug = gl.getExtension('WEBGL_debug_renderer_info');
            info.renderer = String(gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
            info.vendor = String(gl.getParameter(debug ? debug.UNMASKED_VENDOR_WEBGL : gl.VENDOR));
            info.maxTexture = gl.getParameter(gl.MAX_TEXTURE_SIZE);
        } finally {
            // Browsers allow only a few live contexts per page; hand this
            // one back now rather than when the canvas is collected.
            var lose = gl.getExtension('WEBGL_lose_context');
            if (lose) {
                lose.loseContext();
            }
        }
        return info;
    }

    window.tuneboxEnvironment = function () {
        var gl1 = readGl('webgl');
        var gl2 = readGl('webgl2');
        var gl = gl2 || gl1 || {renderer: null, vendor: null, maxTexture: null};
        var screen = window.screen || {};
        return {
            webgl1: Boolean(gl1),
            webgl2: Boolean(gl2),
            renderer: gl.renderer,
            vendor: gl.vendor,
            maxTexture: gl.maxTexture,
            offscreen: typeof window.OffscreenCanvas === 'function',
            cores: navigator.hardwareConcurrency || null,
            // Only in secure contexts; a plain-http page has none.
            memory: navigator.deviceMemory || null,
            dpr: window.devicePixelRatio || 1,
            screen: {width: screen.width || 0, height: screen.height || 0},
        };
    };
}());
