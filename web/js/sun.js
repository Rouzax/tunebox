export const DAYNIGHT_MODES = ['auto', 'day', 'night'];

// Sunrise and sunset by SunCalc's formulas (sun at -0.833 degrees, which
// accounts for refraction and the solar disc).
export function sunTimes(date, lat, lon) {
    const rad = Math.PI / 180;
    const dayMs = 86400000;
    const J1970 = 2440588;
    const J2000 = 2451545;
    const lw = -lon * rad;
    const phi = lat * rad;
    const d = date.valueOf() / dayMs - 0.5 + J1970 - J2000;
    const n = Math.round(d - 0.0009 - lw / (2 * Math.PI));
    const ds = 0.0009 + lw / (2 * Math.PI) + n;
    const M = rad * (357.5291 + 0.98560028 * ds);
    const C = rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
    const L = M + C + rad * 102.9372 + Math.PI;
    const dec = Math.asin(Math.sin(rad * 23.4397) * Math.sin(L));
    const transit = (j) => J2000 + j + 0.0053 * Math.sin(M) - 0.0069 * Math.sin(2 * L);
    const noon = transit(ds);
    const w = Math.acos((Math.sin(-0.833 * rad) - Math.sin(phi) * Math.sin(dec)) / (Math.cos(phi) * Math.cos(dec)));
    const set = transit(0.0009 + (w + lw) / (2 * Math.PI) + n);
    const fromJulian = (j) => new Date((j + 0.5 - J1970) * dayMs);
    return {rise: fromJulian(noon - (set - noon)), set: fromJulian(set)};
}

// Automatic follows the sun where the deployment configured a location, and
// the browser's dark mode setting otherwise.
export function isNight(mode, now, location, prefersDark) {
    if (mode === 'night') {
        return true;
    }
    if (mode === 'day') {
        return false;
    }
    if (!location) {
        return prefersDark;
    }
    const {rise, set} = sunTimes(now, location.latitude, location.longitude);
    return now < rise || now > set;
}
