// The cover's two sides: the song's cover and, on its back, the artists'
// photo. Each song starts on its cover; a tap turns it only when there is
// a photo, and a photo that goes away turns it back.
export function createFlip() {
    let photo = null;
    let flipped = false;
    const state = () => ({photo, flipped, canFlip: photo !== null});
    return {
        song(next) {
            photo = next || null;
            flipped = false;
            return state();
        },
        photo(next) {
            photo = next || null;
            flipped = flipped && photo !== null;
            return state();
        },
        tap() {
            flipped = photo !== null && !flipped;
            return state();
        },
    };
}
