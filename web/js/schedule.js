import {trackKey} from './tracks.js';

// Two answers whose switch times differ by less than this describe the same change.
export const SAME_EVENT_MS = 3000;

// Shows the current track from each /now answer and switches to the
// upcoming one when it becomes audible. A poll answered just before the
// switch can arrive just after it and still name the old track; it is
// recognised by its matching upcoming entry and does not flip back.
export function createSwitcher({show, now, setTimer, clearTimer}) {
    let timer = null;
    let switched = null;

    function cancel() {
        if (timer !== null) {
            clearTimer(timer);
            timer = null;
        }
    }

    return {
        update(body) {
            const at = now();
            const upcoming = body.upcoming
                ? {key: trackKey(body.upcoming.track), track: body.upcoming.track, deadline: at + body.upcoming.in_s * 1000}
                : null;
            if (switched && upcoming && switched.key === upcoming.key
                && Math.abs(switched.deadline - upcoming.deadline) < SAME_EVENT_MS) {
                return;
            }
            cancel();
            switched = null;
            show(body.current || null);
            if (upcoming) {
                timer = setTimer(() => {
                    timer = null;
                    switched = upcoming;
                    show(upcoming.track);
                }, Math.max(0, upcoming.deadline - at));
            }
        },
        reset() {
            cancel();
            switched = null;
        },
    };
}
