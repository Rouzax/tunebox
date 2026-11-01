import assert from 'node:assert/strict';
import {test} from 'node:test';
import qrcode from '../vendor/qrcode-generator-2.0.4/qrcode.mjs';
import {qrMatrix as panelMatrix} from '../../web_admin/js/qr.js';
import {qrMatrix} from '../js/qr.js';

test('the player builds the same code as the panel', () => {
    const text = '{"v":1,"r":[["wave","n",60,59]]}';
    assert.deepEqual(qrMatrix(qrcode, text), panelMatrix(qrcode, text));
});
