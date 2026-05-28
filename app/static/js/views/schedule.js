// ── Schedule view: Timeline (Gantt) + Utilization heatmap ────────────────

const SCHED_DAY_WIDTH = 28; // pixels per day — must match gantt.py DAY_WIDTH arg

// ── Date helpers (timezone-safe via Date.UTC) ─────────────────────────────

function schedTodayStr() {
  const d = new Date();
  return [
    d.getFullYear(),
    String(d.getMonth() + 1).padStart(2, '0'),
    String(d.getDate()).padStart(2, '0'),
  ].join('-');
}

function schedAddDays(dateStr, n) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(y, m - 1, d + n);
  return [
    dt.getFullYear(),
    String(dt.getMonth() + 1).padStart(2, '0'),
    String(dt.getDate()).padStart(2, '0'),
  ].join('-');
}

function schedDaysBetween(a, b) {
  const [ay, am, ad] = a.split('-').map(Number);
  const [by, bm, bd] = b.split('-').map(Number);
  return (Date.UTC(by, bm - 1, bd) - Date.UTC(ay, am - 1, ad)) / 86400000;
}

function schedGenerateDates(from, to) {
  const dates = [];
  let cur = from;
  while (cur <= to) { dates.push(cur); cur = schedAddDays(cur, 1); }
  return dates;
}

function schedMonthLabel(dateStr) {
  const [y, m] = dateStr.split('-').map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
}

// ── Shared date-header (used by both Gantt and Utilization) ───────────────

function buildDateHeader(dates, today) {
  const months = [];
  let curKey = '', curCount = 0;
  for (const d of dates) {
    const key = d.slice(0, 7);
    if (key !== curKey) {
      if (curKey) months.push({ key: curKey, count: curCount });
      curKey = key; curCount = 1;
    } else { curCount++; }
  }
  if (curKey) months.push({ key: curKey, count: curCount });

  const row1 = months.map(mo =>
    `<th class="gantt-month-th" colspan="${mo.count}">${schedMonthLabel(mo.key + '-01')}</th>`
  ).join('');

  const row2 = dates.map(d => {
    const day = parseInt(d.slice(8), 10);
    return `<th class="gantt-day-th${d === today ? ' gantt-today' : ''}">${day}</th>`;
  }).join('');

  return `<tr><th class="gantt-label-th"></th>${row1}</tr>
          <tr><th class="gantt-label-th"></th>${row2}</tr>`;
}

// ── Gantt rendering ───────────────────────────────────────────────────────

function buildTaskBar(task, from, to, dates) {
  if (!task.start_date || task.start_date > to) return '';
  const effectiveEnd = task.end_date || to;
  if (effectiveEnd < from) return '';
  const fromDay = Math.max(0, schedDaysBetween(from, task.start_date));
  const toDay   = Math.min(dates.length - 1, schedDaysBetween(from, effectiveEnd));
  const left    = fromDay * SCHED_DAY_WIDTH;
  const width   = Math.max(SCHED_DAY_WIDTH, (toDay - fromDay + 1) * SCHED_DAY_WIDTH);
  return `<div class="gantt-bar bar-${escHtml(task.status)}"
               style="left:${left}px;width:${width}px"
               title="${escHtml(task.title)} [${escHtml(task.status.replace('_', ' '))}]"
          >${escHtml(task.title)}</div>`;
}

function buildGanttResourceRow(resource, tasks, from, to, dates) {
  const bars = tasks
    .filter(t => t.resource_id === resource.id)
    .map(t => buildTaskBar(t, from, to, dates)).join('');
  const totalW = dates.length * SCHED_DAY_WIDTH;
  return `<tr>
    <td class="gantt-label-td">${escHtml(resource.name)}</td>
    <td class="gantt-track-td" colspan="${dates.length}">
      <div class="gantt-track" style="width:${totalW}px">${bars}</div>
    </td>
  </tr>`;
}

function buildUnassignedRow(tasks, from, to, dates) {
  const unassigned = tasks.filter(t => !t.resource_id && t.start_date);
  if (!unassigned.length) return '';
  const bars = unassigned.map(t => buildTaskBar(t, from, to, dates)).join('');
  const totalW = dates.length * SCHED_DAY_WIDTH;
  return `<tr>
    <td class="gantt-label-td gantt-unassigned">Unassigned</td>
    <td class="gantt-track-td" colspan="${dates.length}">
      <div class="gantt-track" style="width:${totalW}px">${bars}</div>
    </td>
  </tr>`;
}

// ── Utilization heatmap ───────────────────────────────────────────────────

function utilizationColor(pct) {
  if (pct <= 0)  return { bg: '#f8f9fa', fg: '#adb5bd' };
  if (pct < 60)  return { bg: '#d1e7dd', fg: '#0a3622' };
  if (pct < 80)  return { bg: '#a3cfbb', fg: '#0a3622' };
  if (pct < 100) return { bg: '#ffc107', fg: '#212529' };
  if (pct === 100) return { bg: '#fd7e14', fg: 'white' };
  return { bg: '#dc3545', fg: 'white' };   // over-allocated
}

function buildUtilResourceRow(resource, conflictsByDay, today) {
  const days = resource.days;
  const peakPct = days.reduce((mx, d) => Math.max(mx, d.utilization), 0);
  const conflictCount = days.filter(d => d.committed > d.capacity).length;

  const cells = days.map(day => {
    const isConflict = day.committed > day.capacity;
    const colors = utilizationColor(day.utilization);
    const pctRounded = Math.round(day.utilization);
    const label = pctRounded > 0 ? (isConflict ? `!${pctRounded}` : String(pctRounded)) : '';

    let tooltip = `${day.day}: ${day.committed.toFixed(1)}/${day.capacity.toFixed(1)} = ${pctRounded}%`;
    const cdata = conflictsByDay[day.day];
    if (isConflict && cdata) {
      const names = cdata.tasks.map(t => t.title).join(', ');
      tooltip += `\n⚠ Over by ${cdata.overage.toFixed(1)} — ${names}`;
    }

    const todayCls = day.day === today ? ' gantt-today' : '';
    const conflictCls = isConflict ? ' util-conflict' : '';

    return `<td class="util-cell${conflictCls}${todayCls}"
                 style="background:${colors.bg};color:${colors.fg}"
                 title="${escHtml(tooltip)}">${label}</td>`;
  }).join('');

  const summaryHtml = conflictCount > 0
    ? `<small style="color:var(--danger)">⚠ ${conflictCount} conflict day${conflictCount !== 1 ? 's' : ''}</small>`
    : `<small style="color:var(--text-muted)">Peak: ${Math.round(peakPct)}%</small>`;

  return `<tr>
    <td class="gantt-label-td util-label-td">
      <div style="font-weight:600">${escHtml(resource.resource_name)}</div>
      ${summaryHtml}
    </td>
    ${cells}
  </tr>`;
}

function buildUtilHtml(utilData, conflictsMap, dates, today) {
  if (!utilData.length) {
    return '<p style="color:var(--text-muted)">No resources found — add some in the Resources tab.</p>';
  }
  const thead = buildDateHeader(dates, today);
  const tbody = utilData.map(r =>
    buildUtilResourceRow(r, conflictsMap[r.resource_id] || {}, today)
  ).join('');

  return `
    <div class="gantt-legend" style="margin-top:0.75rem">
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:#f8f9fa;border:1px solid #dee2e6"></span>0%</span>
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:#d1e7dd"></span>&lt;60%</span>
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:#a3cfbb"></span>60–79%</span>
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:#ffc107"></span>80–99%</span>
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:#fd7e14"></span>100%</span>
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:#dc3545"></span>&gt;100% ⚠</span>
      <span style="color:var(--text-muted);font-size:0.75rem;margin-left:auto">Hover a cell for details</span>
    </div>
    <div class="gantt-scroll-wrapper">
      <table class="gantt-table">
        <thead>${thead}</thead>
        <tbody>${tbody}</tbody>
      </table>
    </div>`;
}

// ── Main view ─────────────────────────────────────────────────────────────

async function showSchedule(el) {
  const today = schedTodayStr();
  let from      = today;
  let to        = schedAddDays(today, 27);
  let activeTab = 'gantt';
  let renderGen = 0;

  async function render() {
    const gen = ++renderGen;

    el.innerHTML = `
      <h1>Schedule</h1>
      <form id="schedule-form" class="form-row" style="margin-bottom:0.75rem">
        <div><label>From</label><input type="date" name="from" value="${from}" required></div>
        <div><label>To</label><input type="date" name="to" value="${to}" required></div>
        <div style="align-self:flex-end">
          <button type="submit" class="btn btn-primary">View</button>
        </div>
      </form>
      <div class="tab-bar">
        <button class="tab-btn${activeTab === 'gantt' ? ' active' : ''}" id="tab-gantt">Timeline</button>
        <button class="tab-btn${activeTab === 'utilization' ? ' active' : ''}" id="tab-util">Utilization</button>
      </div>
      <div id="view-body" style="padding-top:0.75rem"><p class="loading">Loading…</p></div>
    `;

    el.querySelector('#schedule-form').addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const nf = fd.get('from'), nt = fd.get('to');
      if (nf > nt) { alert("'From' must not be after 'To'."); return; }
      from = nf; to = nt;
      await render();
    });

    el.querySelector('#tab-gantt').addEventListener('click', async () => {
      if (activeTab === 'gantt') return;
      activeTab = 'gantt'; await render();
    });

    el.querySelector('#tab-util').addEventListener('click', async () => {
      if (activeTab === 'utilization') return;
      activeTab = 'utilization'; await render();
    });

    if (activeTab === 'gantt') await renderGantt(gen);
    else await renderUtilization(gen);
  }

  async function renderGantt(gen) {
    let resources, tasks;
    try {
      [resources, tasks] = await Promise.all([api.get('/resources/'), api.get('/tasks/')]);
    } catch (err) {
      if (renderGen !== gen) return;
      const body = document.getElementById('view-body');
      if (body) body.innerHTML = `<p class="error">Error: ${escHtml(err.message)}</p>`;
      return;
    }
    if (renderGen !== gen) return;
    const body = document.getElementById('view-body');
    if (!body) return;

    if (!resources.length) {
      body.innerHTML = '<p style="color:var(--text-muted)">No resources yet — add some in the Resources tab.</p>';
      return;
    }

    const dates = schedGenerateDates(from, to);
    const thead = buildDateHeader(dates, today);
    const tbody = [
      ...resources.map(r => buildGanttResourceRow(r, tasks, from, to, dates)),
      buildUnassignedRow(tasks, from, to, dates),
    ].join('');

    body.innerHTML = `
      <div class="gantt-legend">
        <span class="gantt-legend-item"><span class="gantt-swatch bar-todo"></span>Todo</span>
        <span class="gantt-legend-item"><span class="gantt-swatch bar-in_progress"></span>In progress</span>
        <span class="gantt-legend-item"><span class="gantt-swatch bar-blocked"></span>Blocked</span>
        <span class="gantt-legend-item"><span class="gantt-swatch bar-done"></span>Done</span>
        <span style="color:var(--text-muted);font-size:0.75rem;margin-left:auto">Today highlighted blue</span>
      </div>
      <div class="gantt-scroll-wrapper">
        <table class="gantt-table">
          <thead>${thead}</thead>
          <tbody>${tbody}</tbody>
        </table>
      </div>`;
  }

  async function renderUtilization(gen) {
    let utilData;
    try {
      utilData = await api.get(`/utilization?from=${from}&to=${to}`);
    } catch (err) {
      if (renderGen !== gen) return;
      const body = document.getElementById('view-body');
      if (body) body.innerHTML = `<p class="error">Error: ${escHtml(err.message)}</p>`;
      return;
    }

    // Fetch conflicts for over-allocated resources (best-effort)
    const conflictsMap = {};
    const overResources = utilData.filter(r => r.days.some(d => d.committed > d.capacity));
    if (overResources.length) {
      try {
        const results = await Promise.all(
          overResources.map(r =>
            api.get(`/resources/${r.resource_id}/conflicts?from=${from}&to=${to}`)
              .then(c => ({ id: r.resource_id, conflicts: c }))
          )
        );
        for (const { id, conflicts } of results) {
          conflictsMap[id] = {};
          for (const c of conflicts) conflictsMap[id][c.day] = c;
        }
      } catch (_) { /* heatmap still renders without task-level detail */ }
    }

    if (renderGen !== gen) return;
    const body = document.getElementById('view-body');
    if (!body) return;

    const dates = schedGenerateDates(from, to);
    body.innerHTML = buildUtilHtml(utilData, conflictsMap, dates, today);
  }

  await render();
}

registerView('/schedule', async (el) => {
  await showSchedule(el);
});
