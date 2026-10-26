import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';

import {sourceFromStream} from '../../web_admin/js/deduce.js';

test('every tested station gives its own source back', () => {
    // The example file is where the rules come from; every stream in it
    // must lead to the source written beside it.
    const text = readFileSync(new URL('../../examples/stations-nl.toml', import.meta.url), 'utf8');
    const blocks = text.split('[[channels]]').slice(1);
    let checked = 0;
    for (const block of blocks) {
        const source = /source = \{ type = "(\w+)", (\w+) = "([^"]+)" \}/.exec(block);
        for (const [, url] of block.matchAll(/^url = "([^"]+)"/gm)) {
            assert.deepEqual(sourceFromStream(url), {type: source[1], [source[2]]: source[3]}, url);
            checked += 1;
        }
    }
    assert.ok(checked >= 100, `only ${checked} streams checked`);
});

test('a Triton edge server address works too', () => {
    assert.deepEqual(sourceFromStream('https://29103.live.streamtheworld.com:443/OWR_INTERNATIONAL_ADP.aac'),
        {type: 'triton', mount: 'OWR_INTERNATIONAL'});
});

test('anything else is no guess', () => {
    for (const address of [
        'https://example.com/stream.mp3',
        'not a url',
        '',
        'https://playerservices.streamtheworld.com/api/livestream-redirect/',
        'https://playerservices.streamtheworld.com/api/livestream-redirect/AAC.aac',
        'https://audio-streaming.qmusic.nl/Qmusic_nl_unknown_high.aac',
        'https://icecast.omroep.nl/funx-bb-mp3',
        'https://playerservices.streamtheworld.com.evil.test/RADIO538.mp3',
    ]) {
        assert.equal(sourceFromStream(address), null, address);
    }
});
