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
  const [settings, projects, resources] = await Promise.all([
    api.get('/settings/'),
    api.get('/projects/'),
    api.get('/resources/'),
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

    <hr style="margin:2rem 0;border:none;border-top:1px solid var(--border)">

    <h2>Email notifications</h2>
    <p style="color:var(--text-muted);max-width:480px;margin-bottom:1rem;font-size:0.9em">
      Sends a plain-text email when a task completes, fails, or a dependency is deleted.
      Leave SMTP host blank to disable.
    </p>
    <form id="email-form" style="display:flex;flex-direction:column;gap:0.75rem;max-width:360px">
      <div>
        <label style="display:block;margin-bottom:0.25rem">Recipient email</label>
        <input name="notify_email" type="email" value="${escHtml(settings.notify_email || '')}" placeholder="you@example.com" style="width:100%">
      </div>
      <div>
        <label style="display:block;margin-bottom:0.25rem">SMTP host</label>
        <input name="smtp_host" value="${escHtml(settings.smtp_host || '')}" placeholder="smtp.gmail.com" style="width:100%">
      </div>
      <div style="display:flex;gap:0.5rem">
        <div style="flex:1">
          <label style="display:block;margin-bottom:0.25rem">Port</label>
          <input name="smtp_port" type="number" value="${settings.smtp_port || 587}" style="width:100%">
        </div>
        <div style="flex:1;display:flex;align-items:flex-end;padding-bottom:0.35rem">
          <label style="display:flex;align-items:center;gap:0.4rem;font-size:0.9em">
            <input type="checkbox" name="smtp_tls" ${settings.smtp_tls !== false ? 'checked' : ''}>
            STARTTLS
          </label>
        </div>
      </div>
      <div>
        <label style="display:block;margin-bottom:0.25rem">SMTP username</label>
        <input name="smtp_user" value="${escHtml(settings.smtp_user || '')}" placeholder="user@gmail.com" style="width:100%">
      </div>
      <div>
        <label style="display:block;margin-bottom:0.25rem">
          SMTP password
          ${settings.smtp_password_set ? '<span style="font-size:0.8em;color:var(--text-muted)">(set — leave blank to keep)</span>' : ''}
        </label>
        <input name="smtp_password" type="password" placeholder="${settings.smtp_password_set ? '••••••••' : ''}" style="width:100%" autocomplete="new-password">
      </div>
      <div>
        <label style="display:block;margin-bottom:0.25rem">From address <span style="font-size:0.8em;color:var(--text-muted)">(optional)</span></label>
        <input name="smtp_from" type="email" value="${escHtml(settings.smtp_from || '')}" placeholder="trundlr@yourdomain.com" style="width:100%">
      </div>
      <div style="display:flex;gap:0.5rem;align-items:center">
        <button type="submit" class="btn btn-primary">Save email settings</button>
        <button type="button" id="btn-test-email" class="btn btn-ghost">Send test</button>
        <span id="email-status" style="font-size:0.9em;color:var(--text-muted)"></span>
      </div>
    </form>

    <hr style="margin:2rem 0;border:none;border-top:1px solid var(--border)">

    <h1>Task cycles</h1>
    <p style="color:var(--text-muted);max-width:640px">
      A cycle is a reusable bundle of tasks (e.g. a "Lit Review": Init → Gather → Collect → Draft → Review).
      Add a cycle to any project from its page; each step becomes a task chained to the previous one.
      Durations and resources defined here are identical across every instance.
    </p>
    <div id="cycles-section" style="margin-top:1rem"></div>
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

  el.querySelector('#email-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const statusEl = el.querySelector('#email-status');
    const patch = {
      notify_email: fd.get('notify_email') || null,
      smtp_host:    fd.get('smtp_host') || null,
      smtp_port:    parseInt(fd.get('smtp_port')) || 587,
      smtp_user:    fd.get('smtp_user') || null,
      smtp_from:    fd.get('smtp_from') || null,
      smtp_tls:     fd.has('smtp_tls'),
    };
    const pw = fd.get('smtp_password');
    if (pw) patch.smtp_password = pw;
    try {
      await api.patch('/settings/', patch);
      statusEl.textContent = 'Saved.';
      setTimeout(() => { statusEl.textContent = ''; }, 2000);
    } catch (err) {
      alert(`Error: ${err.message}`);
    }
  });

  el.querySelector('#btn-test-email').addEventListener('click', async () => {
    const btn = el.querySelector('#btn-test-email');
    const statusEl = el.querySelector('#email-status');
    btn.disabled = true;
    statusEl.textContent = 'Sending…';
    try {
      await api.post('/settings/test-email', {});
      statusEl.style.color = 'var(--success, green)';
      statusEl.textContent = 'Sent! Check your inbox.';
    } catch (err) {
      statusEl.style.color = 'var(--danger, red)';
      statusEl.textContent = err.message;
    } finally {
      btn.disabled = false;
      setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = ''; }, 5000);
    }
  });

  await renderCyclesSection(el.querySelector('#cycles-section'), resources);
});

// ── Cycle templates editor ─────────────────────────────────────────────────

function stepResourceChecks(resources, selectedIds = []) {
  if (resources.length === 0)
    return '<span style="color:var(--text-muted);font-size:0.85em">No resources</span>';
  return resources.map(r =>
    `<label style="display:inline-flex;align-items:center;gap:0.25rem;margin-right:0.6rem;font-size:0.85em;white-space:nowrap">
      <input type="checkbox" name="resource_ids" value="${r.id}"${selectedIds.includes(r.id) ? ' checked' : ''}>
      ${escHtml(r.name)}
    </label>`
  ).join('');
}

async function renderCyclesSection(container, resources) {
  const templates = await api.get('/cycle-templates/');

  const templateCards = templates.map(t => {
    const stepRows = t.steps.map((s, i) => `
      <tr class="cycle-step-row" data-step-id="${s.id}" draggable="true">
        <td style="color:var(--text-muted);cursor:grab;user-select:none" title="Drag to reorder">⠿ ${i + 1}</td>
        <td><input name="title" value="${escHtml(s.title)}" style="width:140px"></td>
        <td><input name="duration" type="number" min="0.01" step="any" value="${s.duration ?? ''}" placeholder="—" style="width:70px"></td>
        <td><input name="command" value="${escHtml(s.command ?? '')}" placeholder="shell command (optional)" style="width:220px;font-family:monospace"></td>
        <td>${stepResourceChecks(resources, s.resource_ids || [])}</td>
        <td style="white-space:nowrap;text-align:right">
          <button class="btn btn-ghost save-step-btn" title="Save">💾</button>
          <button class="btn btn-danger del-step-btn" title="Delete step">✕</button>
        </td>
      </tr>`).join('');

    return `
      <div class="cycle-template" data-tmpl-id="${t.id}" style="border:1px solid var(--border);border-radius:6px;padding:0.75rem;margin-bottom:1rem">
        <div style="display:flex;gap:0.5rem;align-items:center;margin-bottom:0.5rem">
          <input class="tmpl-name" value="${escHtml(t.name)}" style="font-weight:600;width:220px">
          <button class="btn btn-ghost rename-tmpl-btn">Rename</button>
          <span style="flex:1"></span>
          <button class="btn btn-danger del-tmpl-btn">Delete cycle</button>
        </div>
        <table style="width:100%">
          <thead><tr>
            <th style="width:24px">#</th><th>Step</th><th>Duration (h)</th><th>Command</th><th>Resources</th><th style="width:90px"></th>
          </tr></thead>
          <tbody>
            ${stepRows || ''}
            <tr class="add-step-row">
              <td style="color:var(--text-muted)">+</td>
              <td><input name="title" placeholder="New step" style="width:140px"></td>
              <td><input name="duration" type="number" min="0.01" step="any" placeholder="—" style="width:70px"></td>
              <td><input name="command" placeholder="shell command (optional)" style="width:220px;font-family:monospace"></td>
              <td>${stepResourceChecks(resources, [])}</td>
              <td style="text-align:right"><button class="btn btn-primary add-step-btn">Add step</button></td>
            </tr>
          </tbody>
        </table>
      </div>`;
  }).join('');

  container.innerHTML = `
    ${templates.length === 0
      ? '<p style="color:var(--text-muted)">No cycles defined yet.</p>'
      : templateCards}
    <form id="new-template-form" class="form-row" style="margin-top:0.5rem;align-items:flex-end">
      <div><label>New cycle name</label><input name="name" placeholder="e.g. Lit Review" style="width:220px"></div>
      <div><button type="submit" class="btn btn-primary">Add cycle</button></div>
    </form>
  `;

  const rerender = () => renderCyclesSection(container, resources);

  container.querySelector('#new-template-form').addEventListener('submit', async e => {
    e.preventDefault();
    const name = new FormData(e.target).get('name').trim();
    if (!name) return;
    try { await api.post('/cycle-templates/', { name }); await rerender(); }
    catch (err) { alert(`Error: ${err.message}`); }
  });

  container.querySelectorAll('.cycle-template').forEach(card => {
    const tid = parseInt(card.dataset.tmplId);

    card.querySelector('.rename-tmpl-btn').addEventListener('click', async () => {
      const name = card.querySelector('.tmpl-name').value.trim();
      if (!name) return;
      try { await api.patch(`/cycle-templates/${tid}`, { name }); await rerender(); }
      catch (err) { alert(`Error: ${err.message}`); }
    });

    card.querySelector('.del-tmpl-btn').addEventListener('click', async () => {
      if (!confirm('Delete this cycle template? Tasks already created from it are unaffected.')) return;
      try { await api.delete(`/cycle-templates/${tid}`); await rerender(); }
      catch (err) { alert(`Error: ${err.message}`); }
    });

    card.querySelectorAll('.cycle-step-row').forEach(row => {
      const sid = parseInt(row.dataset.stepId);
      const readRow = () => {
        const durRaw = row.querySelector('[name="duration"]').value;
        return {
          title: row.querySelector('[name="title"]').value.trim(),
          duration: durRaw ? parseFloat(durRaw) : null,
          command: row.querySelector('[name="command"]').value.trim() || null,
          resource_ids: [...row.querySelectorAll('[name="resource_ids"]:checked')].map(c => parseInt(c.value)),
        };
      };
      row.querySelector('.save-step-btn').addEventListener('click', async () => {
        const body = readRow();
        if (!body.title) { alert('Step needs a title.'); return; }
        try { await api.patch(`/cycle-templates/steps/${sid}`, body); await rerender(); }
        catch (err) { alert(`Error: ${err.message}`); }
      });
      row.querySelector('.del-step-btn').addEventListener('click', async () => {
        try { await api.delete(`/cycle-templates/steps/${sid}`); await rerender(); }
        catch (err) { alert(`Error: ${err.message}`); }
      });
    });

    // Drag-and-drop resequencing
    let dragSrc = null;
    card.querySelectorAll('.cycle-step-row').forEach(row => {
      row.addEventListener('dragstart', e => {
        dragSrc = row;
        e.dataTransfer.effectAllowed = 'move';
        setTimeout(() => row.style.opacity = '0.4', 0);
      });
      row.addEventListener('dragend', () => { row.style.opacity = ''; dragSrc = null; });
      row.addEventListener('dragover', e => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; });
      row.addEventListener('dragenter', e => { e.preventDefault(); if (row !== dragSrc) row.style.outline = '2px solid var(--primary,#0d6efd)'; });
      row.addEventListener('dragleave', () => { row.style.outline = ''; });
      row.addEventListener('drop', async e => {
        e.preventDefault();
        row.style.outline = '';
        if (!dragSrc || dragSrc === row) return;
        const tbody = row.closest('tbody');
        const rows = [...tbody.querySelectorAll('.cycle-step-row')];
        const fromIdx = rows.indexOf(dragSrc);
        const toIdx   = rows.indexOf(row);
        rows.splice(fromIdx, 1);
        rows.splice(toIdx, 0, dragSrc);
        rows.forEach(r => tbody.insertBefore(r, tbody.querySelector('.add-step-row')));
        // Update position numbers in the # column
        rows.forEach((r, i) => { r.querySelector('td:first-child').textContent = `⠿ ${i + 1}`; });
        // Persist new order
        try {
          await Promise.all(rows.map((r, i) =>
            api.patch(`/cycle-templates/steps/${r.dataset.stepId}`, { position: i })
          ));
        } catch (err) { alert(`Error saving order: ${err.message}`); await rerender(); }
      });
    });

    const addRow = card.querySelector('.add-step-row');
    const stepCount = card.querySelectorAll('.cycle-step-row').length;
    addRow.querySelector('.add-step-btn').addEventListener('click', async () => {
      const durRaw = addRow.querySelector('[name="duration"]').value;
      const title = addRow.querySelector('[name="title"]').value.trim();
      if (!title) { alert('Step needs a title.'); return; }
      try {
        await api.post(`/cycle-templates/${tid}/steps`, {
          title,
          duration: durRaw ? parseFloat(durRaw) : null,
          command: addRow.querySelector('[name="command"]').value.trim() || null,
          resource_ids: [...addRow.querySelectorAll('[name="resource_ids"]:checked')].map(c => parseInt(c.value)),
          position: stepCount,
        });
        await rerender();
      } catch (err) { alert(`Error: ${err.message}`); }
    });
  });
}
