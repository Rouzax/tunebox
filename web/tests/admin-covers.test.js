import assert from 'node:assert/strict';
import {test} from 'node:test';
import {albumLink, listPath, releaseText, searchText, stateLine} from '../../web_admin/js/covers.js';

test('album links are checked as the server checks them', () => {
    assert.equal(albumLink(' https://www.deezer.com/en/album/655089251?utm_source=x '), 'https://www.deezer.com/en/album/655089251?utm_source=x');
    assert.equal(albumLink('https://deezer.com/album/1'), 'https://deezer.com/album/1');
    assert.equal(albumLink('https://deezer.page.link/AbC'), null);
    assert.equal(albumLink('https://www.deezer.com/album/1234567890123'), null);
    assert.equal(albumLink('http://www.deezer.com/album/1'), null);
    assert.equal(albumLink('https://www.deezer.com/track/1'), null);
});

test('list paths carry the view, the search and the cursor', () => {
    assert.equal(listPath('problems', '', null), 'covers?view=problems');
    assert.equal(listPath('all', 'a b', '17:a: b|c'), 'covers?view=all&q=a%20b&after=17%3Aa%3A%20b%7Cc');
});

test('each state reads as the owner says it', () => {
    assert.equal(stateLine({state: 'artist'}), 'Artist photo');
    assert.equal(stateLine({state: 'failed', reason: 'gone'}), 'Fetch failed: the image is gone');
    assert.equal(stateLine({state: 'failed', reason: 'odd'}), 'Fetch failed: odd');
    assert.equal(stateLine({state: 'lost'}), 'Image lost: upload it again');
    assert.equal(stateLine({state: 'fix', last_at: null, fix: {label: 'x'}}), 'Your fix · not heard in 30 days');
});

test('release marks and the prefilled search', () => {
    assert.equal(releaseText(true), 'release cover');
    assert.equal(releaseText(false), 'not a release cover');
    assert.equal(releaseText(null), 'not checked');
    assert.equal(searchText({artist: 'AGENTS OF TIME', title: 'Hearts Collide'}), 'AGENTS OF TIME Hearts Collide');
});
