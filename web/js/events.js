// A tiny event emitter that ties the player modules together.
export function createEmitter() {
    const handlers = new Map();
    return {
        on(name, fn) {
            if (!handlers.has(name)) {
                handlers.set(name, new Set());
            }
            handlers.get(name).add(fn);
            return () => handlers.get(name).delete(fn);
        },
        emit(name, value) {
            for (const fn of handlers.get(name) || []) {
                try {
                    fn(value);
                } catch (error) {
                    // One broken listener must not silence the rest of the page.
                    console.error(error);
                }
            }
        },
    };
}
