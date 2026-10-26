// The logo control of the station and group forms: a link field with an
// Upload button and a preview of an uploaded image.
import {button, el} from './dom.js';
import {logoPreview} from './state.js';

// The types the server accepts for an upload; the image itself is judged by
// its bytes there.
export const LOGO_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/svg+xml'];

// An uploaded logo is shown from the panel at the size of the page and of a
// list row; a URL is only downloaded after saving. kept() gives the form's
// unsaved change, which an upload that meets an ended session hands to the
// login so it can be offered again.
export function logoPart(ctx, input, kept) {
    const current = ctx.stillOpen;
    const preview = el('div', {class: 'logo-preview'});
    const picker = el('input', {type: 'file', name: 'logo-file', hidden: true,
        accept: LOGO_TYPES.join(',')});
    const note = el('span', {class: 'hint', role: 'alert'});
    function draw() {
        const shown = logoPreview(input.value.trim());
        if (shown.kind === 'blob') {
            preview.replaceChildren(
                el('img', {class: 'logo-full', src: shown.src, alt: 'Logo'}),
                el('img', {class: 'logo-small', src: shown.src, alt: 'Logo at list size'}));
        } else if (shown.kind === 'url') {
            preview.replaceChildren(el('span', {class: 'hint'}, 'Shown after saving.'));
        } else {
            preview.replaceChildren();
        }
    }
    input.addEventListener('input', draw);
    input.addEventListener('change', draw);
    picker.addEventListener('change', async () => {
        const file = picker.files[0];
        picker.value = '';
        if (!file) {
            return;
        }
        if (!LOGO_TYPES.includes(file.type)) {
            note.textContent = 'Upload a PNG, JPEG, WebP or SVG image.';
            return;
        }
        if (file.size > ctx.limits.logo_max) {
            note.textContent = `The image is larger than ${ctx.limits.logo_max / 1000000} MB.`;
            return;
        }
        note.textContent = '';
        let answer;
        try {
            answer = await ctx.upload(file, kept);
        } catch (error) {
            note.textContent = 'Could not reach tunebox.';
            return;
        }
        if (!current()) {
            return;
        }
        if (answer.status === 401) {
            return;
        }
        if (!answer.ok) {
            ctx.fail(answer.data);
            return;
        }
        input.value = answer.data.logo;
        draw();
    });
    draw();
    return {
        preview,
        controls: el('div', {class: 'actions'}, button('arrow_upward', 'Upload', () => picker.click(), {class: 'quiet'}), picker, note),
    };
}
