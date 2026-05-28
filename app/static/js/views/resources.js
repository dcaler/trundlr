// ── Resources view ────────────────────────────────────────────────────────

async function showResourcesList(el) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const resources = await api.get('/resources/');

  const kindLabel = { human: 'Human (hours/day)', cpu: 'CPU (slots)', gpu: 'GPU (slots)' };

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
        <input type="number" name="capacity" required min="0.1" step="0.5" value="8"
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
            <th>Name</th><th>Kind</th><th>Capacity</th><th style="width:80px"></th>
          </tr></thead>
          <tbody>
            ${resources.map(r => `
              <tr>
                <td><strong>${escHtml(r.name)}</strong></td>
                <td>${escHtml(kindLabel[r.kind] || r.kind)}</td>
                <td>${r.capacity}</td>
                <td style="text-align:right">
                  <button class="btn btn-danger delete-resource-btn" data-id="${r.id}">✕</button>
                </td>
              </tr>`).join('')}
          </tbody>
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
    } catch (err) {
      alert(`Error: ${err.message}`);
    }
  });

  el.querySelectorAll('.delete-resource-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      if (!confirm('Delete this resource? Assigned tasks will be unassigned.')) return;
      try {
        await api.delete(`/resources/${btn.dataset.id}`);
        await showResourcesList(el);
      } catch (err) {
        alert(`Error: ${err.message}`);
      }
    })
  );
}

registerView('/resources', async (el) => {
  await showResourcesList(el);
});
