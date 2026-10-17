// Per-browser settings. Storage can be missing or throw (private windows,
// blocked site data); the page then works with defaults.
export const PREFIX = 'tunebox.';

export function createStorage(backend) {
    const storage = {
        get(key) {
            try {
                return backend ? backend.getItem(PREFIX + key) : null;
            } catch (error) {
                return null;
            }
        },
        set(key, value) {
            try {
                if (backend) {
                    backend.setItem(PREFIX + key, String(value));
                }
            } catch (error) {
                // The choice lasts for this page view.
            }
        },
        number(key, fallback, min, max) {
            const raw = storage.get(key);
            const value = raw === null || raw === '' ? Number.NaN : Number(raw);
            return Number.isFinite(value) && value >= min && value <= max ? value : fallback;
        },
        choice(key, allowed, fallback) {
            const raw = storage.get(key);
            return allowed.indexOf(raw) >= 0 ? raw : fallback;
        },
        flag(key) {
            return storage.get(key) === '1';
        },
    };
    return storage;
}
