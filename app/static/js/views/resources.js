// ── Resources view ────────────────────────────────────────────────────────

const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DAY_VALUES = [1, 2, 4, 8, 16, 32, 64];

const AVAILABILITY_KINDS = new Set(['human', 'ai']);

function availabilityLabel(r) {
  if (!AVAILABILITY_KINDS.has(r.kind)) return null;
  const from = r.available_from || '?';
  const to   = r.available_to   || '?';
  const days = DAY_VALUES
    .map((v, i) => (r.available_days & v) ? DAY_LABELS[i] : null)
    .filter(Boolean)
    .join(' ');
  return `${days} ${from}–${to}`;
}

function parseDaysMask(form) {
  let mask = 0;
  DAY_VALUES.forEach((v, i) => {
    if (form.querySelector(`[name="day_${i}"]`)?.checked) mask |= v;
  });
  return mask;
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
        <input type="time" name="available_from" value="${escHtml(from)}" required style="width:110px">
      </div>
      <div>
        <label>To</label>
        <input type="time" name="available_to" value="${escHtml(to)}" required style="width:110px">
      </div>
      <div style="display:flex;gap:0.3rem;flex-wrap:wrap;align-items:center">
        ${dayCheckboxes(days)}
      </div>
    </div>`;
}

function capacityField(r = null) {
  const val = r?.capacity ?? 4;
  return `<div><label>Capacity</label>
    <input type="number" name="capacity" value="${val}" required min="0.01" step="any"
           style="width:80px" title="parallel slots for compute"></div>`;
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

function buildPayload(fd, kind) {
  if (kind === 'human') {
    return {
      name: fd.get('name'),
      kind,
      available_from: fd.get('available_from'),
      available_to:   fd.get('available_to'),
      available_days: parseInt(fd.get('available_days') || '0'),
    };
  }
  return { name: fd.get('name'), kind, capacity: parseFloat(fd.get('capacity')) };
}

function collectDaysFromForm(form) {
  let mask = 0;
  DAY_VALUES.forEach((v, i) => {
    if (form.querySelector(`[name="day_${i}"]`)?.checked) mask |= v;
  });
  return mask;
}

function wireKindToggle(container, kindSelect, showCreate = true) {
  function refresh() {
    const isHuman = AVAILABILITY_KINDS.has(kindSelect.value);
    container.querySelectorAll('.avail-fields').forEach(el => {
      el.style.display = isHuman ? '' : 'none';
    });
    container.querySelectorAll('.capacity-field').forEach(el => {
      el.style.display = isHuman ? 'none' : '';
    });
  }
  kindSelect.addEventListener('change', refresh);
  refresh();
}

async function showResourcesList(el, editingId = null) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const resources = await api.get('/resources/');

  const kindLabel = { human: 'Human', ai: 'AI', cpu: 'CPU (slots)', gpu: 'GPU (slots)' };

  const rows = resources.map(r => {
    if (r.id === editingId) {
      const isHuman = AVAILABILITY_KINDS.has(r.kind);
      return `<tr class="edit-row" data-id="${r.id}">
        <td colspan="4">
          <form class="form-row edit-resource-form" style="flex-wrap:wrap;gap:0.5rem;padding:0.25rem 0">
            <div><label>Name</label><input name="name" value="${escHtml(r.name)}" required style="width:160px"></div>
            ${kindField(r.kind)}
            <div class="avail-fields" style="display:${isHuman ? '' : 'none'}">
              ${availabilityFields(r)}
            </div>
            <div class="capacity-field" style="display:${isHuman ? 'none' : ''}">
              ${capacityField(r)}
            </div>
            <div style="align-self:flex-end;display:flex;gap:0.25rem">
              <button type="submit" class="btn btn-primary">Save</button>
              <button type="button" class="btn btn-ghost cancel-resource-edit">Cancel</button>
            </div>
          </form>
        </td>
      </tr>`;
    }

    const avail = AVAILABILITY_KINDS.has(r.kind)
      ? escHtml(availabilityLabel(r) || '—')
      : `${r.capacity} slots`;

    return `<tr>
      <td><button class="btn btn-ghost edit-resource-btn" data-id="${r.id}" style="font-weight:600;padding:0;text-align:left">${escHtml(r.name)}</button></td>
      <td>${escHtml(kindLabel[r.kind] || r.kind)}</td>
      <td>${avail}</td>
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
      <div class="capacity-field" style="display:none">
        ${capacityField()}
      </div>
      <div style="align-self:flex-end">
        <button type="submit" class="btn btn-primary">+ Add Resource</button>
      </div>
    </form>

    ${resources.length === 0
      ? '<p style="color:var(--text-muted)">No resources yet — add one above.</p>'
      : `<table>
          <thead><tr>
            <th>Name</th><th>Kind</th><th>Availability / Capacity</th><th style="width:120px"></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`}
  `;

  // Wire kind toggle on create form
  const createForm = el.querySelector('#create-resource-form');
  wireKindToggle(createForm, createForm.querySelector('[name="kind"]'));

  createForm.addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const kind = fd.get('kind');
    const payload = AVAILABILITY_KINDS.has(kind)
      ? {
          name: fd.get('name'),
          kind,
          available_from: fd.get('available_from'),
          available_to:   fd.get('available_to'),
          available_days: collectDaysFromForm(e.target),
        }
      : {
          name: fd.get('name'),
          kind,
          capacity: parseFloat(fd.get('capacity')),
        };
    try {
      await api.post('/resources/', payload);
      await showResourcesList(el);
    } catch (err) { alert(`Error: ${err.message}`); }
  });

  el.querySelectorAll('.edit-resource-btn').forEach(btn =>
    btn.addEventListener('click', () => showResourcesList(el, parseInt(btn.dataset.id)))
  );

  const editForm = el.querySelector('.edit-resource-form');
  if (editForm) {
    const editRow = editForm.closest('tr');
    wireKindToggle(editForm, editForm.querySelector('[name="kind"]'));

    editForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const kind = fd.get('kind');
      const payload = AVAILABILITY_KINDS.has(kind)
        ? {
            name: fd.get('name'),
            kind,
            available_from: fd.get('available_from'),
            available_to:   fd.get('available_to'),
            available_days: collectDaysFromForm(e.target),
          }
        : {
            name: fd.get('name'),
            kind,
            capacity: parseFloat(fd.get('capacity')),
          };
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
