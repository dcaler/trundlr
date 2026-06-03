// ── Settings view ─────────────────────────────────────────────────────────

const TIMEZONES = [
  'UTC',
  'Africa/Cairo', 'Africa/Johannesburg',
  'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/New_York',
  'America/Sao_Paulo', 'America/Toronto', 'America/Vancouver',
  'Asia/Dubai', 'Asia/Hong_Kong', 'Asia/Kolkata', 'Asia/Seoul',
  'Asia/Shanghai', 'Asia/Singapore', 'Asia/Tokyo',
  'Australia/Melbourne', 'Australia/Perth', 'Australia/Sydney',
  'Europe/Amsterdam', 'Europe/Berlin', 'Europe/Dublin', 'Europe/Helsinki',
  'Europe/Istanbul', 'Europe/London', 'Europe/Moscow', 'Europe/Paris',
  'Europe/Stockholm', 'Europe/Zurich',
  'Pacific/Auckland', 'Pacific/Honolulu',
];

registerView('/settings', async (el) => {
  const [settings, projects] = await Promise.all([
    api.get('/settings/'),
    api.get('/projects/'),
  ]);

  const caldavUrl = window.location.origin + '/caldav/';

  el.innerHTML = `
    <h1>Settings</h1>
    <form id="settings-form" style="display:flex;flex-direction:column;gap:1rem;max-width:360px;margin-top:1rem">
      <div>
        <label style="display:block;margin-bottom:0.25rem">Timezone</label>
        <select name="timezone" style="width:100%">
          ${TIMEZONES.map(tz =>
            `<option value="${escHtml(tz)}"${settings.timezone === tz ? ' selected' : ''}>${escHtml(tz)}</option>`
          ).join('')}
        </select>
        <p style="margin:0.4rem 0 0;font-size:0.85em;color:var(--text-muted)">
          Used for iCal feeds. Dates are stored and entered in this timezone.
        </p>
      </div>
      <div>
        <label style="display:block;margin-bottom:0.25rem">CalDAV default project</label>
        <select name="caldav_default_project_id" style="width:100%">
          <option value="">— none —</option>
          ${projects.map(p =>
            `<option value="${p.id}"${settings.caldav_default_project_id === p.id ? ' selected' : ''}>${escHtml(p.name)}</option>`
          ).join('')}
        </select>
        <p style="margin:0.4rem 0 0;font-size:0.85em;color:var(--text-muted)">
          New events created via CalDAV are added to this project.
        </p>
      </div>
      <div>
        <label style="display:block;margin-bottom:0.25rem">CalDAV URL</label>
        <input type="text" readonly value="${escHtml(caldavUrl)}"
          style="width:100%;background:var(--bg-subtle,#f5f5f5);border:1px solid var(--border);padding:0.35rem 0.5rem;border-radius:4px;font-family:monospace;font-size:0.9em"
          onclick="this.select()"
        />
        <p style="margin:0.4rem 0 0;font-size:0.85em;color:var(--text-muted)">
          Use this base URL in Apple Calendar or Thunderbird to subscribe.
        </p>
      </div>
      <div>
        <button type="submit" class="btn btn-primary">Save</button>
        <span id="settings-status" style="margin-left:0.75rem;font-size:0.9em;color:var(--text-muted)"></span>
      </div>
    </form>
  `;

  el.querySelector('#settings-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const statusEl = el.querySelector('#settings-status');
    try {
      const updated = await api.patch('/settings/', {
        timezone: fd.get('timezone'),
        caldav_default_project_id: parseInt(fd.get('caldav_default_project_id')) || null,
      });
      appSettings = updated;
      statusEl.textContent = 'Saved.';
      setTimeout(() => { statusEl.textContent = ''; }, 2000);
    } catch (err) {
      alert(`Error: ${err.message}`);
    }
  });
});
