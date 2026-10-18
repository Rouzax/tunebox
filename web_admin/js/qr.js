// The setup page's QR code. The library is passed in, so this module loads
// the same way in the browser and in node --test.
export function qrMatrix(qrcode, text) {
    const code = qrcode(0, 'M');
    code.addData(text);
    code.make();
    const size = code.getModuleCount();
    const rows = [];
    for (let row = 0; row < size; row += 1) {
        const cells = [];
        for (let col = 0; col < size; col += 1) {
            cells.push(code.isDark(row, col));
        }
        rows.push(cells);
    }
    return rows;
}

// Built with createElementNS, never markup, so nothing in the text can
// become an element.
export function qrSvg(doc, matrix) {
    const ns = 'http://www.w3.org/2000/svg';
    const margin = 4;
    const size = matrix.length + margin * 2;
    const svg = doc.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', `0 0 ${size} ${size}`);
    svg.setAttribute('class', 'qr');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'QR code for your authenticator app');
    const background = doc.createElementNS(ns, 'rect');
    background.setAttribute('width', String(size));
    background.setAttribute('height', String(size));
    background.setAttribute('fill', '#fff');
    svg.append(background);
    matrix.forEach((cells, row) => {
        cells.forEach((dark, col) => {
            if (dark) {
                const cell = doc.createElementNS(ns, 'rect');
                cell.setAttribute('x', String(col + margin));
                cell.setAttribute('y', String(row + margin));
                cell.setAttribute('width', '1');
                cell.setAttribute('height', '1');
                svg.append(cell);
            }
        });
    });
    return svg;
}
