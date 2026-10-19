// The admin API: JSON in and out, the CSRF token on every POST.
let csrf = null;

export function setCsrf(token) {
    csrf = token;
}

export async function call(method, path, body) {
    const headers = {'Content-Type': 'application/json'};
    if (csrf) {
        headers['X-Tunebox-CSRF'] = csrf;
    }
    const response = await fetch(`/admin/api/${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'same-origin',
        cache: 'no-store',
    });
    let data = {};
    if (response.status !== 204) {
        // A proxy's error page is not JSON; it still counts as an answer.
        data = await response.json().catch(() => ({}));
    }
    return {ok: response.ok, status: response.status, data};
}
