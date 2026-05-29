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
  const settings = await api.get('/settings/');

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
      const updated = await api.patch('/settings/', { timezone: fd.get('timezone') });
      appSettings = updated;
      statusEl.textContent = 'Saved.';
      setTimeout(() => { statusEl.textContent = ''; }, 2000);
    } catch (err) {
      alert(`Error: ${err.message}`);
    }
  });
});
