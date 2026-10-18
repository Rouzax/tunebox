import assert from 'node:assert/strict';
import {test} from 'node:test';
import qrcode from '../vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {qrMatrix} from '../../web_admin/js/qr.js';

test('qrMatrix gives a square of dark and light modules with finder patterns', () => {
    const matrix = qrMatrix(qrcode, 'otpauth://totp/tunebox%3Aadmin?secret=ABC');
    assert.ok(matrix.length >= 21);
    assert.ok(matrix.every((row) => row.length === matrix.length));
    // The top-left finder pattern: a dark ring around a light one.
    assert.equal(matrix[0][0], true);
    assert.equal(matrix[0][6], true);
    assert.equal(matrix[1][1], false);
    assert.equal(matrix[3][3], true);
});
