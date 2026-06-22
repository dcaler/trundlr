// ── Projects view ─────────────────────────────────────────────────────────

const PRIORITY_LABELS = { 1: 'P1 – Critical', 2: 'P2 – High', 3: 'P3 – Medium', 4: 'P4 – Low' };
const PRIORITY_KEYS   = [1, 2, 3, 4];

function priorityBadge(p) {
  if (!p) return '';
  return `<span class="badge priority-p${p}" style="font-size:0.7em;margin-right:0.3em">P${p}</span>`;
}

function prioritySelect(selected = 3) {
  return `<select name="priority">
    ${PRIORITY_KEYS.map(k =>
      `<option value="${k}"${k === selected ? ' selected' : ''}>${escHtml(PRIORITY_LABELS[k])}</option>`
    ).join('')}
  </select>`;
}

// Current wall-clock time in the app's configured timezone, as a naive ISO
// string "YYYY-MM-DDTHH:MM:00". Stored datetimes are entered and interpreted
// in appSettings.timezone (see Settings + the iCal/CalDAV feeds), so "now"
// must be computed in that zone too — stamping it in UTC (or raw browser
// local time) shifts every auto-recorded start/end by the tz offset.
function nowIsoStr() {
  const tz = (typeof appSettings !== 'undefined' && appSettings.timezone) || 'UTC';
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz, hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  }).formatToParts(new Date()).reduce((o, p) => (o[p.type] = p.value, o), {});
  const hour = parts.hour === '24' ? '00' : parts.hour;  // some engines emit "24" at midnight
  return `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}:00`;
}

// Format ISO datetime string for display: "2025-06-01T09:00:00" → "2025-06-01 09:00"
function fmtDt(iso) {
  if (!iso) return '—';
  return iso.replace('T', ' ').slice(0, 16);
}

// Split ISO datetime for separate date + time text inputs (always 24h)
function dtDate(iso) { return iso ? iso.slice(0, 10) : ''; }
function dtTime(iso) { return iso ? iso.replace('T', ' ').slice(11, 16) : ''; }

// Wire up start_date + duration → auto-fill end_date (readonly) on a task form.
function setupAutoCalcEnd(form) {
  const startDateEl = form.querySelector('[name="start_date"]');
  const startTimeEl = form.querySelector('[name="start_time"]');
  const durEl       = form.querySelector('[name="duration"]');
  const endDateEl   = form.querySelector('[name="end_date"]');
  const endTimeEl   = form.querySelector('[name="end_time"]');
  if (!startDateEl || !durEl || !endDateEl) return;
  function calc() {
    const dateVal = startDateEl.value;
    const timeVal = startTimeEl?.value || '00:00';
    if (!dateVal || !durEl.value) {
      endDateEl.value = '';
      if (endTimeEl) endTimeEl.value = '';
      return;
    }
    const ms = new Date(`${dateVal}T${timeVal}`).getTime() + parseFloat(durEl.value) * 3_600_000;
    if (!isNaN(ms)) {
      const d = new Date(ms);
      const p = n => String(n).padStart(2, '0');
      endDateEl.value = `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;
      if (endTimeEl) endTimeEl.value = `${p(d.getHours())}:${p(d.getMinutes())}`;
    }
  }
  startDateEl.addEventListener('change', calc);
  if (startTimeEl) startTimeEl.addEventListener('change', calc);
  durEl.addEventListener('change', calc);
  calc();
}

// When resources are selected in a task form, fill start_date from the first resource's next slot.
function setupResourceAutoStart(form, resourceById = {}) {
  const startDateEl = form.querySelector('[name="start_date"]');
  const startTimeEl = form.querySelector('[name="start_time"]');
  if (!startDateEl) return;

  async function refreshStart() {
    const checked = [...form.querySelectorAll('[name="resource_ids"]:checked')];
    if (!checked.length) return;

    // All datetime strings are naive wall-clock in the configured app timezone.
    // Compare as strings — ISO format sorts lexicographically correctly.
    let best = nowIsoStr();
    try {
      const results = await Promise.all(
        checked.map(cb => api.get(`/resources/${cb.value}/next-available`))
      );
      for (const data of results) {
        if (data.next_available) {
          const na = data.next_available.replace(' ', 'T');
          if (na > best) best = na;
        }
      }
    } catch (_) {}

    startDateEl.value = best.slice(0, 10);
    if (startTimeEl) startTimeEl.value = best.slice(11, 16);
    startDateEl.dispatchEvent(new Event('change'));
  }

  form.querySelectorAll('[name="resource_ids"]').forEach(cb => {
    cb.addEventListener('change', refreshStart);
  });
}

// When a dependency task is selected, fill start_date from that task's end (or start).
function setupDependencyAutoStart(form, taskById) {
  const depEl       = form.querySelector('[name="depends_on_id"]');
  const startDateEl = form.querySelector('[name="start_date"]');
  const startTimeEl = form.querySelector('[name="start_time"]');
  if (!depEl || !startDateEl) return;
  depEl.addEventListener('change', () => {
    const depId = parseInt(depEl.value);
    if (!depId) return;
    const dep = taskById[depId];
    if (!dep) return;
    const anchor = dep.end_date || dep.start_date;
    if (anchor) {
      const anchorStr = anchor.replace(' ', 'T');
      const best = anchorStr > nowIsoStr() ? anchorStr : nowIsoStr();
      startDateEl.value = best.slice(0, 10);
      if (startTimeEl) startTimeEl.value = best.slice(11, 16);
      startDateEl.dispatchEvent(new Event('change'));
    }
  });
}

async function showProjectsList(el, editingId = null) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const projects = await api.get('/projects/');

  const rows = projects.map(p => {
    if (p.id === editingId) {
      return `<tr class="edit-row" data-id="${p.id}">
        <td colspan="3">
          <form class="form-row edit-project-form" style="flex-wrap:wrap;gap:0.5rem;padding:0.25rem 0">
            <div><label>Name</label><input name="name" value="${escHtml(p.name)}" required style="width:180px"></div>
            <div><label>Directory</label><input name="folder" value="${escHtml(p.folder || '')}" placeholder="/path/on/runner" style="width:200px"></div>
            <div><label>Description</label><input name="description" value="${escHtml(p.description || '')}" style="width:220px"></div>
            <div><label>Priority</label>${prioritySelect(p.priority || 3)}</div>
            <div style="align-self:flex-end;display:flex;gap:0.25rem">
              <button type="submit" class="btn btn-primary">Save</button>
              <button type="button" class="btn btn-ghost cancel-project-edit">Cancel</button>
            </div>
          </form>
        </td>
      </tr>`;
    }
    return `<tr>
      <td><button class="btn btn-ghost view-btn" data-id="${p.id}" style="font-weight:600;padding:0;text-align:left">${priorityBadge(p.priority)}${escHtml(p.name)}</button></td>
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
      <div><label>Directory</label><input name="folder" placeholder="/path/on/runner" style="width:200px"></div>
      <div><label>Description</label><input name="description" placeholder="Optional" style="width:260px"></div>
      <div><label>Priority</label>${prioritySelect(3)}</div>
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
        priority: parseInt(fd.get('priority')) || 3,
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
          priority: parseInt(fd.get('priority')) || 3,
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

async function showProjectDetail(el, projectId, editingTaskId = null, scrollY = null) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const [project, allTasks, resources, allProjects, cycleTemplates] = await Promise.all([
    api.get(`/projects/${projectId}`),
    api.get('/tasks/'),
    api.get('/resources/'),
    api.get('/projects/'),
    api.get('/cycle-templates/'),
  ]);
  // tasks = only this project's tasks (for the task list); allTasks used for dependency lookup
  const tasks = allTasks.filter(t => t.project_id === projectId);

  const resourceById = Object.fromEntries(resources.map(r => [r.id, r]));
  const taskById     = Object.fromEntries(allTasks.map(t => [t.id, t]));
  const projById     = Object.fromEntries(allProjects.map(p => [p.id, p]));

  // Dependency dropdown grouped by project — excludes selfId to prevent self-reference.
  // Current project appears first, then other projects alphabetically.
  const dependsOptions = (selectedId, selfId) => {
    const grouped = {};
    for (const t of allTasks) {
      if (t.id === selfId) continue;
      (grouped[t.project_id] = grouped[t.project_id] || []).push(t);
    }
    const makeOptions = ts => ts.map(t =>
      `<option value="${t.id}"${t.id === selectedId ? ' selected' : ''}>${escHtml(t.title)}</option>`
    ).join('');
    const groups = [
      ...(grouped[projectId]
        ? [`<optgroup label="${escHtml(projById[projectId]?.name || 'This project')} (this project)">${makeOptions(grouped[projectId])}</optgroup>`]
        : []),
      ...allProjects
        .filter(p => p.id !== projectId && grouped[p.id])
        .sort((a, b) => a.name.localeCompare(b.name))
        .map(p => `<optgroup label="${escHtml(p.name)}">${makeOptions(grouped[p.id])}</optgroup>`),
    ].join('');
    return `<option value=""${!selectedId ? ' selected' : ''}>— none —</option>${groups}`;
  };

  const resourceCheckboxes = (selectedIds = []) =>
    resources.length === 0
      ? '<span style="color:var(--text-muted);font-size:0.85em">No resources</span>'
      : resources.map(r =>
          `<label style="display:flex;align-items:center;gap:0.25rem;white-space:nowrap;font-size:0.85em">
            <input type="checkbox" name="resource_ids" value="${r.id}"${selectedIds.includes(r.id) ? ' checked' : ''}>
            ${escHtml(r.name)} <span style="color:var(--text-muted)">(${escHtml(r.kind)})</span>
          </label>`
        ).join('');

  const statusOptions = (selected) => ['todo', 'in_progress', 'blocked', 'done', 'failed']
    .map(s => `<option value="${s}"${s === selected ? ' selected' : ''}>${s.replace('_', ' ')}</option>`)
    .join('');

  const taskRows = tasks.map(t => {
    const resNames = (t.resource_ids || []).map(id => resourceById[id]?.name).filter(Boolean).join(', ');
    if (t.id === editingTaskId) {
      return `<tr class="edit-row" data-id="${t.id}">
        <td colspan="9">
          <form class="form-row edit-task-form" style="flex-wrap:wrap;gap:0.5rem;padding:0.25rem 0">
            <div><label>ID</label><input value="${t.id}" readonly style="width:60px;color:var(--text-muted)"></div>
            <div><label>Title</label><input name="title" value="${escHtml(t.title)}" required style="width:160px"></div>
            <div><label>Description</label><input name="description" value="${escHtml(t.description || '')}" style="width:240px" placeholder="Optional description"></div>
            <div><label>Command</label><input name="command" value="${escHtml(t.command || '')}" style="width:280px;font-family:monospace" placeholder="shell command (cpu/gpu tasks)"></div>
            <div><label>Resources</label>${resourceCheckboxes(t.resource_ids || [])}</div>
            <div><label>Depends on</label><select name="depends_on_id">${dependsOptions(t.depends_on_id, t.id)}</select></div>
            <div><label>Start</label>
              <span style="display:flex;gap:0.25rem">
                <input type="date" name="start_date" value="${dtDate(t.start_date)}" style="width:130px">
                <input type="text" name="start_time" value="${dtTime(t.start_date)}" placeholder="HH:MM" maxlength="5" style="width:65px">
              </span>
            </div>
            <div><label>End (auto)</label>
              <span style="display:flex;gap:0.25rem">
                <input type="date" name="end_date" value="${dtDate(t.end_date)}" readonly style="width:130px">
                <input type="text" name="end_time" value="${dtTime(t.end_date)}" placeholder="HH:MM" maxlength="5" readonly style="width:65px">
              </span>
            </div>
            <div><label>Duration (h)</label><input type="number" name="duration" value="${t.duration != null ? t.duration : ''}" min="0.01" step="any" style="width:70px" placeholder="—"></div>
            <div><label>Status</label><select name="status">${statusOptions(t.status)}</select></div>
            ${t.exit_code != null ? `<div><label>Exit code</label><input value="${escHtml(String(t.exit_code))}" readonly style="width:70px"></div>` : ''}
            <div><label>Move to project</label><select name="project_id">
              ${allProjects.map(p => `<option value="${p.id}"${p.id === t.project_id ? ' selected' : ''}>${escHtml(p.name)}</option>`).join('')}
            </select></div>
            <div style="align-self:flex-end;display:flex;gap:0.25rem">
              <button type="submit" class="btn btn-primary">Save</button>
              <button type="button" class="btn btn-ghost cancel-task-edit">Cancel</button>
            </div>
            ${t.log_tail ? `<div style="width:100%;margin-top:0.5rem"><label>Log output</label><textarea readonly style="width:100%;height:10rem;font-family:monospace;font-size:0.75rem;white-space:pre">${escHtml(t.log_tail)}</textarea></div>` : ''}
          </form>
        </td>
      </tr>`;
    }
    return `<tr>
      <td>
        <button class="btn btn-ghost edit-task-btn" data-id="${t.id}" style="padding:0;text-align:left">${escHtml(t.title)}</button>
        ${t.description ? `<div style="font-size:0.75rem;color:var(--text-muted);margin-top:1px">${escHtml(t.description)}</div>` : ''}
        ${t.command ? `<div style="font-size:0.75rem;color:var(--text-muted);margin-top:1px;font-family:monospace;max-width:60ch;white-space:pre-wrap;overflow-wrap:anywhere">$ ${escHtml(t.command)}</div>` : ''}
      </td>
      <td>
        <select class="status-select" data-id="${t.id}" style="font-size:0.8rem">
          ${statusOptions(t.status)}
        </select>
        ${t.exit_code != null ? `<div style="font-size:0.7rem;color:var(--text-muted)">exit: ${escHtml(String(t.exit_code))}</div>` : ''}
      </td>
      <td>${resNames || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td style="font-size:0.8rem">${t.dependency_broken
        ? `<span style="color:var(--danger)" title="This task's dependency was deleted — set a new one.">⚠ deleted — update</span>`
        : t.depends_on_id && taskById[t.depends_on_id]
        ? (() => { const dep = taskById[t.depends_on_id]; const met = dep.status === 'done';
            return `<span style="color:var(--text-muted)"><span style="color:${met ? 'var(--success,#4caf50)' : 'var(--danger)'}" title="${met ? 'Dependency met' : 'Dependency not yet done'}">${met ? '✓' : '✗'}</span> ${escHtml(dep.title)}</span>`; })()
        : '<span style="color:var(--text-muted)">—</span>'}</td>
      <td style="font-size:0.8rem">${fmtDt(t.start_date)}</td>
      <td style="font-size:0.8rem">${fmtDt(t.end_date)}</td>
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
    <h1>${priorityBadge(project.priority)}${escHtml(project.name)}</h1>
    ${project.folder ? `<p style="color:var(--text-muted);margin-bottom:0.25rem"><strong>Directory:</strong> <code>${escHtml(project.folder)}</code></p>` : ''}
    ${project.description ? `<p style="color:var(--text-muted);margin-bottom:1rem">${escHtml(project.description)}</p>` : ''}

    ${cycleTemplates.length ? `
    <form id="add-cycle-form" class="form-row" style="margin-top:1.5rem;align-items:flex-end;gap:0.5rem">
      <div>
        <label>Add cycle</label>
        <select name="template_id" style="width:200px">
          ${cycleTemplates.map(t => `<option value="${t.id}">${escHtml(t.name)} (${t.steps.length} step${t.steps.length === 1 ? '' : 's'})</option>`).join('')}
        </select>
      </div>
      <div><button type="submit" class="btn btn-primary">Add cycle</button></div>
      <span style="font-size:0.85em;color:var(--text-muted)">Creates a numbered, chained batch of tasks.</span>
    </form>` : ''}

    <div style="margin-top:1.5rem;display:flex;gap:0.5rem;align-items:center">
      <button id="quick-bug-btn" class="btn btn-ghost">+ Bug Fix</button>
      <button id="quick-feature-btn" class="btn btn-ghost">+ Add Feature</button>
      <span style="font-size:0.85em;color:var(--text-muted)">Creates a 1h task and opens it for editing.</span>
    </div>

    <h2 style="margin-top:1.5rem;margin-bottom:0.75rem">Add task</h2>
    <form id="add-task-form" class="form-row" style="margin-bottom:1.5rem;flex-wrap:wrap">
      <div><label>Title *</label><input name="title" required placeholder="Task title" style="width:180px"></div>
      <div><label>Description</label><input name="description" placeholder="Optional description" style="width:240px"></div>
      <div><label>Command</label><input name="command" placeholder="shell command (cpu/gpu tasks)" style="width:280px;font-family:monospace"></div>
      <div><label>Resources</label>${resourceCheckboxes([])}</div>
      <div><label>Depends on</label><select name="depends_on_id">${dependsOptions(null, null)}</select></div>
      <div><label>Start</label>
        <span style="display:flex;gap:0.25rem">
          <input type="date" name="start_date" style="width:130px">
          <input type="text" name="start_time" placeholder="HH:MM" maxlength="5" style="width:65px">
        </span>
      </div>
      <div><label>End (auto)</label>
        <span style="display:flex;gap:0.25rem">
          <input type="date" name="end_date" readonly style="width:130px">
          <input type="text" name="end_time" placeholder="HH:MM" maxlength="5" readonly style="width:65px">
        </span>
      </div>
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
            <th>Start</th><th>End</th><th>Duration</th><th style="width:100px"></th>
          </tr></thead>
          <tbody>${taskRows}</tbody>
        </table>`}
  `;

  if (scrollY !== null) {
    requestAnimationFrame(() => window.scrollTo({ top: scrollY, behavior: 'instant' }));
  } else if (editingTaskId !== null) {
    requestAnimationFrame(() => {
      const row = el.querySelector(`.edit-row[data-id="${editingTaskId}"]`);
      if (row) row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }

  el.querySelector('.back-btn').addEventListener('click', () => showProjectsList(el));

  const cycleForm = el.querySelector('#add-cycle-form');
  if (cycleForm) {
    cycleForm.addEventListener('submit', async e => {
      e.preventDefault();
      const templateId = parseInt(new FormData(e.target).get('template_id'));
      try {
        await api.post(`/cycle-templates/${templateId}/instantiate`, { project_id: projectId });
        await showProjectDetail(el, projectId);
      } catch (err) { alert(`Error: ${err.message}`); }
    });
  }

  const quickAdd = async (title) => {
    try {
      const task = await api.post('/tasks/', { title, project_id: projectId, duration: 1, status: 'todo' });
      await showProjectDetail(el, projectId, task.id);
    } catch (err) { alert(`Error: ${err.message}`); }
  };
  el.querySelector('#quick-bug-btn')?.addEventListener('click', () => quickAdd('Bug Fix'));
  el.querySelector('#quick-feature-btn')?.addEventListener('click', () => quickAdd('Add Feature'));

  const addForm = el.querySelector('#add-task-form');
  setupAutoCalcEnd(addForm);
  setupResourceAutoStart(addForm, resourceById);
  setupDependencyAutoStart(addForm, taskById);

  el.querySelector('#add-task-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const durRaw = fd.get('duration');
    try {
      const depRaw = fd.get('depends_on_id');
      await api.post('/tasks/', {
        title: fd.get('title'),
        description: fd.get('description') || null,
        command: fd.get('command') || null,
        project_id: projectId,
        resource_ids: fd.getAll('resource_ids').map(v => parseInt(v)),
        depends_on_id: depRaw ? parseInt(depRaw) : null,
        start_date: fd.get('start_date') ? `${fd.get('start_date')}T${fd.get('start_time') || '00:00'}` : null,
        end_date: fd.get('end_date') ? `${fd.get('end_date')}T${fd.get('end_time') || '00:00'}` : null,
        duration: durRaw ? parseFloat(durRaw) : null,
        status: fd.get('status'),
      });
      await showProjectDetail(el, projectId);
    } catch (err) { alert(`Error: ${err.message}`); }
  });

  el.querySelectorAll('.edit-task-btn').forEach(btn =>
    btn.addEventListener('click', () => showProjectDetail(el, projectId, parseInt(btn.dataset.id), window.scrollY))
  );

  const editTaskForm = el.querySelector('.edit-task-form');
  if (editTaskForm) {
    setupAutoCalcEnd(editTaskForm);
    setupDependencyAutoStart(editTaskForm, taskById);
    const editRow = editTaskForm.closest('tr');
    editTaskForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const durRaw = fd.get('duration');
      try {
        const depRaw2 = fd.get('depends_on_id');
        const newProjectId = parseInt(fd.get('project_id'));
        await api.patch(`/tasks/${editRow.dataset.id}`, {
          title: fd.get('title'),
          description: fd.get('description') || null,
          command: fd.get('command') || null,
          resource_ids: fd.getAll('resource_ids').map(v => parseInt(v)),
          depends_on_id: depRaw2 ? parseInt(depRaw2) : null,
          start_date: fd.get('start_date') ? `${fd.get('start_date')}T${fd.get('start_time') || '00:00'}` : null,
          end_date: fd.get('end_date') ? `${fd.get('end_date')}T${fd.get('end_time') || '00:00'}` : null,
          duration: durRaw ? parseFloat(durRaw) : null,
          status: fd.get('status'),
          project_id: newProjectId,
        });
        // If task moved to a different project, navigate there so it's visible
        const targetProjectId = newProjectId !== projectId ? newProjectId : projectId;
        await showProjectDetail(el, targetProjectId);
      } catch (err) { alert(`Error: ${err.message}`); }
    });
    el.querySelector('.cancel-task-edit').addEventListener('click',
      () => showProjectDetail(el, projectId, null, window.scrollY)
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
      const patch = { status: sel.value };
      if (sel.value === 'in_progress') {
        patch.start_date = nowIsoStr();
        const task = taskById[parseInt(sel.dataset.id)];
        if (task?.duration) {
          const endMs = Date.parse(patch.start_date + 'Z') + task.duration * 3600000;
          const d = new Date(endMs);
          const p = n => String(n).padStart(2, '0');
          patch.end_date = `${d.getUTCFullYear()}-${p(d.getUTCMonth()+1)}-${p(d.getUTCDate())}T${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:00`;
        } else if (task?.end_date && task.end_date < patch.start_date) {
          patch.end_date = null;
        }
      } else if (sel.value === 'done') {
        patch.end_date = nowIsoStr();
        const task = taskById[parseInt(sel.dataset.id)];
        if (task?.start_date) {
          // Both are naive wall-clock strings in the same (configured) zone;
          // parse both as UTC so the tz offset cancels and we get real elapsed hours.
          const startMs = Date.parse(task.start_date.replace(' ', 'T') + 'Z');
          const durH = (Date.parse(patch.end_date + 'Z') - startMs) / 3600000;
          if (durH > 0) patch.duration = Math.round(durH * 100) / 100;
        }
      }
      try {
        await api.patch(`/tasks/${sel.dataset.id}`, patch);
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

registerView('/projects', async (el, params = {}) => {
  if (params.id) await showProjectDetail(el, params.id);
  else            await showProjectsList(el);
});
