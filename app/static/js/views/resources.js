// ── Resources view ────────────────────────────────────────────────────────

async function showResourcesList(el, editingId = null) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const resources = await api.get('/resources/');

  const kindLabel = { human: 'Human (hours/day)', cpu: 'CPU (slots)', gpu: 'GPU (slots)' };

  const rows = resources.map(r => {
    if (r.id === editingId) {
      return `<tr class="edit-row" data-id="${r.id}">
        <td colspan="4">
          <form class="form-row edit-resource-form" style="flex-wrap:wrap;gap:0.5rem;padding:0.25rem 0">
            <div><label>Name</label><input name="name" value="${escHtml(r.name)}" required style="width:160px"></div>
            <div>
              <label>Kind</label>
              <select name="kind">
                <option value="human"${r.kind === 'human' ? ' selected' : ''}>Human</option>
                <option value="cpu"${r.kind === 'cpu' ? ' selected' : ''}>CPU</option>
                <option value="gpu"${r.kind === 'gpu' ? ' selected' : ''}>GPU</option>
              </select>
            </div>
            <div><label>Capacity</label><input type="number" name="capacity" value="${r.capacity}" required min="0.01" step="any" style="width:80px"></div>
            <div style="align-self:flex-end;display:flex;gap:0.25rem">
              <button type="submit" class="btn btn-primary">Save</button>
              <button type="button" class="btn btn-ghost cancel-resource-edit">Cancel</button>
            </div>
          </form>
        </td>
      </tr>`;
    }
    return `<tr>
      <td><button class="btn btn-ghost edit-resource-btn" data-id="${r.id}" style="font-weight:600;padding:0;text-align:left">${escHtml(r.name)}</button></td>
      <td>${escHtml(kindLabel[r.kind] || r.kind)}</td>
      <td>${r.capacity}</td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn btn-ghost edit-resource-btn" data-id="${r.id}" title="Edit">✎</button>
        <button class="btn btn-danger delete-resource-btn" data-id="${r.id}">✕</button>
      </td>
    </tr>`;
  }).join('');

  el.innerHTML = `
    <h1>Resources</h1>

    <form id="create-resource-form" class="form-row" style="margin-bottom:1.5rem">
      <div>
        <label>Name</label>
        <input name="name" required placeholder="Resource name" style="width:180px">
      </div>
      <div>
        <label>Kind</label>
        <select name="kind">
          <option value="human">Human</option>
          <option value="cpu">CPU</option>
          <option value="gpu">GPU</option>
        </select>
      </div>
      <div>
        <label>Capacity</label>
        <input type="number" name="capacity" required min="0.01" step="any" value="8"
               style="width:80px" title="hours/day for humans; slots for compute">
      </div>
      <div style="align-self:flex-end">
        <button type="submit" class="btn btn-primary">+ Add Resource</button>
      </div>
    </form>

    ${resources.length === 0
      ? '<p style="color:var(--text-muted)">No resources yet — add one above.</p>'
      : `<table>
          <thead><tr>
            <th>Name</th><th>Kind</th><th>Capacity</th><th style="width:100px"></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`}
  `;

  el.querySelector('#create-resource-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await api.post('/resources/', {
        name: fd.get('name'),
        kind: fd.get('kind'),
        capacity: parseFloat(fd.get('capacity')),
      });
      await showResourcesList(el);
    } catch (err) { alert(`Error: ${err.message}`); }
  });

  el.querySelectorAll('.edit-resource-btn').forEach(btn =>
    btn.addEventListener('click', () => showResourcesList(el, parseInt(btn.dataset.id)))
  );

  const editForm = el.querySelector('.edit-resource-form');
  if (editForm) {
    const editRow = editForm.closest('tr');
    editForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try {
        await api.patch(`/resources/${editRow.dataset.id}`, {
          name: fd.get('name'),
          kind: fd.get('kind'),
          capacity: parseFloat(fd.get('capacity')),
        });
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
