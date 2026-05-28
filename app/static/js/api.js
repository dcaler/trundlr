/**
 * Thin fetch wrapper. All methods return a Promise that resolves to parsed
 * JSON or null (for 204), and reject with an Error whose .status is the HTTP
 * status code on any non-2xx response.
 */
const API_BASE = '/api';

async function apiFetch(path, options = {}) {
  const opts = { ...options };
  if (opts.body !== undefined) {
    opts.body = JSON.stringify(opts.body);
    opts.headers = { 'Content-Type': 'application/json', ...opts.headers };
  }
  const resp = await fetch(`${API_BASE}${path}`, opts);
  if (!resp.ok) {
    let msg = resp.statusText;
    try { msg = (await resp.json()).detail || msg; } catch (_) { /* ignore */ }
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return resp.status === 204 ? null : resp.json();
}

// eslint-disable-next-line no-unused-vars
const api = {
  get:    (path)       => apiFetch(path),
  post:   (path, body) => apiFetch(path, { method: 'POST',   body }),
  patch:  (path, body) => apiFetch(path, { method: 'PATCH',  body }),
  delete: (path)       => apiFetch(path, { method: 'DELETE' }),
};
