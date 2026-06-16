// ── Task board view ────────────────────────────────────────────────────────

const STATUS_ORDER = ['todo', 'in_progress', 'blocked', 'done', 'failed'];

function taskSortKey(t) {
  // Scheduled tasks sorted by start_date; unscheduled float to the bottom.
  return t.start_date ? new Date(t.start_date).getTime() : Infinity;
}

async function showTaskBoard(el, showCompleted = false, resourceFilter = null) {
  el.innerHTML = '<p class="loading">Loading…</p>';

  const [tasks, projects, resources] = await Promise.all([
    api.get('/tasks/'),
    api.get('/projects/'),
    api.get('/resources/'),
  ]);

  const byProject         = Object.fromEntries(projects.map(p => [p.id, p.name]));
  const priorityByProject = Object.fromEntries(projects.map(p => [p.id, p.priority || 3]));
  const byResource        = Object.fromEntries(resources.map(r => [r.id, r.name]));
  const taskById          = Object.fromEntries(tasks.map(t => [t.id, t]));

  // Optional resource scope (from the filter dropdown or a deep link)
  const scoped = resourceFilter
    ? tasks.filter(t => (t.resource_ids || []).includes(resourceFilter))
    : tasks;
  const visible = showCompleted ? scoped : scoped.filter(t => t.status !== 'done');
  // Blocked tasks always go to the bottom; within groups sort by start_date then priority.
  visible.sort((a, b) => {
    const aBlocked = a.status === 'blocked' ? 1 : 0;
    const bBlocked = b.status === 'blocked' ? 1 : 0;
    if (aBlocked !== bBlocked) return aBlocked - bBlocked;
    return taskSortKey(a) - taskSortKey(b) ||
      (priorityByProject[a.project_id] || 3) - (priorityByProject[b.project_id] || 3);
  });

  const rows = visible.map(t => {
    const done = t.status === 'done';
    const rowStyle = done ? 'opacity:0.5;text-decoration:line-through' : '';
    const statusOpts = STATUS_ORDER.map(s =>
      `<option value="${s}"${t.status === s ? ' selected' : ''}>${escHtml(s.replace('_', ' '))}</option>`
    ).join('');

    const dep = t.depends_on_id ? taskById[t.depends_on_id] : null;
    const depHtml = t.dependency_broken
      ? `<div style="font-size:0.78em;color:var(--danger);margin-top:1px" title="This task's dependency was deleted — set a new one.">⚠ dependency deleted — update it</div>`
      : dep
      ? `<div style="font-size:0.78em;color:var(--text-muted);margin-top:1px">↳ ${escHtml(dep.title)}</div>`
      : '';

    const descSnippet = t.description
      ? ` <span style="font-size:0.78em;color:var(--text-muted)" title="${escHtml(t.description)}">${escHtml(t.description.length > 24 ? t.description.slice(0, 24) + '…' : t.description)}</span>`
      : '';

    const pinnedBtn = t.status === 'todo'
      ? `<button class="pin-btn btn btn-ghost" data-id="${t.id}" data-pinned="${t.pinned ? '1' : '0'}" title="${t.pinned ? 'Unpin (re-align will reschedule)' : 'Pin (preserve timing through re-align)'}" style="padding:0.1em 0.35em;font-size:0.9em;opacity:${t.pinned ? '1' : '0.35'}">${t.pinned ? '📌' : '📌'}</button>`
      : '';

    return `<tr style="${rowStyle}" data-id="${t.id}">
      <td>
        <select class="status-sel badge badge-${escHtml(t.status)}" data-id="${t.id}" style="cursor:pointer;border:none;background:transparent;font-size:0.8em;font-weight:600;padding:0.15em 0.3em;border-radius:3px">
          ${statusOpts}
        </select>
      </td>
      <td><button class="btn btn-ghost open-task-btn" data-id="${t.id}" data-project-id="${t.project_id}" style="padding:0;text-align:left;font-weight:inherit;font-size:inherit">${escHtml(t.title)}</button>${descSnippet}${depHtml}</td>
      <td style="color:var(--text-muted);font-size:0.9em">${priorityBadge(priorityByProject[t.project_id])}${escHtml(byProject[t.project_id] || '—')}</td>
      <td style="color:var(--text-muted);font-size:0.9em">${escHtml((t.resource_ids || []).map(id => byResource[id]).filter(Boolean).join(', ') || '—')}</td>
      <td style="font-size:0.85em;white-space:nowrap">${fmtDt(t.start_date)}</td>
      <td style="font-size:0.85em;white-space:nowrap">${fmtDt(t.end_date)}</td>
      <td style="font-size:0.85em">${t.duration != null ? t.duration + 'h' : '—'}</td>
      <td style="text-align:center">${pinnedBtn}</td>
    </tr>`;
  }).join('');

  const showDoneChecked = showCompleted ? 'checked' : '';
  const totalCount = scoped.length;
  const doneCount  = scoped.filter(t => t.status === 'done').length;

  const resourceOpts = resources.map(r =>
    `<option value="${r.id}"${r.id === resourceFilter ? ' selected' : ''}>${escHtml(r.name)}</option>`
  ).join('');

  el.innerHTML = `
    <div style="display:flex;align-items:baseline;gap:1.5rem;margin-bottom:1rem;flex-wrap:wrap">
      <h1 style="margin:0">Tasks</h1>
      <span style="color:var(--text-muted);font-size:0.9em">${doneCount} / ${totalCount} done</span>
      <button id="btn-reflow" class="btn btn-ghost" style="font-size:0.85em">↺ Re-flow</button>
      <label style="font-size:0.9em">
        Resource
        <select id="filter-resource" style="font-size:0.95em">
          <option value="">All</option>
          ${resourceOpts}
        </select>
      </label>
      <label style="font-size:0.9em;margin-left:auto">
        <input type="checkbox" id="show-done" ${showDoneChecked}> Show completed
      </label>
    </div>
    ${visible.length === 0
      ? '<p style="color:var(--text-muted)">No tasks.</p>'
      : `<table>
          <thead><tr>
            <th style="width:120px">Status</th>
            <th>Task</th>
            <th>Project</th>
            <th>Resource</th>
            <th>Start</th>
            <th>End</th>
            <th>Duration</th>
            <th style="width:40px"></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`}
  `;

  el.querySelector('#show-done')?.addEventListener('change', e => {
    showTaskBoard(el, e.target.checked, resourceFilter);
  });

  el.querySelector('#filter-resource')?.addEventListener('change', e => {
    showTaskBoard(el, showCompleted, parseInt(e.target.value) || null);
  });

  el.querySelector('#btn-reflow')?.addEventListener('click', async () => {
    const btn = el.querySelector('#btn-reflow');
    btn.disabled = true;
    btn.textContent = 'Re-flowing…';
    try {
      const result = await reflowSchedule();
      const hasUnscheduled = result.unscheduled && result.unscheduled.length;
      if (result.changed === 0 && !hasUnscheduled) {
        alert('Tasks are already in priority order — no changes needed.');
        btn.disabled = false;
        btn.textContent = '↺ Re-flow';
      } else {
        if (hasUnscheduled) alert(reflowSummary(result));
        await showTaskBoard(el, showCompleted, resourceFilter);
      }
    } catch (err) {
      alert(`Re-flow failed: ${err.message}`);
      btn.disabled = false;
      btn.textContent = '↺ Re-flow';
    }
  });

  el.querySelectorAll('.open-task-btn').forEach(btn => {
    btn.addEventListener('click', () =>
      showProjectDetail(el, parseInt(btn.dataset.projectId), parseInt(btn.dataset.id))
    );
  });

  el.querySelectorAll('.status-sel').forEach(sel => {
    sel.addEventListener('change', async () => {
      const patch = { status: sel.value };
      if (sel.value === 'in_progress') {
        patch.start_date = nowIsoStr();
      } else if (sel.value === 'done') {
        patch.end_date = nowIsoStr();
        const task = tasks.find(t => t.id === parseInt(sel.dataset.id));
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
        await showTaskBoard(el, showCompleted, resourceFilter);
      } catch (err) { alert(`Error: ${err.message}`); }
    });
  });

  el.querySelectorAll('.pin-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        const nowPinned = btn.dataset.pinned === '1';
        await api.patch(`/tasks/${btn.dataset.id}`, { pinned: !nowPinned });
        await showTaskBoard(el, showCompleted, resourceFilter);
      } catch (err) { alert(`Error: ${err.message}`); }
    });
  });
}

registerView('/tasks', async (el, params = {}) => {
  if (params.id) {
    try {
      const task = await api.get(`/tasks/${params.id}`);
      await showProjectDetail(el, task.project_id, task.id);
    } catch (_) {
      await showTaskBoard(el, false);
    }
  } else {
    const rid = params.resource ? parseInt(params.resource) : null;
    await showTaskBoard(el, false, rid);
  }
});
