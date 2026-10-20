// The admin API: JSON in and out, the CSRF token on every POST.
let csrf = null;

export function setCsrf(token) {
    csrf = token;
}

// No admin call takes long on the server (writes wait at most 5 s, a rule
// check 2 s); one with no answer by now has lost its network, and the page
// treats it like one that failed to send.
const DEADLINE_MS = 20000;

export async function call(method, path, body, options) {
    const fetchFn = (options && options.fetchFn) || fetch;
    const deadlineMs = (options && options.deadlineMs) || DEADLINE_MS;
    const headers = {'Content-Type': 'application/json'};
    if (csrf) {
        headers['X-Tunebox-CSRF'] = csrf;
    }
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), deadlineMs);
    try {
        const response = await fetchFn(`/admin/api/${path}`, {
            method,
            headers,
            body: body === undefined ? undefined : JSON.stringify(body),
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
