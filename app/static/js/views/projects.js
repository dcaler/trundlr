// ── Projects view ─────────────────────────────────────────────────────────

// Format ISO datetime string for display: "2025-06-01T09:00:00" → "2025-06-01 09:00"
function fmtDt(iso) {
  if (!iso) return '—';
  return iso.replace('T', ' ').slice(0, 16);
}

// Truncate ISO datetime to "YYYY-MM-DDTHH:MM" for datetime-local input value
function dtLocal(iso) {
  if (!iso) return '';
  return iso.slice(0, 16).replace(' ', 'T');
}

// Wire up start_date + duration → auto-fill end_date (readonly) on a task form.
function setupAutoCalcEnd(form) {
  const startEl = form.querySelector('[name="start_date"]');
  const durEl   = form.querySelector('[name="duration"]');
  const endEl   = form.querySelector('[name="end_date"]');
  if (!startEl || !durEl || !endEl) return;
  function calc() {
    if (!startEl.value || !durEl.value) { endEl.value = ''; return; }
    const ms = new Date(startEl.value).getTime() + parseFloat(durEl.value) * 3_600_000;
    if (!isNaN(ms)) endEl.value = new Date(ms).toISOString().slice(0, 16);
  }
  startEl.addEventListener('change', calc);
  durEl.addEventListener('change', calc);
  calc(); // run once on load in case values are already set
}

// When a resource is selected in a task form, fill start_date with its next available slot.
function setupResourceAutoStart(form) {
  const ridEl   = form.querySelector('[name="resource_id"]');
  const startEl = form.querySelector('[name="start_date"]');
  if (!ridEl || !startEl) return;
  ridEl.addEventListener('change', async () => {
    const rid = ridEl.value;
    if (!rid) return;
    try {
      const data = await api.get(`/resources/${rid}/next-available`);
      if (data.next_available) {
        startEl.value = data.next_available.slice(0, 16).replace(' ', 'T');
        startEl.dispatchEvent(new Event('change'));
      }
    } catch (_) { /* leave blank if fetch fails */ }
  });
}

// When a dependency task is selected, fill start_date from that task's end (or start).
function setupDependencyAutoStart(form, taskById) {
  const depEl   = form.querySelector('[name="depends_on_id"]');
  const startEl = form.querySelector('[name="start_date"]');
  if (!depEl || !startEl) return;
  depEl.addEventListener('change', () => {
    const depId = parseInt(depEl.value);
    if (!depId) return;
    const dep = taskById[depId];
    if (!dep) return;
    const anchor = dep.end_date || dep.start_date;
    if (anchor) {
      startEl.value = anchor.slice(0, 16).replace(' ', 'T');
      startEl.dispatchEvent(new Event('change'));
    }
  });
}

async function showProjectsList(el, editingId = null) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const projects = await api.get('/projects/');

  const rows = projects.map(p => {
    if (p.id === editingId) {
      return `<tr class="edit-row" data-id="${p.id}">
        <td colspan="4">
          <form class="form-row edit-project-form" style="flex-wrap:wrap;gap:0.5rem;padding:0.25rem 0">
            <div><label>Name</label><input name="name" value="${escHtml(p.name)}" required style="width:180px"></div>
            <div><label>Folder</label><input name="folder" value="${escHtml(p.folder || '')}" style="width:140px"></div>
            <div><label>Description</label><input name="description" value="${escHtml(p.description || '')}" style="width:220px"></div>
            <div style="align-self:flex-end;display:flex;gap:0.25rem">
              <button type="submit" class="btn btn-primary">Save</button>
              <button type="button" class="btn btn-ghost cancel-project-edit">Cancel</button>
            </div>
          </form>
        </td>
      </tr>`;
    }
    return `<tr>
      <td><button class="btn btn-ghost view-btn" data-id="${p.id}" style="font-weight:600;padding:0;text-align:left">${escHtml(p.name)}</button></td>
      <td style="color:var(--text-muted)">${escHtml(p.folder || '—')}</td>
      <td style="color:var(--text-muted)">${escHtml(p.description || '—')}</td>
      <td style="white-space:nowrap;text-align:right">
        <button class="btn btn-ghost edit-project-btn" data-id="${p.id}" title="Edit">✎</button>
        <button class="btn btn-ghost copy-project-btn" data-id="${p.id}" title="Duplicate">⧉</button>
        <button class="btn btn-ghost archive-project-btn" data-id="${p.id}" title="Mark as done / archive" style="color:var(--text-muted)">✓</button>
        <button class="btn btn-danger delete-project-btn" data-id="${p.id}">✕</button>
      </td>
    </tr>`;
  }).join('');

  el.innerHTML = `
    <h1>Projects</h1>

    <form id="create-project-form" class="form-row" style="margin-bottom:1.5rem">
      <div><label>Name</label><input name="name" required placeholder="Project name" style="width:200px"></div>
      <div><label>Folder</label><input name="folder" placeholder="Optional folder" style="width:160px"></div>
      <div><label>Description</label><input name="description" placeholder="Optional" style="width:260px"></div>
      <div style="align-self:flex-end">
        <button type="submit" class="btn btn-primary">+ New Project</button>
      </div>
    </form>

    ${projects.length === 0
      ? '<p style="color:var(--text-muted)">No projects yet — create one above.</p>'
      : `<table>
          <thead><tr>
            <th>Name</th><th>Folder</th><th>Description</th><th style="width:160px"></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`}
  `;

  el.querySelector('#create-project-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await api.post('/projects/', {
        name: fd.get('name'),
        folder: fd.get('folder') || null,
        description: fd.get('description') || null,
      });
      await showProjectsList(el);
    } catch (err) { alert(`Error: ${err.message}`); }
  });

  el.querySelectorAll('.view-btn').forEach(btn =>
    btn.addEventListener('click', () => showProjectDetail(el, parseInt(btn.dataset.id)))
  );

  el.querySelectorAll('.edit-project-btn').forEach(btn =>
    btn.addEventListener('click', () => showProjectsList(el, parseInt(btn.dataset.id)))
  );

  const editForm = el.querySelector('.edit-project-form');
  if (editForm) {
    editForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const id = editForm.closest('tr').dataset.id;
      try {
        await api.patch(`/projects/${id}`, {
          name: fd.get('name'),
          folder: fd.get('folder') || null,
          description: fd.get('description') || null,
        });
        await showProjectsList(el);
      } catch (err) { alert(`Error: ${err.message}`); }
    });
    el.querySelector('.cancel-project-edit').addEventListener('click', () => showProjectsList(el));
  }

  el.querySelectorAll('.copy-project-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      try {
        await api.post(`/projects/${btn.dataset.id}/copy`, {});
        await showProjectsList(el);
      } catch (err) { alert(`Error: ${err.message}`); }
    })
  );

  el.querySelectorAll('.archive-project-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      if (!confirm('Mark this project as done and move it to the archive?')) return;
      try {
        await api.post(`/projects/${btn.dataset.id}/archive`, {});
        await showProjectsList(el);
      } catch (err) { alert(`Error: ${err.message}`); }
    })
  );

  el.querySelectorAll('.delete-project-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      if (!confirm('Delete this project and all its tasks?')) return;
      try {
        await api.delete(`/projects/${btn.dataset.id}`);
        await showProjectsList(el);
      } catch (err) { alert(`Error: ${err.message}`); }
    })
  );
}

async function showProjectDetail(el, projectId, editingTaskId = null) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const [project, tasks, resources] = await Promise.all([
    api.get(`/projects/${projectId}`),
    api.get(`/tasks/?project_id=${projectId}`),
    api.get('/resources/'),
  ]);

  const resourceById = Object.fromEntries(resources.map(r => [r.id, r]));
  const taskById     = Object.fromEntries(tasks.map(t => [t.id, t]));

  // Dependency dropdown — excludes the task being edited (selfId) to prevent self-reference
  const dependsOptions = (selectedId, selfId) => [
    `<option value=""${!selectedId ? ' selected' : ''}>— none —</option>`,
    ...tasks
      .filter(t => t.id !== selfId)
      .map(t =>
        `<option value="${t.id}"${t.id === selectedId ? ' selected' : ''}>${escHtml(t.title)}</option>`
      ),
  ].join('');

  const resourceOptions = (selectedId) => [
    `<option value=""${!selectedId ? ' selected' : ''}>— unassigned —</option>`,
    ...resources.map(r =>
      `<option value="${r.id}"${r.id === selectedId ? ' selected' : ''}>${escHtml(r.name)} (${escHtml(r.kind)})</option>`
    ),
  ].join('');

  const statusOptions = (selected) => ['todo', 'in_progress', 'blocked', 'done']
    .map(s => `<option value="${s}"${s === selected ? ' selected' : ''}>${s.replace('_', ' ')}</option>`)
    .join('');

  const taskRows = tasks.map(t => {
    const res = resourceById[t.resource_id];
    if (t.id === editingTaskId) {
      return `<tr class="edit-row" data-id="${t.id}">
        <td colspan="9">
          <form class="form-row edit-task-form" style="flex-wrap:wrap;gap:0.5rem;padding:0.25rem 0">
            <div><label>Title</label><input name="title" value="${escHtml(t.title)}" required style="width:160px"></div>
            <div><label>Description / Command</label><input name="description" value="${escHtml(t.description || '')}" style="width:240px" placeholder="Optional description or shell command"></div>
            <div><label>Resource</label><select name="resource_id">${resourceOptions(t.resource_id)}</select></div>
            <div><label>Depends on</label><select name="depends_on_id">${dependsOptions(t.depends_on_id, t.id)}</select></div>
            <div><label>Start</label><input type="datetime-local" name="start_date" value="${dtLocal(t.start_date)}"></div>
            <div><label>End (auto)</label><input type="datetime-local" name="end_date" value="${dtLocal(t.end_date)}" readonly></div>
            <div><label>Load</label><input type="number" name="load" value="${t.load}" min="0.01" step="any" style="width:70px"></div>
            <div><label>Duration (h)</label><input type="number" name="duration" value="${t.duration != null ? t.duration : ''}" min="0.01" step="any" style="width:70px" placeholder="—"></div>
            <div><label>Status</label><select name="status">${statusOptions(t.status)}</select></div>
            <div style="align-self:flex-end;display:flex;gap:0.25rem">
              <button type="submit" class="btn btn-primary">Save</button>
              <button type="button" class="btn btn-ghost cancel-task-edit">Cancel</button>
            </div>
          </form>
        </td>
      </tr>`;
    }
    return `<tr>
      <td>
        <button class="btn btn-ghost edit-task-btn" data-id="${t.id}" style="padding:0;text-align:left">${escHtml(t.title)}</button>
        ${t.description ? `<div style="font-size:0.75rem;color:var(--text-muted);margin-top:1px;font-family:monospace">${escHtml(t.description)}</div>` : ''}
      </td>
      <td>
        <select class="status-select" data-id="${t.id}" style="font-size:0.8rem">
          ${statusOptions(t.status)}
        </select>
      </td>
      <td>${res ? escHtml(res.name) : '<span style="color:var(--text-muted)">—</span>'}</td>
      <td style="color:var(--text-muted);font-size:0.8rem">${t.depends_on_id && taskById[t.depends_on_id] ? '↳ ' + escHtml(taskById[t.depends_on_id].title) : '—'}</td>
      <td style="font-size:0.8rem">${fmtDt(t.start_date)}</td>
      <td style="font-size:0.8rem">${fmtDt(t.end_date)}</td>
      <td>${t.load}</td>
      <td>${t.duration != null ? t.duration + 'h' : '—'}</td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn btn-ghost edit-task-btn" data-id="${t.id}" title="Edit">✎</button>
        <button class="btn btn-ghost copy-task-btn" data-id="${t.id}" title="Copy">⧉</button>
        <button class="btn btn-danger delete-task-btn" data-id="${t.id}">✕</button>
      </td>
    </tr>`;
  }).join('');

  el.innerHTML = `
    <div style="margin-bottom:1rem">
      <button class="btn btn-ghost back-btn">← Projects</button>
    </div>
    <h1>${escHtml(project.name)}</h1>
    ${project.folder ? `<p style="color:var(--text-muted);margin-bottom:0.25rem"><strong>Folder:</strong> ${escHtml(project.folder)}</p>` : ''}
    ${project.description ? `<p style="color:var(--text-muted);margin-bottom:1rem">${escHtml(project.description)}</p>` : ''}

    <h2 style="margin-top:1.5rem;margin-bottom:0.75rem">Add task</h2>
    <form id="add-task-form" class="form-row" style="margin-bottom:1.5rem;flex-wrap:wrap">
      <div><label>Title *</label><input name="title" required placeholder="Task title" style="width:180px"></div>
      <div><label>Description / Command</label><input name="description" placeholder="Optional description or shell command" style="width:240px"></div>
      <div><label>Resource</label><select name="resource_id">${resourceOptions(null)}</select></div>
      <div><label>Depends on</label><select name="depends_on_id">${dependsOptions(null, null)}</select></div>
      <div><label>Start</label><input type="datetime-local" name="start_date"></div>
      <div><label>End (auto)</label><input type="datetime-local" name="end_date" readonly></div>
      <div><label>Load</label><input type="number" name="load" value="1" min="0.01" step="any" style="width:70px"></div>
      <div><label>Duration (h)</label><input type="number" name="duration" min="0.01" step="any" style="width:70px" placeholder="—"></div>
      <div><label>Status</label><select name="status">${statusOptions('todo')}</select></div>
      <div style="align-self:flex-end"><button type="submit" class="btn btn-primary">Add task</button></div>
    </form>

    <h2>Tasks (${tasks.length})</h2>
    ${tasks.length === 0
      ? '<p style="color:var(--text-muted)">No tasks yet — add one above.</p>'
      : `<table>
          <thead><tr>
            <th>Title</th><th>Status</th><th>Resource</th><th>Depends on</th>
            <th>Start</th><th>End</th><th>Load</th><th>Duration</th><th style="width:100px"></th>
          </tr></thead>
          <tbody>${taskRows}</tbody>
        </table>`}
  `;

  el.querySelector('.back-btn').addEventListener('click', () => showProjectsList(el));

  const addForm = el.querySelector('#add-task-form');
  setupAutoCalcEnd(addForm);
  setupResourceAutoStart(addForm);
  setupDependencyAutoStart(addForm, taskById);

  el.querySelector('#add-task-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const ridRaw = fd.get('resource_id');
    const durRaw = fd.get('duration');
    try {
      const depRaw = fd.get('depends_on_id');
      await api.post('/tasks/', {
        title: fd.get('title'),
        description: fd.get('description') || null,
        project_id: projectId,
        resource_id: ridRaw ? parseInt(ridRaw) : null,
        depends_on_id: depRaw ? parseInt(depRaw) : null,
        start_date: fd.get('start_date') || null,
        end_date: fd.get('end_date') || null,
        load: parseFloat(fd.get('load')),
        duration: durRaw ? parseFloat(durRaw) : null,
        status: fd.get('status'),
      });
      await showProjectDetail(el, projectId);
    } catch (err) { alert(`Error: ${err.message}`); }
  });

  el.querySelectorAll('.edit-task-btn').forEach(btn =>
    btn.addEventListener('click', () => showProjectDetail(el, projectId, parseInt(btn.dataset.id)))
  );

  const editTaskForm = el.querySelector('.edit-task-form');
  if (editTaskForm) {
    setupAutoCalcEnd(editTaskForm);
    setupDependencyAutoStart(editTaskForm, taskById);
    const editRow = editTaskForm.closest('tr');
    editTaskForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const ridRaw = fd.get('resource_id');
      const durRaw = fd.get('duration');
      try {
        const depRaw2 = fd.get('depends_on_id');
        await api.patch(`/tasks/${editRow.dataset.id}`, {
          title: fd.get('title'),
          description: fd.get('description') || null,
          resource_id: ridRaw ? parseInt(ridRaw) : null,
          depends_on_id: depRaw2 ? parseInt(depRaw2) : null,
          start_date: fd.get('start_date') || null,
          end_date: fd.get('end_date') || null,
          load: parseFloat(fd.get('load')),
          duration: durRaw ? parseFloat(durRaw) : null,
          status: fd.get('status'),
        });
        await showProjectDetail(el, projectId);
      } catch (err) { alert(`Error: ${err.message}`); }
    });
    el.querySelector('.cancel-task-edit').addEventListener('click',
      () => showProjectDetail(el, projectId)
    );
  }

  el.querySelectorAll('.copy-task-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      try {
        await api.post(`/tasks/${btn.dataset.id}/copy`, {});
        await showProjectDetail(el, projectId);
      } catch (err) { alert(`Error: ${err.message}`); }
    })
  );

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
      } catch (err) { alert(`Error: ${err.message}`); }
    })
  );
}

registerView('/projects', async (el) => {
  await showProjectsList(el);
});
