(function () {
    var table = document.getElementById('report');

    function row(name, value) {
        var tr = document.createElement('tr');
        var th = document.createElement('th');
        var td = document.createElement('td');
        th.textContent = name;
        td.textContent = String(value);
        tr.appendChild(th);
        tr.appendChild(td);
        table.appendChild(tr);
        return td;
    }

    // Set by check-async.js and check-optional.js, which only parse in
    // browsers that know the syntax (the page allows no eval).
    var syntax = window.tuneboxSyntax || {};

    function css(property, value) {
        return window.CSS && CSS.supports ? (CSS.supports(property, value) ? 'yes' : 'no') : 'unknown';
    }

    var audio = document.createElement('audio');
    row('User agent', navigator.userAgent);
    row('Window (CSS px)', window.innerWidth + ' x ' + window.innerHeight + ' @' + (window.devicePixelRatio || 1) + 'x');
    var probe = document.createElement('div');
    probe.className = 'vh-probe';
    document.body.appendChild(probe);
    row('100vh vs window height', probe.offsetHeight + ' vs ' + window.innerHeight);
    document.body.removeChild(probe);
    var scheme = row('Dark mode', '?');
    if (window.matchMedia) {
        var query = window.matchMedia('(prefers-color-scheme: dark)');
        var show = function () { scheme.textContent = (query.matches ? 'dark' : 'light') + ' at ' + new Date().toLocaleTimeString(); };
        show();
        if (query.addEventListener) {
            query.addEventListener('change', show);
        }
    }
    row('ES modules', 'noModule' in document.createElement('script') ? 'yes' : 'no');
    row('async/await', syntax.asyncAwait ? 'yes' : 'no');
    row('Optional chaining (?.)', syntax.optionalChaining ? 'yes' : 'no');
    row('replaceChildren()', typeof document.body.replaceChildren === 'function' ? 'yes' : 'no');
    row('AbortController', typeof AbortController === 'function' ? 'yes' : 'no');
    row('ResizeObserver', typeof ResizeObserver === 'function' ? 'yes' : 'no');
    row('CSS @property', window.CSS && CSS.registerProperty ? 'yes' : 'no');
    row('CSS color-mix()', css('color', 'color-mix(in srgb, red 50%, blue)'));
    row('CSS clamp()', css('width', 'clamp(1px, 2px, 3px)'));
    row('CSS mask-image', css('mask-image', 'none') === 'yes' || css('-webkit-mask-image', 'none') === 'yes' ? 'yes' : 'no');
    row('Fullscreen', document.fullscreenEnabled ? 'yes' : (document.webkitFullscreenEnabled ? 'webkit' : 'no'));
    row('Web Audio', window.AudioContext ? 'yes' : (window.webkitAudioContext ? 'webkit' : 'no'));
    row('Media Session', 'mediaSession' in navigator ? 'yes' : 'no');
    row('Plays AAC (audio/aac)', audio.canPlayType('audio/aac') || 'no');
    row('Plays HE-AAC (mp4a.40.5)', audio.canPlayType('audio/mp4; codecs="mp4a.40.5"') || 'no');
    row('Plays MP3', audio.canPlayType('audio/mpeg') || 'no');
    var storage = 'no';
    try {
        localStorage.setItem('tunebox.check', '1');
        localStorage.removeItem('tunebox.check');
        storage = 'yes';
    } catch (error) {
        storage = 'blocked';
    }
    row('localStorage', storage);
}());
