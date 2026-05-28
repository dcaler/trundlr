// ── Projects view ─────────────────────────────────────────────────────────

async function showProjectsList(el) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const projects = await api.get('/projects/');

  el.innerHTML = `
    <h1>Projects</h1>

    <form id="create-project-form" class="form-row" style="margin-bottom:1.5rem">
      <div>
        <label>Name</label>
        <input name="name" required placeholder="Project name" style="width:200px">
      </div>
      <div>
        <label>Description</label>
        <input name="description" placeholder="Optional" style="width:260px">
      </div>
      <div style="align-self:flex-end">
        <button type="submit" class="btn btn-primary">+ New Project</button>
      </div>
    </form>

    ${projects.length === 0
      ? '<p style="color:var(--text-muted)">No projects yet — create one above.</p>'
      : `<table>
          <thead><tr>
            <th>Name</th><th>Description</th><th style="width:160px"></th>
          </tr></thead>
          <tbody>
            ${projects.map(p => `
              <tr>
                <td><strong>${escHtml(p.name)}</strong></td>
                <td style="color:var(--text-muted)">${escHtml(p.description || '—')}</td>
                <td style="white-space:nowrap;text-align:right">
                  <button class="btn btn-ghost view-btn" data-id="${p.id}">View tasks</button>
                  <button class="btn btn-danger delete-project-btn" data-id="${p.id}">✕</button>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>`}
  `;

  el.querySelector('#create-project-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await api.post('/projects/', {
        name: fd.get('name'),
        description: fd.get('description') || null,
      });
      await showProjectsList(el);
    } catch (err) {
      alert(`Error: ${err.message}`);
    }
  });

  el.querySelectorAll('.view-btn').forEach(btn =>
    btn.addEventListener('click', () => showProjectDetail(el, parseInt(btn.dataset.id)))
  );

  el.querySelectorAll('.delete-project-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      if (!confirm('Delete this project and all its tasks?')) return;
      try {
        await api.delete(`/projects/${btn.dataset.id}`);
        await showProjectsList(el);
      } catch (err) {
        alert(`Error: ${err.message}`);
      }
    })
  );
}

async function showProjectDetail(el, projectId) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const [project, tasks, resources] = await Promise.all([
    api.get(`/projects/${projectId}`),
    api.get(`/tasks/?project_id=${projectId}`),
    api.get('/resources/'),
  ]);

  const resourceOptions = [
    '<option value="">— unassigned —</option>',
    ...resources.map(r => `<option value="${r.id}">${escHtml(r.name)} (${escHtml(r.kind)})</option>`),
  ].join('');

  const statusOptions = ['todo', 'in_progress', 'blocked', 'done']
    .map(s => `<option value="${s}">${s.replace('_', ' ')}</option>`).join('');

  const resourceById = Object.fromEntries(resources.map(r => [r.id, r]));

  el.innerHTML = `
    <div style="margin-bottom:1rem">
      <button class="btn btn-ghost back-btn">← Projects</button>
    </div>
    <h1>${escHtml(project.name)}</h1>
    ${project.description
      ? `<p style="color:var(--text-muted);margin-bottom:1rem">${escHtml(project.description)}</p>`
      : ''}

    <h2 style="margin-top:1.5rem;margin-bottom:0.75rem">Add task</h2>
    <form id="add-task-form" class="form-row" style="margin-bottom:1.5rem;flex-wrap:wrap">
      <div>
        <label>Title *</label>
        <input name="title" required placeholder="Task title" style="width:180px">
      </div>
      <div>
        <label>Resource</label>
        <select name="resource_id">${resourceOptions}</select>
      </div>
      <div>
        <label>Start</label>
        <input type="date" name="start_date">
      </div>
      <div>
        <label>End</label>
        <input type="date" name="end_date">
      </div>
      <div>
        <label>Load</label>
        <input type="number" name="load" value="1" min="0.1" step="0.5" style="width:70px">
      </div>
      <div>
        <label>Status</label>
        <select name="status">${statusOptions}</select>
      </div>
      <div style="align-self:flex-end">
        <button type="submit" class="btn btn-primary">Add task</button>
      </div>
    </form>

    <h2>Tasks (${tasks.length})</h2>
    ${tasks.length === 0
      ? '<p style="color:var(--text-muted)">No tasks yet — add one above.</p>'
      : `<table>
          <thead><tr>
            <th>Title</th><th>Status</th><th>Resource</th>
            <th>Start</th><th>End</th><th>Load</th><th style="width:80px"></th>
          </tr></thead>
          <tbody>
            ${tasks.map(t => {
              const res = resourceById[t.resource_id];
              const statusSel = ['todo', 'in_progress', 'blocked', 'done']
                .map(s => `<option value="${s}"${s === t.status ? ' selected' : ''}>${s.replace('_', ' ')}</option>`)
                .join('');
              return `<tr>
                <td>${escHtml(t.title)}</td>
                <td>
                  <select class="status-select" data-id="${t.id}" style="font-size:0.8rem">
                    ${statusSel}
                  </select>
                </td>
                <td>${res ? escHtml(res.name) : '<span style="color:var(--text-muted)">—</span>'}</td>
                <td>${t.start_date || '—'}</td>
                <td>${t.end_date || '—'}</td>
                <td>${t.load}</td>
                <td style="text-align:right">
                  <button class="btn btn-danger delete-task-btn" data-id="${t.id}">✕</button>
                </td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>`}
  `;

  el.querySelector('.back-btn').addEventListener('click', () => showProjectsList(el));

  el.querySelector('#add-task-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const ridRaw = fd.get('resource_id');
    try {
      await api.post('/tasks/', {
        title: fd.get('title'),
        project_id: projectId,
        resource_id: ridRaw ? parseInt(ridRaw) : null,
        start_date: fd.get('start_date') || null,
        end_date: fd.get('end_date') || null,
        load: parseFloat(fd.get('load')),
        status: fd.get('status'),
      });
      await showProjectDetail(el, projectId);
    } catch (err) {
      alert(`Error: ${err.message}`);
    }
  });

  el.querySelectorAll('.status-select').forEach(sel =>
    sel.addEventListener('change', async () => {
      try {
        await api.patch(`/tasks/${sel.dataset.id}`, { status: sel.value });
      } catch (err) {
        alert(`Error: ${err.message}`);
        await showProjectDetail(el, projectId);
      }
    })
  );

  el.querySelectorAll('.delete-task-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      if (!confirm('Delete this task?')) return;
      try {
        await api.delete(`/tasks/${btn.dataset.id}`);
        await showProjectDetail(el, projectId);
      } catch (err) {
        alert(`Error: ${err.message}`);
      }
    })
  );
}

registerView('/projects', async (el) => {
  await showProjectsList(el);
});
