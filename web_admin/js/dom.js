// Elements from data only: text goes in as text, never as markup.
export function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
        if (key === 'class') {
            node.className = value;
        } else if (key.startsWith('on')) {
            node.addEventListener(key.slice(2), value);
        } else if (key === 'value' || key === 'checked') {
            // Properties, not attributes: they are what a form reads back.
            node[key] = value;
        } else if (value === true) {
            node.setAttribute(key, '');
        } else if (value !== false && value !== null && value !== undefined) {
            node.setAttribute(key, String(value));
        }
    }
    for (const child of children.flat()) {
        if (child !== null && child !== undefined && child !== false) {
            node.append(child instanceof Node ? child : document.createTextNode(String(child)));
        }
    }
    return node;
}

const SPRITE = '/vendor/material-symbols-rounded-737e332/icons.svg';

export function icon(name) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', `${SPRITE}#${name}`);
    svg.append(use);
    return svg;
}

// A button with an icon; a label of null makes an icon-only button, named
// for screen readers by title.
export function button(iconName, label, onclick, extra) {
    const attrs = Object.assign({type: 'button', onclick}, extra || {});
    if (label === null) {
        attrs['aria-label'] = attrs.title;
        attrs.class = `${attrs.class || ''} plain`.trim();
    }
    return el('button', attrs, icon(iconName), label);
}

export function field(label, input, hint) {
    return el('label', null, label, input, hint ? el('span', {class: 'hint'}, hint) : null);
}

export function when(seconds) {
    return new Date(seconds * 1000).toLocaleString();
}
