// ── Schedule view: Timeline (Gantt) + Utilization heatmap ────────────────

const SCHED_HOUR_WIDTH = 20; // pixels per hour in the Gantt timeline

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

// ── Hourly Gantt helpers ──────────────────────────────────────────────────

const GANTT_BAR_H    = 28;  // bar height in px — must match CSS .gantt-bar height
const GANTT_LANE_PAD = 4;   // px above lane 0 and between lanes
const GANTT_LANE_H   = GANTT_BAR_H + GANTT_LANE_PAD; // 32px per lane slot

// Build date+hour header for the hourly Gantt.
// Row 1: one cell per day spanning 24 hour columns.
// Row 2: hour numbers 00–23 for every day.
function buildHourlyHeader(dates, today) {
  const row1 = dates.map(d => {
    const [y, mo, day] = d.split('-').map(Number);
    const label = new Date(y, mo - 1, day)
      .toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
    const cls = d === today ? ' gantt-today' : '';
    return `<th class="gantt-month-th${cls}" colspan="24">${label}</th>`;
  }).join('');

  const row2 = dates.flatMap(d =>
    Array.from({ length: 24 }, (_, h) => {
      const cls = d === today ? ' gantt-today' : '';
      // Label every 6 hours (00, 06, 12, 18); tick only otherwise
      const label = h % 6 === 0 ? String(h).padStart(2, '0') : '';
      const fsize = h % 6 === 0 ? '0.7rem' : '0';
      return `<th class="gantt-day-th${cls}" style="width:${SCHED_HOUR_WIDTH}px;min-width:${SCHED_HOUR_WIDTH}px;font-size:${fsize};padding:1px 0;overflow:hidden">${label}</th>`;
    })
  ).join('');

  return `<tr><th class="gantt-label-th"></th>${row1}</tr>
          <tr><th class="gantt-label-th"></th>${row2}</tr>`;
}

// Compute pixel position of a task bar within the hourly grid.
// Returns {left, width} in px, or null if the task is outside the range.
function ganttBarPosition(task, fromDate, toDate) {
  if (!task.start_date) return null;
  const rangeStartMs = Date.UTC(...fromDate.split('-').map(Number).map((v, i) => i === 1 ? v - 1 : v));
  const rangeEndMs   = rangeStartMs + (schedDaysBetween(fromDate, toDate) + 1) * 86400000;
  const taskStartMs  = new Date(task.start_date).getTime();
  const taskEndMs    = task.end_date ? new Date(task.end_date).getTime() : rangeEndMs;
  if (taskStartMs >= rangeEndMs || taskEndMs <= rangeStartMs) return null;
  const effStartMs = Math.max(taskStartMs, rangeStartMs);
  const effEndMs   = Math.min(taskEndMs, rangeEndMs);
  const leftHours  = (effStartMs - rangeStartMs) / 3600000;
  const widthHours = Math.max(1, (effEndMs - effStartMs) / 3600000);
  return {
    left:  Math.round(leftHours * SCHED_HOUR_WIDTH),
    width: Math.max(SCHED_HOUR_WIDTH, Math.round(widthHours * SCHED_HOUR_WIDTH)),
  };
}

// Greedy lane assignment: sort bars by (left, priority) so higher-priority
// tasks (lower number) win the top lane when tasks start at the same time.
// Mutates bar.lane in-place.
function assignLanes(bars) {
  bars.sort((a, b) => a.left - b.left || a.priority - b.priority);
  const laneEnds = [];
  for (const bar of bars) {
    let lane = laneEnds.findIndex(end => end <= bar.left);
    if (lane === -1) lane = laneEnds.length;
    laneEnds[lane] = bar.left + bar.width;
    bar.lane = lane;
  }
}

// Build all bar HTML for a set of tasks, with lane stacking for overlaps.
// Returns {html, trackHeight} where trackHeight accommodates all lanes.
function buildGanttBarsHtml(tasks, fromDate, toDate, priorityByProject = {}) {
  const bars = [];
  for (const task of tasks) {
    const pos = ganttBarPosition(task, fromDate, toDate);
    const priority = priorityByProject[task.project_id] || 3;
    if (pos) bars.push({ ...pos, task, priority, lane: 0 });
  }
  const defaultHeight = GANTT_LANE_PAD + GANTT_LANE_H;
  if (!bars.length) return { html: '', trackHeight: defaultHeight };

  assignLanes(bars);

  const numLanes   = Math.max(...bars.map(b => b.lane)) + 1;
  const trackHeight = GANTT_LANE_PAD + numLanes * GANTT_LANE_H;

  const html = bars.map(({ left, width, task, priority, lane }) => {
    const top        = GANTT_LANE_PAD + lane * GANTT_LANE_H;
    const startLabel = task.start_date.replace('T', ' ').slice(0, 16);
    const endLabel   = task.end_date ? task.end_date.replace('T', ' ').slice(0, 16) : '∞';
    const descLine   = task.description ? `\n${escHtml(task.description)}` : '';
    const priorityCls = priority <= 2 ? ` bar-p${priority}` : '';
    return `<div class="gantt-bar bar-${escHtml(task.status)}${priorityCls}"
                 style="left:${left}px;width:${width}px;top:${top}px"
                 title="${escHtml(task.title)} [${escHtml(task.status.replace('_', ' '))}]\n${startLabel} → ${endLabel}${descLine}"
            >${escHtml(task.title)}</div>`;
  }).join('');

  return { html, trackHeight };
}

function buildGanttResourceRowHourly(resource, tasks, fromDate, toDate, totalHours, priorityByProject = {}) {
  const resTasks = tasks.filter(t => (t.resource_ids || []).includes(resource.id));
  const { html: bars, trackHeight } = buildGanttBarsHtml(resTasks, fromDate, toDate, priorityByProject);
  const totalW = totalHours * SCHED_HOUR_WIDTH;
  const grid = `repeating-linear-gradient(90deg,transparent,transparent ${SCHED_HOUR_WIDTH - 1}px,#dee2e6 ${SCHED_HOUR_WIDTH - 1}px,#dee2e6 ${SCHED_HOUR_WIDTH}px)`;
  return `<tr>
    <td class="gantt-label-td">${escHtml(resource.name)}</td>
    <td class="gantt-track-td" colspan="${totalHours}">
      <div class="gantt-track" style="width:${totalW}px;height:${trackHeight}px;background:${grid}">${bars}</div>
    </td>
  </tr>`;
}

function buildUnassignedRowHourly(tasks, fromDate, toDate, totalHours, priorityByProject = {}) {
  const unassigned = tasks.filter(t => !(t.resource_ids || []).length && t.start_date);
  if (!unassigned.length) return '';
  const { html: bars, trackHeight } = buildGanttBarsHtml(unassigned, fromDate, toDate, priorityByProject);
  const totalW = totalHours * SCHED_HOUR_WIDTH;
  const grid = `repeating-linear-gradient(90deg,transparent,transparent ${SCHED_HOUR_WIDTH - 1}px,#dee2e6 ${SCHED_HOUR_WIDTH - 1}px,#dee2e6 ${SCHED_HOUR_WIDTH}px)`;
  return `<tr>
    <td class="gantt-label-td gantt-unassigned">Unassigned</td>
    <td class="gantt-track-td" colspan="${totalHours}">
      <div class="gantt-track" style="width:${totalW}px;height:${trackHeight}px;background:${grid}">${bars}</div>
    </td>
  </tr>`;
}

// ── Utilization heatmap (unchanged — still day-level) ─────────────────────

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

function utilizationColor(pct) {
  if (pct <= 0)  return { bg: '#f8f9fa', fg: '#adb5bd' };
  if (pct < 60)  return { bg: '#d1e7dd', fg: '#0a3622' };
  if (pct < 80)  return { bg: '#a3cfbb', fg: '#0a3622' };
  if (pct < 100) return { bg: '#ffc107', fg: '#212529' };
  if (pct === 100) return { bg: '#fd7e14', fg: 'white' };
  return { bg: '#dc3545', fg: 'white' };
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

    const todayCls    = day.day === today ? ' gantt-today' : '';
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
  const today   = schedTodayStr();
  let from      = today;
  let numDays   = 3;
  let activeTab = 'gantt';
  let renderGen = 0;

  async function render() {
    const gen = ++renderGen;
    const to  = schedAddDays(from, numDays - 1);

    el.innerHTML = `
      <h1>Schedule</h1>
      <div class="form-row" style="margin-bottom:0.75rem;align-items:center;flex-wrap:wrap;gap:0.5rem">
        <button id="btn-prev" class="btn btn-ghost" title="Previous day">‹ Prev</button>
        <label style="display:flex;align-items:center;gap:0.3rem;margin:0">
          From <input type="date" id="from-input" value="${from}">
        </label>
        <span style="color:var(--text-muted);font-size:0.85rem">→ ${to}</span>
        <label style="display:flex;align-items:center;gap:0.3rem;margin:0">
          Show <input type="number" id="days-input" value="${numDays}" min="1" max="90" style="width:55px">
          <span style="font-size:0.85rem;color:var(--text-muted)">days</span>
        </label>
        <button id="btn-next" class="btn btn-ghost" title="Next day">Next ›</button>
      </div>
      <div class="tab-bar">
        <button class="tab-btn${activeTab === 'gantt' ? ' active' : ''}" id="tab-gantt">Timeline</button>
        <button class="tab-btn${activeTab === 'utilization' ? ' active' : ''}" id="tab-util">Utilization</button>
      </div>
      <div id="view-body" style="padding-top:0.75rem"><p class="loading">Loading…</p></div>
    `;

    el.querySelector('#btn-prev').addEventListener('click', async () => {
      from = schedAddDays(from, -1); await render();
    });
    el.querySelector('#btn-next').addEventListener('click', async () => {
      from = schedAddDays(from, 1); await render();
    });
    el.querySelector('#from-input').addEventListener('change', async e => {
      from = e.target.value; await render();
    });
    el.querySelector('#days-input').addEventListener('change', async e => {
      const n = parseInt(e.target.value);
      if (n >= 1) { numDays = n; await render(); }
    });

    el.querySelector('#tab-gantt').addEventListener('click', async () => {
      if (activeTab === 'gantt') return;
      activeTab = 'gantt'; await render();
    });

    el.querySelector('#tab-util').addEventListener('click', async () => {
      if (activeTab === 'utilization') return;
      activeTab = 'utilization'; await render();
    });

    if (activeTab === 'gantt') await renderGantt(gen, to);
    else await renderUtilization(gen, to);
  }

  async function renderGantt(gen, to) {
    let resources, tasks, projects;
    try {
      [resources, tasks, projects] = await Promise.all([
        api.get('/resources/'), api.get('/tasks/'), api.get('/projects/'),
      ]);
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

    const priorityByProject = Object.fromEntries(projects.map(p => [p.id, p.priority || 3]));

    const dates      = schedGenerateDates(from, to);
    const totalHours = dates.length * 24;
    const thead      = buildHourlyHeader(dates, today);
    const tbody = [
      ...resources.map(r => buildGanttResourceRowHourly(r, tasks, from, to, totalHours, priorityByProject)),
      buildUnassignedRowHourly(tasks, from, to, totalHours, priorityByProject),
    ].join('');

    body.innerHTML = `
      <div class="gantt-legend">
        <span class="gantt-legend-item"><span class="gantt-swatch bar-todo"></span>Todo</span>
        <span class="gantt-legend-item"><span class="gantt-swatch bar-in_progress"></span>In progress</span>
        <span class="gantt-legend-item"><span class="gantt-swatch bar-blocked"></span>Blocked</span>
        <span class="gantt-legend-item"><span class="gantt-swatch bar-done"></span>Done</span>
        <span style="color:var(--text-muted);font-size:0.75rem;margin-left:auto">Today highlighted blue · hover bar for times</span>
      </div>
      <div class="gantt-scroll-wrapper">
        <table class="gantt-table">
          <thead>${thead}</thead>
          <tbody>${tbody}</tbody>
        </table>
      </div>`;
  }

  async function renderUtilization(gen, to) {
    let utilData;
    try {
      utilData = await api.get(`/utilization?from=${from}&to=${to}`);
    } catch (err) {
      if (renderGen !== gen) return;
      const body = document.getElementById('view-body');
      if (body) body.innerHTML = `<p class="error">Error: ${escHtml(err.message)}</p>`;
      return;
    }

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

    const dates = schedGenerateDates(from, to);  // to is the parameter
    body.innerHTML = buildUtilHtml(utilData, conflictsMap, dates, today);
  }

  await render();
}

registerView('/schedule', async (el) => {
  await showSchedule(el);
});
