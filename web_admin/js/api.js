// The admin API: JSON in and out, the CSRF token on every POST.
let csrf = null;

export function setCsrf(token) {
    csrf = token;
}

// No admin call takes long on the server (writes wait at most 5 s, a rule
// check 2 s); one with no answer by now has lost its network, and the page
// treats it like one that failed to send.
const DEADLINE_MS = 20000;

// Sends a body as it is (a JSON string, or the bytes of an image) under its
// own content type, with the session's CSRF token.
export async function send(method, path, body, contentType, options) {
    const fetchFn = (options && options.fetchFn) || fetch;
    const deadlineMs = (options && options.deadlineMs) || DEADLINE_MS;
    const headers = {'Content-Type': contentType};
    if (csrf) {
        headers['X-Tunebox-CSRF'] = csrf;
    }
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), deadlineMs);
    try {
        const response = await fetchFn(`/admin/api/${path}`, {
            method,
            headers,
            body,
            credentials: 'same-origin',
            cache: 'no-store',
            signal: abort.signal,
        });
        let data = {};
        if (response.status !== 204) {
            // A proxy's error page is not JSON; it still counts as an answer.
            data = await response.json().catch(() => ({}));
        }
        return {ok: response.ok, status: response.status, data};
    } finally {
        clearTimeout(timer);
    }
}

export function call(method, path, body, options) {
    return send(method, path, body === undefined ? undefined : JSON.stringify(body), 'application/json', options);
}
