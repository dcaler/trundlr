// ── Task board view ────────────────────────────────────────────────────────

const STATUS_ORDER = ['todo', 'in_progress', 'blocked', 'done'];

function taskSortKey(t) {
  // Scheduled tasks sorted by start_date; unscheduled float to the bottom.
  return t.start_date ? new Date(t.start_date).getTime() : Infinity;
}

async function showTaskBoard(el, showCompleted = false) {
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

  const visible = showCompleted ? tasks : tasks.filter(t => t.status !== 'done');
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
    const depHtml = dep
      ? `<div style="font-size:0.78em;color:var(--text-muted);margin-top:1px">↳ ${escHtml(dep.title)}</div>`
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
      <td>${escHtml(t.title)}${depHtml}</td>
      <td style="color:var(--text-muted);font-size:0.9em">${priorityBadge(priorityByProject[t.project_id])}${escHtml(byProject[t.project_id] || '—')}</td>
      <td style="color:var(--text-muted);font-size:0.9em">${escHtml((t.resource_ids || []).map(id => byResource[id]).filter(Boolean).join(', ') || '—')}</td>
      <td style="font-size:0.85em;white-space:nowrap">${fmtDt(t.start_date)}</td>
      <td style="font-size:0.85em;white-space:nowrap">${fmtDt(t.end_date)}</td>
      <td style="font-size:0.85em">${t.duration != null ? t.duration + 'h' : '—'}</td>
      <td style="text-align:center">${pinnedBtn}</td>
    </tr>`;
  }).join('');

  const showDoneChecked = showCompleted ? 'checked' : '';
  const totalCount = tasks.length;
  const doneCount  = tasks.filter(t => t.status === 'done').length;

  el.innerHTML = `
    <div style="display:flex;align-items:baseline;gap:1.5rem;margin-bottom:1rem;flex-wrap:wrap">
      <h1 style="margin:0">Tasks</h1>
      <span style="color:var(--text-muted);font-size:0.9em">${doneCount} / ${totalCount} done</span>
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
    showTaskBoard(el, e.target.checked);
  });

  el.querySelectorAll('.status-sel').forEach(sel => {
    sel.addEventListener('change', async () => {
      try {
        await api.patch(`/tasks/${sel.dataset.id}`, { status: sel.value });
        await showTaskBoard(el, showCompleted);
      } catch (err) { alert(`Error: ${err.message}`); }
    });
  });

  el.querySelectorAll('.pin-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        const nowPinned = btn.dataset.pinned === '1';
        await api.patch(`/tasks/${btn.dataset.id}`, { pinned: !nowPinned });
        await showTaskBoard(el, showCompleted);
      } catch (err) { alert(`Error: ${err.message}`); }
    });
  });
}

registerView('/tasks', async (el) => {
  await showTaskBoard(el, false);
});
