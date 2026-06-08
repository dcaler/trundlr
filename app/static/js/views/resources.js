// ── Resources view ────────────────────────────────────────────────────────

const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DAY_VALUES = [1, 2, 4, 8, 16, 32, 64];

function availabilityLabel(r) {
  const from = r.available_from || '?';
  const to   = r.available_to   || '?';
  const days = DAY_VALUES
    .map((v, i) => (r.available_days & v) ? DAY_LABELS[i] : null)
    .filter(Boolean)
    .join(' ');
  return `${days} ${from}–${to}`;
}

function dayCheckboxes(selected = 31) {
  return DAY_LABELS.map((label, i) => {
    const checked = (selected & DAY_VALUES[i]) ? 'checked' : '';
    return `<label style="white-space:nowrap;font-size:0.85em">
      <input type="checkbox" name="day_${i}" value="${DAY_VALUES[i]}" ${checked}> ${label}
    </label>`;
  }).join(' ');
}

function availabilityFields(r = null) {
  const from = r?.available_from ?? '09:00';
  const to   = r?.available_to   ?? '17:00';
  const days = r?.available_days ?? 31;
  return `
    <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap">
      <div>
        <label>From</label>
        <input type="text" name="available_from" value="${escHtml(from)}" placeholder="HH:MM" maxlength="5" required style="width:80px">
      </div>
      <div>
        <label>To</label>
        <input type="text" name="available_to" value="${escHtml(to)}" placeholder="HH:MM" maxlength="5" required style="width:80px">
      </div>
      <div style="display:flex;gap:0.3rem;flex-wrap:wrap;align-items:center">
        ${dayCheckboxes(days)}
      </div>
    </div>`;
}

function kindField(selected = 'human', includeLabel = true) {
  const opt = (val, label) =>
    `<option value="${val}"${selected === val ? ' selected' : ''}>${label}</option>`;
  return `<div>${includeLabel ? '<label>Kind</label>' : ''}
    <select name="kind">
      ${opt('human', 'Human')}
      ${opt('ai', 'AI')}
      ${opt('cpu', 'CPU')}
      ${opt('gpu', 'GPU')}
    </select>
  </div>`;
}

function collectDaysFromForm(form) {
  let mask = 0;
  DAY_VALUES.forEach((v, i) => {
    if (form.querySelector(`[name="day_${i}"]`)?.checked) mask |= v;
  });
  return mask;
}

function buildPayload(fd, form) {
  return {
    name: fd.get('name'),
    kind: fd.get('kind'),
    available_from: fd.get('available_from'),
    available_to:   fd.get('available_to'),
    available_days: collectDaysFromForm(form),
  };
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function renderWindowsSection(windows, resourceId) {
  const sorted = [...windows].sort((a, b) => a.day_of_week - b.day_of_week || a.from_time.localeCompare(b.from_time));
  const dayOptions = DAY_LABELS.map((d, i) => `<option value="${i}">${d}</option>`).join('');

  const rows = sorted.map(w => `<tr>
    <td>${DAY_LABELS[w.day_of_week]}</td>
    <td>${escHtml(w.from_time)}</td>
    <td>${escHtml(w.to_time)}</td>
    <td style="text-align:right">
      <button class="btn btn-danger delete-window-btn" data-id="${w.id}">✕</button>
    </td>
  </tr>`).join('');

  const notice = windows.length === 0
    ? `<p style="color:var(--text-muted);font-size:0.85rem;margin:0 0 0.5rem">
        None — default availability (shown above) is used for scheduling.
       </p>`
    : `<p style="color:var(--text-muted);font-size:0.85rem;margin:0 0 0.5rem">
        These windows replace the default availability for scheduling purposes.
       </p>`;

  return `
    <div style="margin-bottom:1.5rem">
      <h3 style="margin:0 0 0.25rem;font-size:1rem">Weekly windows</h3>
      ${notice}
      ${windows.length > 0 ? `<table style="margin-bottom:0.5rem">
        <thead><tr><th>Day</th><th>From</th><th>To</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>` : ''}
      <form id="add-window-form" class="form-row" style="flex-wrap:wrap;gap:0.5rem;align-items:flex-end">
        <div>
          <label>Day</label>
          <select name="day_of_week">${dayOptions}</select>
        </div>
        <div>
          <label>From</label>
          <input name="from_time" type="text" value="09:00" placeholder="HH:MM" maxlength="5" style="width:70px" required>
        </div>
        <div>
          <label>To</label>
          <input name="to_time" type="text" value="17:00" placeholder="HH:MM" maxlength="5" style="width:70px" required>
        </div>
        <div style="align-self:flex-end">
          <button type="submit" class="btn btn-primary">+ Add window</button>
        </div>
      </form>
    </div>`;
}

function renderBlockoutsSection(blockouts) {
  const sorted = [...blockouts].sort((a, b) => a.start_date.localeCompare(b.start_date));

  const rows = sorted.map(b => {
    const dates = b.start_date === b.end_date
      ? fmtDate(b.start_date)
      : `${fmtDate(b.start_date)} – ${fmtDate(b.end_date)}`;
    const times = b.from_time
      ? `${b.from_time}–${b.to_time || '?'}`
      : 'all day';
    return `<tr>
      <td>${escHtml(dates)}</td>
      <td style="font-size:0.85rem;color:var(--text-muted)">${escHtml(times)}</td>
      <td style="font-size:0.85rem">${escHtml(b.note || '')}</td>
      <td style="text-align:right">
        <button class="btn btn-danger delete-blockout-btn" data-id="${b.id}">✕</button>
      </td>
    </tr>`;
  }).join('');

  return `
    <div>
      <h3 style="margin:0 0 0.25rem;font-size:1rem">Blockouts</h3>
      <p style="color:var(--text-muted);font-size:0.85rem;margin:0 0 0.5rem">
        Block specific date ranges regardless of windows (e.g. vacation, maintenance).
      </p>
      ${blockouts.length > 0 ? `<table style="margin-bottom:0.5rem">
        <thead><tr><th>Dates</th><th>Times</th><th>Note</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>` : ''}
      <form id="add-blockout-form" class="form-row" style="flex-wrap:wrap;gap:0.5rem;align-items:flex-end">
        <div>
          <label>Start</label>
          <input name="start_date" type="date" required style="width:130px">
        </div>
        <div>
          <label>End</label>
          <input name="end_date" type="date" required style="width:130px">
        </div>
        <div>
          <label>From time <span style="font-weight:normal;color:var(--text-muted)">(optional)</span></label>
          <input name="from_time" type="text" placeholder="HH:MM" maxlength="5" style="width:70px">
        </div>
        <div>
          <label>To time <span style="font-weight:normal;color:var(--text-muted)">(optional)</span></label>
          <input name="to_time" type="text" placeholder="HH:MM" maxlength="5" style="width:70px">
        </div>
        <div>
          <label>Note <span style="font-weight:normal;color:var(--text-muted)">(optional)</span></label>
          <input name="note" type="text" placeholder="e.g. Vacation" style="width:140px">
        </div>
        <div style="align-self:flex-end">
          <button type="submit" class="btn btn-primary">+ Add blockout</button>
        </div>
      </form>
    </div>`;
}

async function showResourceDetail(el, resourceId, showCompleted = false) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const [resource, tasks, projects, windows, blockouts] = await Promise.all([
    api.get(`/resources/${resourceId}`),
    api.get(`/tasks/?resource_id=${resourceId}`),
    api.get('/projects/'),
    api.get(`/resources/${resourceId}/windows`),
    api.get(`/resources/${resourceId}/blockouts`),
  ]);

  const projectById = Object.fromEntries(projects.map(p => [p.id, p]));
  const kindLabel   = { human: 'Human', ai: 'AI', cpu: 'CPU', gpu: 'GPU' };
  const DONE_STATUSES = new Set(['done', 'failed']);

  const visible = showCompleted ? tasks : tasks.filter(t => !DONE_STATUSES.has(t.status));
  const hiddenCount = tasks.length - visible.length;

  const rows = visible.map(t => {
    const project = projectById[t.project_id] || {};
    return `<tr>
      <td>${escHtml(t.title)}${t.description ? `<div style="font-size:0.75rem;color:var(--text-muted)">${escHtml(t.description)}</div>` : ''}</td>
      <td>${escHtml(project.name || '—')}</td>
      <td>${statusBadge(t.status)}</td>
      <td style="font-size:0.8rem">${fmtDt(t.start_date)}</td>
      <td style="font-size:0.8rem">${fmtDt(t.end_date)}</td>
    </tr>`;
  }).join('');

  const toggleLabel = showCompleted
    ? 'Hide completed'
    : `Show completed${hiddenCount > 0 ? ` (${hiddenCount})` : ''}`;

  const scheduleLabel = windows.length > 0
    ? `Custom schedule (${windows.length} window${windows.length !== 1 ? 's' : ''})`
    : availabilityLabel(resource);

  el.innerHTML = `
    <div style="margin-bottom:1rem">
      <button class="btn btn-ghost back-btn">← Resources</button>
    </div>
    <h1>${escHtml(resource.name)}</h1>
    <p style="color:var(--text-muted);margin-bottom:1.5rem">${escHtml(kindLabel[resource.kind] || resource.kind)} · ${escHtml(scheduleLabel)}</p>

    <div style="display:flex;align-items:baseline;gap:1rem;margin-bottom:0.5rem">
      <h2 style="margin:0">Tasks (${visible.length}${hiddenCount > 0 && !showCompleted ? `/${tasks.length}` : ''})</h2>
      ${tasks.some(t => DONE_STATUSES.has(t.status))
        ? `<button class="btn btn-ghost toggle-completed-btn" style="font-size:0.8rem;padding:0.1rem 0.4rem">${toggleLabel}</button>`
        : ''}
    </div>
    ${visible.length === 0
      ? '<p style="color:var(--text-muted)">No active tasks assigned to this resource.</p>'
      : `<table>
          <thead><tr>
            <th>Title</th><th>Project</th><th>Status</th><th>Start</th><th>End</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`}

    <div style="margin-top:2.5rem;border-top:1px solid var(--border);padding-top:1.5rem">
      <h2 style="margin:0 0 1rem">Schedule</h2>
      ${renderWindowsSection(windows, resourceId)}
      ${renderBlockoutsSection(blockouts)}
    </div>
  `;

  el.querySelector('.back-btn').addEventListener('click', () => showResourcesList(el));
  el.querySelector('.toggle-completed-btn')?.addEventListener('click',
    () => showResourceDetail(el, resourceId, !showCompleted)
  );

  el.querySelectorAll('.delete-window-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      try {
        await api.delete(`/resources/${resourceId}/windows/${btn.dataset.id}`);
        await showResourceDetail(el, resourceId, showCompleted);
      } catch (err) { alert(`Error: ${err.message}`); }
    })
  );

  el.querySelectorAll('.delete-blockout-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      try {
        await api.delete(`/resources/${resourceId}/blockouts/${btn.dataset.id}`);
        await showResourceDetail(el, resourceId, showCompleted);
      } catch (err) { alert(`Error: ${err.message}`); }
    })
  );

  el.querySelector('#add-window-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = {
      day_of_week: parseInt(fd.get('day_of_week')),
      from_time: fd.get('from_time'),
      to_time: fd.get('to_time'),
    };
    try {
      await api.post(`/resources/${resourceId}/windows`, payload);
      await showResourceDetail(el, resourceId, showCompleted);
    } catch (err) { alert(`Error: ${err.message}`); }
  });

  el.querySelector('#add-blockout-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = {
      start_date: fd.get('start_date'),
      end_date: fd.get('end_date'),
      from_time: fd.get('from_time') || null,
      to_time: fd.get('to_time') || null,
      note: fd.get('note') || null,
    };
    try {
      await api.post(`/resources/${resourceId}/blockouts`, payload);
      await showResourceDetail(el, resourceId, showCompleted);
    } catch (err) { alert(`Error: ${err.message}`); }
  });
}

async function showResourcesList(el, editingId = null) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const resources = await api.get('/resources/');

  const kindLabel = { human: 'Human', ai: 'AI', cpu: 'CPU', gpu: 'GPU' };

  const rows = resources.map(r => {
    if (r.id === editingId) {
      return `<tr class="edit-row" data-id="${r.id}">
        <td colspan="4">
          <form class="form-row edit-resource-form" style="flex-wrap:wrap;gap:0.5rem;padding:0.25rem 0">
            <div><label>Name</label><input name="name" value="${escHtml(r.name)}" required style="width:160px"></div>
            ${kindField(r.kind)}
            <div class="avail-fields">
              ${availabilityFields(r)}
            </div>
            <div style="align-self:flex-end;display:flex;gap:0.25rem">
              <button type="submit" class="btn btn-primary">Save</button>
              <button type="button" class="btn btn-ghost cancel-resource-edit">Cancel</button>
            </div>
          </form>
        </td>
      </tr>`;
    }

    return `<tr>
      <td><button class="btn btn-ghost view-resource-btn" data-id="${r.id}" style="font-weight:600;padding:0;text-align:left">${escHtml(r.name)}</button></td>
      <td>${escHtml(kindLabel[r.kind] || r.kind)}</td>
      <td>${escHtml(availabilityLabel(r))}</td>
      <td style="text-align:right;white-space:nowrap">
        <a href="/api/resources/${r.id}/calendar.ics" title="Subscribe (iCal)"
           style="margin-right:0.4rem;text-decoration:none;font-size:1rem">&#128197;</a>
        <button class="btn btn-ghost edit-resource-btn" data-id="${r.id}" title="Edit">✎</button>
        <button class="btn btn-danger delete-resource-btn" data-id="${r.id}">✕</button>
      </td>
    </tr>`;
  }).join('');

  el.innerHTML = `
    <h1>Resources</h1>

    <form id="create-resource-form" class="form-row" style="margin-bottom:1.5rem;flex-wrap:wrap;gap:0.5rem;align-items:flex-start">
      <div>
        <label>Name</label>
        <input name="name" required placeholder="Resource name" style="width:180px">
      </div>
      ${kindField('human')}
      <div class="avail-fields">
        ${availabilityFields()}
      </div>
      <div style="align-self:flex-end">
        <button type="submit" class="btn btn-primary">+ Add Resource</button>
      </div>
    </form>

    ${resources.length === 0
      ? '<p style="color:var(--text-muted)">No resources yet — add one above.</p>'
      : `<table>
          <thead><tr>
            <th>Name</th><th>Kind</th><th>Availability</th><th style="width:120px"></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`}
  `;

  const createForm = el.querySelector('#create-resource-form');
  createForm.addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = buildPayload(fd, e.target);
    try {
      await api.post('/resources/', payload);
      await showResourcesList(el);
    } catch (err) { alert(`Error: ${err.message}`); }
  });

  el.querySelectorAll('.view-resource-btn').forEach(btn =>
    btn.addEventListener('click', () => showResourceDetail(el, parseInt(btn.dataset.id)))
  );

  el.querySelectorAll('.edit-resource-btn').forEach(btn =>
    btn.addEventListener('click', () => showResourcesList(el, parseInt(btn.dataset.id)))
  );

  const editForm = el.querySelector('.edit-resource-form');
  if (editForm) {
    const editRow = editForm.closest('tr');

    editForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const payload = buildPayload(fd, e.target);
      try {
        await api.patch(`/resources/${editRow.dataset.id}`, payload);
        await showResourcesList(el);
      } catch (err) { alert(`Error: ${err.message}`); }
    });
    el.querySelector('.cancel-resource-edit').addEventListener('click',
      () => showResourcesList(el)
    );
  }

  el.querySelectorAll('.delete-resource-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      if (!confirm('Delete this resource? Assigned tasks will be unassigned.')) return;
      try {
        await api.delete(`/resources/${btn.dataset.id}`);
        await showResourcesList(el);
      } catch (err) { alert(`Error: ${err.message}`); }
    })
  );
}

registerView('/resources', async (el) => {
  await showResourcesList(el);
});
