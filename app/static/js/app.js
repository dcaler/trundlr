/**
 * Hash-based router. View scripts call registerView('/path', fn) where fn
 * receives the #app element and may:
 *   - manage #app.innerHTML directly (return null/undefined), or
 *   - return an HTML string or Promise<string> (router sets innerHTML).
 */
const views = {};

function registerView(path, renderFn) {
  views[path] = renderFn;
}

/** Escape a value for safe insertion into HTML. */
function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function statusBadge(status) {
  return `<span class="badge badge-${escHtml(status)}">${escHtml(status.replace('_', ' '))}</span>`;
}

function setActiveNav(hash) {
  document.querySelectorAll('.nav-links a').forEach(a => {
    a.classList.toggle('active', a.getAttribute('href') === `#${hash}`);
  });
}

async function navigate() {
  const hash = location.hash.slice(1) || '/projects';
  const view = views[hash] || views['/projects'];
  const el = document.getElementById('app');

  setActiveNav(hash);

  if (!view) {
    el.innerHTML = '<p class="error">Page not found.</p>';
    return;
  }

  el.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const html = await view(el);
    if (html != null) el.innerHTML = html;
  } catch (err) {
    el.innerHTML = `<p class="error">Error: ${escHtml(err.message)}</p>`;
  }
}

// Current app settings — populated before first render, updated by settings view.
// eslint-disable-next-line no-unused-vars
let appSettings = { timezone: 'UTC' };

window.addEventListener('hashchange', navigate);
window.addEventListener('DOMContentLoaded', async () => {
  try {
    const v = await api.get('/version');
    const el = document.getElementById('nav-version');
    if (el) el.textContent = v.version;
  } catch (_) {}
  try { appSettings = await api.get('/settings/'); } catch (_) { /* use defaults */ }
  navigate();
});
