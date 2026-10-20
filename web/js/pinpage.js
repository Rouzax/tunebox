// The page a locked site shows at every page address. Once the PIN is
// accepted the grant cookie is set, and the page that was asked for loads.
import {createApi} from './api.js';
import {bindPinScreen} from './pin.js';

const root = document.querySelector('.tunebox');
const screen = bindPinScreen(root, createApi((input, init) => window.fetch(input, init)));
screen.ask({title: 'Enter the PIN to listen', cancellable: false}).then(() => {
    // Opened as /pin.html itself, there is no other page to reload into.
    if (window.location.pathname === '/pin.html') {
        window.location.replace('/');
    } else {
        window.location.reload();
    }
});
