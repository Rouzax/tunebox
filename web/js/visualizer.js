import AudioMotionAnalyzer from '../vendor/audiomotion-analyzer-4.5.4/audioMotion-analyzer.js';
import {createEffects} from './effects.js';
import {createMilkdrop, webgl2Available} from './milkdrop.js';
import {keepAudioRunning} from './reconnect.js';
import {accentColor} from './palette.js';
import {
    ACCENT_GRADIENT, ART_GRADIENT, BACKGROUND, BASE, LOW_POWER, LOW_POWER_FPS, RESET, availableStyles, chooseStyle,
    groupOf, groupsFor,
} from './vizcore.js';

const LOW_POWER_CHECK_MS = 3000;

// The spectrum styles come from audioMotion, the effects from effects.js,
// MilkDrop from milkdrop.js.
// Web Audio starts only after the listener's first tap or key press: an
// AudioContext made earlier stays suspended, and a stream routed through a
// suspended context plays silence. Until then, and with Off, the stream
// plays straight from the audio element.
export function bindVisualizer(app) {
    const {root, events, storage, player, toast} = app;
    const toggle = root.querySelector('.viz-toggle');
    const groupsBox = root.querySelector('.viz-groups');
    const toTiles = root.querySelector('.btn-viz-tiles');
    const offButton = root.querySelector('.viz-off');
    const analyzer = root.querySelector('#analyzer');
    const artBox = root.querySelector('.artwork-img');
    // MilkDrop needs WebGL 2; without it its styles are not offered.
    const gl2 = webgl2Available();
    const STYLES = availableStyles(gl2);
    const chosen = chooseStyle(storage.get('visualizer'), gl2);
    let index = STYLES.map((style) => style.id).indexOf(chosen);
    let motion = null;
    let effects = null;
    let milkdrop = null;
    // A style shown for this page view in place of the chosen one, which
    // could not start; the stored choice stays, so a later visit tries it.
    let standIn = null;
    let artStops = null;
    let lowPower = false;
    let gestured = false;
    let available = true;
    let resumeAudio = () => {};
    // Lab mode (?lab=1): builds the test signal's node in the analyzer's
    // context; set, the analyzer reads it instead of the stream.
    let labBuild = null;
    let powerHeld = false;
    // Frames audioMotion has drawn, for the lab's draw rate.
    let draws = 0;
    // Set by lab mode: told of a style picked from the menu, and whether
    // the menu may change the style at all (not during a test).
    let onMenuPick = null;
    let menuLocked = false;

    function ensureMotion() {
        if (motion || !gestured || !available || !(labBuild || player.element())) {
            return motion;
        }
        motion = new AudioMotionAnalyzer(analyzer, {...BASE, ...RESET, source: labBuild ? undefined : player.element()});
        if (labBuild) {
            connectLab();
        }
        motion.registerGradient(ACCENT_GRADIENT, {bgColor: BACKGROUND, colorStops: [accentColor(root)]});
        if (artStops) {
            motion.registerGradient(ART_GRADIENT, {bgColor: BACKGROUND, colorStops: artStops});
        }
        motion.setOptions({onCanvasDraw: (instance) => {
            draws += 1;
            app.setBass(instance.getEnergy('bass'));
        }});
        resumeAudio = keepAudioRunning(motion.audioCtx, () => player.state() !== 'stopped');
        effects = createEffects(motion, root, app.setBass);
        milkdrop = createMilkdrop({motion, root, onLevel: app.setBass, onFail: milkdropFailed, held: () => powerHeld});
        checkPower();
        return motion;
    }

    // The lab's signal is for the analysers only: the speakers stay silent.
    function connectLab() {
        motion.volume = 0;
        motion.connectInput(labBuild(motion.audioCtx));
    }

    // Moves the canvas to a square around the cover and sizes the ring's
    // hole to the cover; audioMotion's inner radius is 0.375 x radius x the
    // canvas's shorter side. The cover is measured against its own page,
    // because the pages slide sideways (translate) while the menu is open.
    function placeRing() {
        if (!motion) {
            return;
        }
        const host = analyzer.offsetParent;
        const page = root.querySelector('.page.main');
        const container = root.querySelector('.main-container > .container');
        if (!host || !artBox || !page || !container) {
            return;
        }
        const hostRect = host.getBoundingClientRect();
        const art = artBox.getBoundingClientRect();
        const pageRect = page.getBoundingClientRect();
        const resting = container.getBoundingClientRect();
        const side = Math.min(art.width * 2.3, hostRect.width);
        const centerX = art.left - pageRect.left + resting.left + art.width / 2 - hostRect.left;
        const centerY = art.top - pageRect.top + resting.top + art.height / 2 - hostRect.top;
        Object.assign(analyzer.style, {
            top: `${centerY - side / 2}px`, left: `${centerX - side / 2}px`,
            width: `${side}px`, height: `${side}px`, right: 'auto', bottom: 'auto',
        });
        motion.setOptions({radius: (art.width / 2) / (0.375 * side)});
    }

    function clearRing() {
        for (const prop of ['top', 'left', 'width', 'height', 'right', 'bottom']) {
            analyzer.style.removeProperty(prop);
        }
    }

    function styleIndex(id) {
        return STYLES.map((style) => style.id).indexOf(id);
    }

    function milkdropFailed(why) {
        if (why === 'butterchurn') {
            toast.show('MilkDrop could not start here.');
            standIn = styleIndex('wave');
        } else {
            toast.show('That MilkDrop preset could not load; shuffling instead.');
            standIn = styleIndex('md-shuffle');
        }
        apply();
    }

    function apply() {
        const style = STYLES[standIn === null ? index : standIn];
        root.dataset.viz = style.id;
        // Effects and MilkDrop draw on their own canvas; the spectrum
        // canvas would show its last frame.
        root.toggleAttribute('data-effect', style.engine === 'effects' || style.engine === 'milkdrop');
        markActive();
        if (style.engine === 'off') {
            if (motion) {
                motion.toggleAnalyzer(false);
                effects.stop();
                milkdrop.stop();
            }
            clearRing();
            app.setBass(0, true);
            return;
        }
        if (!ensureMotion()) {
            // Applied again once Web Audio can start.
            return;
        }
        if (style.engine === 'effects') {
            motion.toggleAnalyzer(false);
            milkdrop.stop();
            clearRing();
            effects.start(style.effect);
            return;
        }
        if (style.engine === 'milkdrop') {
            motion.toggleAnalyzer(false);
            effects.stop();
            clearRing();
            milkdrop.start(style, lowPower);
            return;
        }
        effects.stop();
        milkdrop.stop();
        let gradient = ACCENT_GRADIENT;
        if (style.gradient) {
            gradient = style.gradient;
        } else if (style.art && artStops) {
            gradient = ART_GRADIENT;
        }
        motion.setOptions({...RESET, ...style.options, ...(lowPower ? LOW_POWER : {}), gradient});
        if (style.ring) {
            placeRing();
        } else {
            clearRing();
        }
        motion.toggleAnalyzer(true);
    }

    // Two slow samples in a row while the analyzer runs switch it to low
    // power for the rest of the visit; three fast ones end the check.
    function checkPower() {
        let slow = 0;
        let fast = 0;
        const check = window.setInterval(() => {
            if (powerHeld) {
                window.clearInterval(check);
                return;
            }
            if (!motion.isOn || !motion.fps) {
                return;
            }
            if (motion.fps < LOW_POWER_FPS) {
                fast = 0;
                slow += 1;
                if (slow >= 2) {
                    window.clearInterval(check);
                    lowPower = true;
                    root.dataset.power = 'low';
                    apply();
                }
            } else {
                slow = 0;
                fast += 1;
                if (fast >= 3) {
                    window.clearInterval(check);
                }
            }
        }, LOW_POWER_CHECK_MS);
    }

    // The page shows the group tiles, or one group's styles. It opens on
    // the group of the style showing, as the channel page opens on the
    // playing station.
    const groups = groupsFor(gl2);
    let openGroup = null;
    // The rendered style buttons: their list item and style index.
    let items = [];

    function element(tag, className, text) {
        const el = document.createElement(tag);
        if (className) {
            el.className = className;
        }
        if (text !== undefined) {
            el.textContent = text;
        }
        return el;
    }

    function pick(i) {
        if (!menuLocked) {
            index = i;
            standIn = null;
            storage.set('visualizer', STYLES[i].id);
            apply();
            if (onMenuPick) {
                onMenuPick();
            }
        }
        app.showPage('main');
    }

    function renderViz() {
        items = [];
        groupsBox.textContent = '';
        const group = groups.filter((candidate) => candidate.id === openGroup)[0];
        if (!group) {
            openGroup = null;
            const grid = element('div', 'viz-tiles');
            for (const each of groups) {
                const tile = element('button', 'tile');
                tile.type = 'button';
                tile.dataset.group = each.id;
                const art = element('span', 'tile-art');
                const image = element('img');
                image.alt = '';
                image.src = each.image;
                art.append(image);
                tile.append(art, element('span', 'name', each.label));
                grid.append(tile);
            }
            groupsBox.append(grid);
        } else {
            const list = element('ul', 'viz-list');
            STYLES.forEach((style, i) => {
                if (style.group !== openGroup) {
                    return;
                }
                const item = element('li');
                const button = element('button', '', style.label);
                button.type = 'button';
                button.addEventListener('click', () => pick(i));
                item.append(button);
                list.append(item);
                items.push({item, i});
            });
            groupsBox.append(element('h4', '', group.label), list);
        }
        toTiles.classList.toggle('hidden', openGroup === null);
        offButton.classList.toggle('hidden', openGroup !== null);
        markActive();
    }

    // Opens a group's styles, or the tiles again with null; back on the
    // tiles, the tile just left takes the focus when the page had it.
    function showGroup(id) {
        const left = openGroup;
        const hadFocus = root.contains(document.activeElement) && document.activeElement !== document.body;
        openGroup = id;
        renderViz();
        groupsBox.closest('.page').scrollTop = 0;
        if (!hadFocus) {
            return;
        }
        const target = openGroup !== null
            ? toTiles
            : [...groupsBox.querySelectorAll('.tile')].filter((tile) => tile.dataset.group === left)[0];
        if (target) {
            target.focus({preventScroll: true});
        }
    }

    function markActive() {
        items.forEach(({item, i}) => item.classList.toggle('active', i === index));
        offButton.classList.toggle('active', STYLES[index].engine === 'off');
    }

    groupsBox.addEventListener('click', (event) => {
        const tile = event.target.closest('.tile[data-group]');
        if (tile) {
            showGroup(tile.dataset.group);
        }
    });
    toTiles.addEventListener('click', () => showGroup(null));
    offButton.addEventListener('click', () => pick(styleIndex('off')));
    events.on('page', (page) => {
        if (page === 'viz') {
            openGroup = groupOf(STYLES[index].id);
            renderViz();
        }
    });
    // Escape on the page: from a group to the tiles (true), else nothing.
    app.vizBack = () => {
        if (openGroup === null) {
            return false;
        }
        showGroup(null);
        return true;
    };
    renderViz();

    toggle.classList.remove('hidden');
    toggle.addEventListener('click', () => app.showPage(app.page() === 'viz' ? 'main' : 'viz'));

    const onGesture = () => {
        if (gestured) {
            return;
        }
        gestured = true;
        if (player.state() !== 'stopped') {
            apply();
        }
    };
    for (const type of ['pointerdown', 'keydown', 'touchstart']) {
        document.addEventListener(type, onGesture, {capture: true, passive: true});
    }

    events.on('state', (state) => {
        if (state === 'loading' || state === 'playing') {
            if (motion) {
                resumeAudio();
            } else {
                apply();
            }
        }
    });
    events.on('audio', ({element, cors}) => {
        available = cors;
        if (!motion) {
            return;
        }
        motion.disconnectInput();
        if (cors) {
            motion.connectInput(element);
            effects.reconnect();
            milkdrop.reconnect();
            apply();
        } else {
            motion.toggleAnalyzer(false);
            effects.stop();
            milkdrop.stop();
            toast.show('No visualizer for this stream.');
        }
    });
    events.on('palette', (palette) => {
        artStops = palette ? palette.map(([h, s]) => `hsl(${h}, ${s}%, 60%)`) : null;
        if (motion && artStops) {
            motion.registerGradient(ART_GRADIENT, {bgColor: BACKGROUND, colorStops: artStops});
        }
        apply();
    });
    if (window.ResizeObserver && artBox) {
        new ResizeObserver(() => {
            if (STYLES[index].ring) {
                placeRing();
            }
        }).observe(root);
    }
    apply();

    // For lab mode only: the gallery's view of the styles. Nothing here is
    // stored, so the normal player keeps the listener's own choice.
    return {
        styles: () => STYLES.map(({id, label, engine}) => ({id, label, engine})),
        index: () => index,
        select(i) {
            index = i;
            standIn = null;
            apply();
        },
        lowPower: () => lowPower,
        setLowPower(on) {
            lowPower = on;
            if (on) {
                root.dataset.power = 'low';
            } else {
                delete root.dataset.power;
            }
            apply();
        },
        // Keeps the automatic switch to low power from firing mid-test.
        holdPower() {
            powerHeld = true;
        },
        // build(audioCtx) returns the AudioNode to analyse in place of the
        // stream. Call from a user gesture: it may create the AudioContext.
        connectLab(build) {
            gestured = true;
            labBuild = build;
            // Directly: apply() makes none while the style is Off. Lab mode
            // plays no stream, so nothing has made one before.
            ensureMotion();
            apply();
            if (motion && motion.audioCtx.state === 'suspended') {
                motion.audioCtx.resume().catch(() => {});
            }
            return Boolean(motion);
        },
        // Frames audioMotion has drawn so far, or null while the style is
        // one it does not draw (Off and the effects.js styles).
        draws: () => (STYLES[index].engine === 'motion' ? draws : null),
        onMenuPick(fn) {
            onMenuPick = fn;
        },
        lockMenu(on) {
            menuLocked = on;
        },
    };
}
