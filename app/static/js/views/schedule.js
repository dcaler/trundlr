// ── Schedule view: Timeline (Gantt) + Utilization heatmap ────────────────

const SCHED_HOUR_WIDTH = 20; // pixels per hour in the Gantt timeline
let   GANTT_LABEL_W    = 160; // pinned label-column width (px); recomputed per render
const UTIL_DAY_W       = 96;  // utilization heatmap day-cell width (~10 days on a desktop)

function isMobileSchedule() {
  return window.matchMedia('(max-width: 640px)').matches;
}

// Pinned label-column width from CSS (--gantt-label-w on :root). Used by the
// utilization heatmap; the timeline forces 0 on mobile (label moves to its own row).
function cssLabelW() {
  const v = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--gantt-label-w'), 10);
  return isNaN(v) ? 160 : v;
}

// ── Re-flow: delegate to the backend priority-driven scheduler ──────────────
// The scheduler (POST /schedule/reflow) recomputes start/end for every todo
// task: highest-priority project first, fastest feasible path given dependencies
// and resource availability, backfilling gaps, and honoring dependency chains
// across resources and projects. Unschedulable tasks are left untouched and
// reported rather than stamped with bogus dates.

async function reflowSchedule() {
  return api.post('/schedule/reflow');
}

// Human-readable summary of a reflow result for an alert().
function reflowSummary(r) {
  const lines = [];
  if (r.changed === 0) {
    lines.push('Tasks are already in priority order — no changes needed.');
  } else {
    lines.push(`Re-flowed: ${r.changed} change${r.changed === 1 ? '' : 's'}.`);
  }
  if (r.unscheduled && r.unscheduled.length) {
    lines.push('', `${r.unscheduled.length} task${r.unscheduled.length === 1 ? '' : 's'} couldn't be scheduled:`);
    r.unscheduled.forEach(u => lines.push(`• ${u.title} — ${u.reason}`));
  }
  return lines.join('\n');
}

// ── Date helpers ──────────────────────────────────────────────────────────

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
      // overflow:visible so "00"/"06" etc. aren't clipped by the narrow column
      return `<th class="gantt-day-th${cls}" style="width:${SCHED_HOUR_WIDTH}px;min-width:${SCHED_HOUR_WIDTH}px;font-size:${fsize};padding:1px 0;overflow:visible">${label}</th>`;
    })
  ).join('');

  return `<tr><th class="gantt-label-th"></th>${row1}</tr>
          <tr><th class="gantt-label-th"></th>${row2}</tr>`;
}

// Compute pixel position of a task bar within the hourly grid.
// Returns {left, width} in px, or null if the task is outside the range.
function ganttBarPosition(task, fromDate, toDate) {
  if (!task.start_date) return null;
  const [fy, fm, fd] = fromDate.split('-').map(Number);
  const rangeStartMs = new Date(fy, fm - 1, fd).getTime(); // local midnight, consistent with task datetimes
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
function buildGanttBarsHtml(tasks, fromDate, toDate, priorityByProject = {}, projectById = {}) {
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
    const projectName = projectById[task.project_id]?.name;
    const projectLine = projectName ? `\n${escHtml(projectName)}` : '';
    const descLine    = task.description ? `\n${escHtml(task.description)}` : '';
    const priorityCls = priority ? ` bar-p${priority}` : '';
    return `<div class="gantt-bar bar-${escHtml(task.status)}${priorityCls}"
                 style="left:${left}px;width:${width}px;top:${top}px"
                 title="${escHtml(task.title)} [${escHtml(task.status.replace('_', ' '))}]\n${startLabel} → ${endLabel}${projectLine}${descLine}"
            >${escHtml(task.title)}</div>`;
  }).join('');

  return { html, trackHeight };
}

// Build a CSS linear-gradient encoding resource availability over the date range.
// Unavailable hours get a grey tint; full-day blockouts get a red tint.
// Returns a gradient string, or null if the resource is available during every hour shown.
function buildAvailabilityGradient(resource, dates, windows, blockouts) {
  const toH = t => { const [h, m] = t.split(':').map(Number); return h + m / 60; };
  const px  = h => `${Math.round(h * SCHED_HOUR_WIDTH)}px`;
  const totalHours = dates.length * 24;
  const hasWindows = windows.length > 0;

  // Per-hour status: 0 = available, 1 = unavailable, 2 = blocked out
  const st = new Uint8Array(totalHours);

  dates.forEach((dateStr, di) => {
    const base = di * 24;
    const [y, m, d] = dateStr.split('-').map(Number);
    const dow = (new Date(y, m - 1, d).getDay() + 6) % 7; // 0=Mon…6=Sun
    const dayBlocks = blockouts.filter(b => dateStr >= b.start_date && dateStr <= b.end_date);

    if (dayBlocks.some(b => b.from_time === null)) {
      st.fill(2, base, base + 24);
      return;
    }

    let avail = [];
    if (hasWindows) {
      avail = windows.filter(w => w.day_of_week === dow)
        .map(w => [toH(w.from_time), toH(w.to_time)]);
    } else if (resource.available_days & (1 << dow)) {
      avail = [[toH(resource.available_from || '09:00'), toH(resource.available_to || '17:00')]];
    }

    for (let h = 0; h < 24; h++) {
      if (!avail.some(([f, t]) => h >= f && h < t)) st[base + h] = 1;
    }

    dayBlocks.filter(b => b.from_time !== null).forEach(b => {
      const f = toH(b.from_time), t = toH(b.to_time);
      for (let h = Math.floor(f); h < Math.ceil(t) && h < 24; h++) st[base + h] = 2;
    });
  });

  if (!st.some(v => v > 0)) return null;

  const COLORS = [null, 'rgba(0,0,0,0.12)', 'rgba(220,53,69,0.22)'];
  const stops = [];
  let runSt = st[0], runStart = 0;

  const flushRun = (from, to, s) => {
    const color = COLORS[s] || 'transparent';
    stops.push(`${color} ${px(from)}`);
    stops.push(`${color} ${px(to)}`);
  };

  for (let h = 1; h <= totalHours; h++) {
    const s = h < totalHours ? st[h] : -1;
    if (s !== runSt) { flushRun(runStart, h, runSt); runSt = s; runStart = h; }
  }

  if (!stops.length) return null;
  return `linear-gradient(90deg, ${stops.join(', ')})`;
}

function buildGanttResourceRowHourly(resource, tasks, fromDate, toDate, totalHours, priorityByProject = {}, projectById = {}, windows = [], blockouts = []) {
  const dates    = schedGenerateDates(fromDate, toDate);
  const resTasks = tasks.filter(t => (t.resource_ids || []).includes(resource.id));
  const { html: bars, trackHeight } = buildGanttBarsHtml(resTasks, fromDate, toDate, priorityByProject, projectById);
  const totalW    = totalHours * SCHED_HOUR_WIDTH;
  const grid      = `repeating-linear-gradient(90deg,transparent,transparent ${SCHED_HOUR_WIDTH - 1}px,#dee2e6 ${SCHED_HOUR_WIDTH - 1}px,#dee2e6 ${SCHED_HOUR_WIDTH}px)`;
  const availGrad = buildAvailabilityGradient(resource, dates, windows, blockouts);
  const background = availGrad ? `${grid}, ${availGrad}` : grid;
  const nameRow = `<tr class="gantt-name-row"><td class="gantt-name-td" colspan="${totalHours + 1}"><div class="gantt-name-inner">${escHtml(resource.name)}</div></td></tr>`;
  return `${nameRow}<tr>
    <td class="gantt-label-td">${escHtml(resource.name)}</td>
    <td class="gantt-track-td" colspan="${totalHours}">
      <div class="gantt-track" style="width:${totalW}px;height:${trackHeight}px;background:${background}">${bars}</div>
    </td>
  </tr>`;
}

function buildUnassignedRowHourly(tasks, fromDate, toDate, totalHours, priorityByProject = {}, projectById = {}) {
  const unassigned = tasks.filter(t => !(t.resource_ids || []).length && t.start_date);
  if (!unassigned.length) return '';
  const { html: bars, trackHeight } = buildGanttBarsHtml(unassigned, fromDate, toDate, priorityByProject, projectById);
  const totalW = totalHours * SCHED_HOUR_WIDTH;
  const grid = `repeating-linear-gradient(90deg,transparent,transparent ${SCHED_HOUR_WIDTH - 1}px,#dee2e6 ${SCHED_HOUR_WIDTH - 1}px,#dee2e6 ${SCHED_HOUR_WIDTH}px)`;
  const nameRow = `<tr class="gantt-name-row"><td class="gantt-name-td" colspan="${totalHours + 1}"><div class="gantt-name-inner gantt-unassigned">Unassigned</div></td></tr>`;
  return `${nameRow}<tr>
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
    const [y, mo, dd] = d.split('-').map(Number);
    const wd  = new Date(y, mo - 1, dd).toLocaleDateString('en-US', { weekday: 'short' });
    return `<th class="gantt-day-th${d === today ? ' gantt-today' : ''}" style="width:${UTIL_DAY_W}px;min-width:${UTIL_DAY_W}px;max-width:${UTIL_DAY_W}px">${wd} ${dd}</th>`;
  }).join('');

  return `<tr><th class="gantt-label-th"></th>${row1}</tr>
          <tr><th class="gantt-label-th"></th>${row2}</tr>`;
}

const UTIL_TOL = 0.05; // hours within which committed ≈ capacity counts as "at capacity"

// Format an over/under hours value rounded to ≤1 decimal: "+2" (over), "−3" (spare).
function fmtNetHours(over) {
  const r = Math.round(over * 10) / 10;
  const mag = Number.isInteger(r) ? Math.abs(r) : Math.abs(r).toFixed(1);
  return r > 0 ? `+${mag}` : `−${mag}`; // minus sign for spare
}

// Cell appearance from (committed, capacity) hours. Washed-out colors:
// red = over availability (+h), blue = under (−h spare), green = even.
// The number is committed − capacity: positive = hours OVER availability.
function netCellInfo(committed, capacity) {
  if (capacity === 0 && committed === 0) {
    return { label: '', bg: '#f8f9fa', fg: '#adb5bd', conflict: false }; // unavailable / empty
  }
  const over = committed - capacity;
  if (over >  UTIL_TOL) return { label: fmtNetHours(over), bg: 'rgba(220,53,69,0.09)', fg: '#842029', conflict: true  }; // over
  if (over < -UTIL_TOL) return { label: fmtNetHours(over), bg: 'rgba(13,110,253,0.06)', fg: '#0a467e', conflict: false }; // under
  return                       { label: '—',               bg: 'rgba(25,135,84,0.09)', fg: '#0a3622', conflict: false }; // even
}

// Forward-looking capacity summary for the coming 10 days from today.
function forwardSummary(days, today) {
  const fwd = days.filter(d => d.day >= today).slice(0, 10);
  if (!fwd.length) return '';
  let over = 0, spare = 0;
  for (const d of fwd) {
    const net = d.committed - d.capacity;
    if (net > 0) over += net; else spare += -net;
  }
  if (over > UTIL_TOL) {
    return `<small class="util-summary" style="color:var(--danger)">⚠ Over by ${over.toFixed(1)}h</small>`;
  }
  return `<small class="util-summary" style="color:var(--text-muted)">${spare.toFixed(1)}h spare</small>`;
}

function buildUtilResourceRow(resource, conflictsByDay, today) {
  const days = resource.days;

  const cells = days.map(day => {
    const info = netCellInfo(day.committed, day.capacity);

    let tooltip = `${day.day}: ${day.committed.toFixed(1)}h assigned / ${day.capacity.toFixed(1)}h available`;
    const cdata = conflictsByDay[day.day];
    if (info.conflict && cdata) {
      const names = cdata.tasks.map(t => t.title).join(', ');
      tooltip += `\n⚠ Over by ${cdata.overage.toFixed(1)}h — ${names}`;
    }

    const todayCls    = day.day === today ? ' util-today' : '';
    const conflictCls = info.conflict ? ' util-conflict' : '';

    return `<td class="util-cell${conflictCls}${todayCls}"
                 style="background:${info.bg};color:${info.fg}"
                 title="${escHtml(tooltip)}">${info.label}</td>`;
  }).join('');

  return `<tr>
    <td class="gantt-label-td util-label-td">
      <div style="font-weight:600">${escHtml(resource.resource_name)}</div>
      ${forwardSummary(days, today)}
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
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:rgba(13,110,253,0.06);border:1px solid #dee2e6"></span>Under (spare h)</span>
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:rgba(25,135,84,0.09);border:1px solid #dee2e6"></span>At capacity</span>
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:rgba(220,53,69,0.09);border:1px solid #dee2e6"></span>Over ⚠</span>
      <span style="color:var(--text-muted);font-size:0.75rem;margin-left:auto">Cells show over (+) / spare (−) hours · hover for detail</span>
    </div>
    <div class="gantt-scroll-wrapper">
      <table class="gantt-table" style="width:${GANTT_LABEL_W + dates.length * UTIL_DAY_W}px">
        <thead>${thead}</thead>
        <tbody>${tbody}</tbody>
      </table>
    </div>`;
}

// ── Main view ─────────────────────────────────────────────────────────────

async function showSchedule(el) {
  const today   = schedTodayStr();
  let activeTab = 'gantt';
  let renderGen = 0;

  async function render() {
    const gen = ++renderGen;

    el.innerHTML = `
      <h1>Schedule</h1>
      <div class="tab-bar">
        <button class="tab-btn${activeTab === 'gantt' ? ' active' : ''}" id="tab-gantt">Timeline</button>
        <button class="tab-btn${activeTab === 'utilization' ? ' active' : ''}" id="tab-util">Utilization</button>
      </div>
      <div id="view-body" style="padding-top:0.75rem"><p class="loading">Loading…</p></div>
    `;

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
    // Mobile: label column collapses to 0; names render on their own rows.
    GANTT_LABEL_W = isMobileSchedule() ? 0 : cssLabelW();
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

    // Compute date range from all task dates, always including today
    const taskDateStrs = tasks.flatMap(t => [t.start_date, t.end_date]).filter(Boolean).map(d => d.slice(0, 10));
    const earliest = taskDateStrs.length ? taskDateStrs.reduce((a, b) => a < b ? a : b) : today;
    const latest   = taskDateStrs.length ? taskDateStrs.reduce((a, b) => a > b ? a : b) : schedAddDays(today, 14);
    const from = schedAddDays(earliest < today ? earliest : today, -7);
    const to   = schedAddDays(latest   > today ? latest   : today,  14);

    // Fetch availability data for shading; fall back to empty if requests fail.
    // CalDAV blocks are merged into blockouts so they shade red identically.
    let windowsByResource = {}, blockoutsByResource = {};
    try {
      const [wList, bList, cbList] = await Promise.all([
        Promise.all(resources.map(r => api.get(`/resources/${r.id}/windows`))),
        Promise.all(resources.map(r => api.get(`/resources/${r.id}/blockouts`))),
        Promise.all(resources.map(r => api.get(`/resources/${r.id}/calblocks`))),
      ]);
      windowsByResource  = Object.fromEntries(resources.map((r, i) => [r.id, wList[i]]));
      blockoutsByResource = Object.fromEntries(resources.map((r, i) => [r.id, [...bList[i], ...cbList[i]]]));
    } catch (_) {}
    if (renderGen !== gen) return;

    const priorityByProject = Object.fromEntries(projects.map(p => [p.id, p.priority || 3]));
    const projectById       = Object.fromEntries(projects.map(p => [p.id, p]));

    const dates      = schedGenerateDates(from, to);
    const totalHours = dates.length * 24;
    const thead      = buildHourlyHeader(dates, today);
    const tbody = [
      ...resources.map(r => buildGanttResourceRowHourly(
        r, tasks, from, to, totalHours, priorityByProject, projectById,
        windowsByResource[r.id] || [], blockoutsByResource[r.id] || []
      )),
      buildUnassignedRowHourly(tasks, from, to, totalHours, priorityByProject, projectById),
    ].join('');

    body.innerHTML = `
      <div class="gantt-legend">
        <span class="gantt-legend-item"><span class="gantt-swatch bar-todo"></span>Todo</span>
        <span class="gantt-legend-item"><span class="gantt-swatch bar-in_progress"></span>In progress</span>
        <span class="gantt-legend-item"><span class="gantt-swatch bar-blocked"></span>Blocked</span>
        <span class="gantt-legend-item"><span class="gantt-swatch bar-done"></span>Done</span>
        <span class="gantt-legend-item"><span class="gantt-swatch" style="background:rgba(0,0,0,0.12);border:1px solid #dee2e6"></span>Unavailable</span>
        <span class="gantt-legend-item"><span class="gantt-swatch" style="background:rgba(220,53,69,0.22);border:1px solid #dee2e6"></span>Blockout</span>
        <span style="margin-left:auto;display:flex;align-items:center;gap:0.75rem">
          <button id="btn-realign" class="btn btn-ghost" style="font-size:0.75rem;padding:0.2rem 0.5rem">↺ Re-flow</button>
          <span style="color:var(--text-muted);font-size:0.75rem">Today highlighted blue · hover bar for times</span>
        </span>
      </div>
      <div class="gantt-scroll-wrapper">
        <table class="gantt-table gantt-timeline" style="width:${GANTT_LABEL_W + totalHours * SCHED_HOUR_WIDTH}px">
          <thead>${thead}</thead>
          <tbody>${tbody}</tbody>
        </table>
      </div>`;

    // Pin label column and anchor scroll to today.
    // CSS sticky is unreliable on border-collapse:collapse tables; translateX is guaranteed.
    const wrapper = body.querySelector('.gantt-scroll-wrapper');
    if (wrapper) {
      const syncLabels = () => {
        const x = wrapper.scrollLeft;
        wrapper.querySelectorAll('.gantt-label-th, .gantt-label-td, .gantt-name-inner')
          .forEach(el => { el.style.transform = `translateX(${x}px)`; });
      };
      wrapper.addEventListener('scroll', syncLabels, { passive: true });
      const daysIn = schedDaysBetween(from, today);
      wrapper.scrollLeft = Math.max(0, daysIn * 24 * SCHED_HOUR_WIDTH - 2 * SCHED_HOUR_WIDTH);
      syncLabels();

      // "Now" indicator line
      const [fy, fm, fd] = from.split('-').map(Number);
      const rangeStartMs = new Date(fy, fm - 1, fd).getTime();
      const nowPx = GANTT_LABEL_W + Math.round((Date.now() - rangeStartMs) / 3_600_000 * SCHED_HOUR_WIDTH);
      const nowLine = document.createElement('div');
      nowLine.className = 'gantt-now-line';
      nowLine.style.left = `${nowPx}px`;
      wrapper.appendChild(nowLine);
    }

    body.querySelector('#btn-realign').addEventListener('click', async () => {
      const btn = body.querySelector('#btn-realign');
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
          await render();
        }
      } catch (err) {
        alert(`Re-flow failed: ${err.message}`);
        btn.disabled = false;
        btn.textContent = '↺ Re-flow';
      }
    });
  }

  async function renderUtilization(gen) {
    GANTT_LABEL_W = cssLabelW();
    // Span from 7 days before today to 60 days ahead
    const from = schedAddDays(today, -7);
    const to   = schedAddDays(today,  60);

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

    const dates = schedGenerateDates(from, to);
    body.innerHTML = buildUtilHtml(utilData, conflictsMap, dates, today);

    // Pin label column and anchor scroll to today
    const wrapper = body.querySelector('.gantt-scroll-wrapper');
    if (wrapper) {
      const syncLabels = () => {
        const x = wrapper.scrollLeft;
        wrapper.querySelectorAll('.gantt-label-th, .gantt-label-td')
          .forEach(el => { el.style.transform = `translateX(${x}px)`; });
      };
      wrapper.addEventListener('scroll', syncLabels, { passive: true });
      const daysIn = schedDaysBetween(from, today);
      // Snap today's column flush against the pinned label column so it shows in
      // full (its content-left is GANTT_LABEL_W + daysIn·UTIL_DAY_W; subtracting
      // GANTT_LABEL_W lands its left edge exactly at the label's right edge).
      wrapper.scrollLeft = Math.max(0, daysIn * UTIL_DAY_W);
      syncLabels();
    }
  }

  await render();
}

registerView('/schedule', async (el) => {
  await showSchedule(el);
});
