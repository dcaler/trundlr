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
  const raw = location.hash.slice(1) || '/projects';
  const el = document.getElementById('app');

  // Split off any ?query=string  e.g. /tasks?resource=3
  const qIdx  = raw.indexOf('?');
  const path  = qIdx === -1 ? raw : raw.slice(0, qIdx);
  const query = Object.fromEntries(new URLSearchParams(qIdx === -1 ? '' : raw.slice(qIdx + 1)));

  let view   = views[path];
  let params = { ...query };

  // Parameterized route: /base/id  e.g. /projects/42
  if (!view) {
    const cut = path.indexOf('/', 1);
    if (cut !== -1) {
      const base = path.slice(0, cut);
      const id   = parseInt(path.slice(cut + 1));
      if (!isNaN(id) && views[base]) {
        view   = views[base];
        params = { ...query, id };
      }
    }
  }

  // Highlight the base nav item when on a deep-link sub-path
  setActiveNav(params.id ? path.slice(0, path.lastIndexOf('/')) : path);

  if (!view) view = views['/projects'];
  if (!view) { el.innerHTML = '<p class="error">Page not found.</p>'; return; }

  el.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const html = await view(el, params);
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
  // Hamburger menu toggle for mobile
  const navToggle = document.getElementById('nav-toggle');
  const navLinks  = document.querySelector('.nav-links');
  if (navToggle && navLinks) {
    navToggle.addEventListener('click', () => {
      const open = navLinks.classList.toggle('open');
      navToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      navToggle.textContent = open ? '✕' : '☰';
    });
    // Close the menu whenever a nav link is tapped
    navLinks.addEventListener('click', e => {
      if (e.target.tagName === 'A') {
        navLinks.classList.remove('open');
        navToggle.setAttribute('aria-expanded', 'false');
        navToggle.textContent = '☰';
      }
    });
  }

  // When a nav link is already the active hash, hashchange won't fire (the
  // URL doesn't change). Intercept clicks so we always re-render in that case
  // — e.g. navigating back to the Projects list from a project detail view.
  document.querySelectorAll('.nav-links a').forEach(a => {
    a.addEventListener('click', e => {
      if (location.hash === a.getAttribute('href')) {
        e.preventDefault();
        navigate();
      }
    });
  });
  try {
    const v = await api.get('/version');
    const el = document.getElementById('nav-version');
    if (el) el.textContent = v.version;
  } catch (_) {}
  try { appSettings = await api.get('/settings/'); } catch (_) { /* use defaults */ }
  navigate();
});
