// ── Schedule view: Timeline (Gantt) + Utilization heatmap ────────────────

const SCHED_HOUR_WIDTH = 20; // pixels per hour in the Gantt timeline
const GANTT_LABEL_W    = 160; // must match .gantt-label-th/.gantt-label-td width in CSS
const UTIL_DAY_W       = 28;  // must match .gantt-day-th / .util-cell width in CSS

// ── Availability helpers ──────────────────────────────────────────────────

// Advance cursorMs to the next moment inside resource.available_from/to/days,
// skipping any blockout periods.  If cursor is already in a valid slot, returns it.
function nextSlotInWindow(cursorMs, resource, blockouts = []) {
  const timeToMs = t => { const [h, m] = t.split(':').map(Number); return (h * 60 + m) * 60000; };
  const fmtDate  = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;

  const fromMs = timeToMs(resource.available_from || '00:00');
  const toMs   = timeToMs(resource.available_to   || '23:59');

  let probe = cursorMs;
  for (let i = 0; i < 730; i++) {
    const d        = new Date(probe);
    const dateStr  = fmtDate(d);
    const dow      = (d.getDay() + 6) % 7; // 0=Mon … 6=Sun
    const dayStart = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const nextDay  = dayStart + 86400000 + fromMs;

    // Full-day blockout — skip entire day
    if (blockouts.some(b => b.from_time === null && dateStr >= b.start_date && dateStr <= b.end_date)) {
      probe = nextDay; continue;
    }

    // Not an available workday — skip to next day
    if (!(resource.available_days & (1 << dow))) { probe = nextDay; continue; }

    const winStart = dayStart + fromMs;
    const winEnd   = dayStart + toMs;

    if (probe < winStart) { probe = winStart; continue; } // before window — jump to start
    if (probe >= winEnd)  { probe = nextDay;  continue; } // past window  — next day

    // Inside window — skip over any partial blockout that covers probe
    const partial = blockouts.find(b => {
      if (b.from_time === null) return false;
      if (dateStr < b.start_date || dateStr > b.end_date) return false;
      const bs = dayStart + timeToMs(b.from_time);
      const be = dayStart + timeToMs(b.to_time);
      return probe >= bs && probe < be;
    });
    if (partial) { probe = dayStart + timeToMs(partial.to_time); continue; }

    return probe;
  }
  return probe;
}

// ── Re-align: sort tasks by project priority per resource, sequential start times ─

async function realignSchedule(resources, tasks, projects) {
  const priorityByProject = Object.fromEntries(projects.map(p => [p.id, p.priority || 3]));

  // patchMap records ALL claimed tasks (changed or not) so shared tasks aren't
  // double-scheduled when processed by a second resource.
  const patchMap = new Map(); // id → {start, end}

  const fmt = ms => {
    const d = new Date(ms);
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:00`;
  };

  // Resolve the end of a task using already-computed times where available
  const resolvedEnd = id => {
    if (patchMap.has(id)) return new Date(patchMap.get(id).end).getTime();
    const t = tasks.find(t => t.id === id);
    return t ? new Date(t.end_date || t.start_date || 0).getTime() : 0;
  };

  // Window boundary helpers (simple available_from/to model)
  const winStart = (ms, r) => {
    const [h, m] = (r.available_from || '00:00').split(':').map(Number);
    const d = new Date(ms);
    return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() + (h*60+m)*60000;
  };
  const winEnd = (ms, r) => {
    const [h, m] = (r.available_to || '23:59').split(':').map(Number);
    const d = new Date(ms);
    return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() + (h*60+m)*60000;
  };

  // Fetch blockouts
  let blockoutsByResource = {};
  try {
    const bList = await Promise.all(resources.map(r => api.get(`/resources/${r.id}/blockouts`)));
    blockoutsByResource = Object.fromEntries(resources.map((r, i) => [r.id, bList[i]]));
  } catch (_) {}

  for (const resource of resources) {
    const onResource = t => (t.resource_ids || []).includes(resource.id);
    const blockouts  = blockoutsByResource[resource.id] || [];

    // Only todo tasks are moved; already-claimed tasks (from earlier resource iterations) are skipped
    const movable = tasks
      .filter(t => onResource(t) && t.status === 'todo' && !patchMap.has(t.id))
      .sort((a, b) => {
        const pa = priorityByProject[a.project_id] || 3;
        const pb = priorityByProject[b.project_id] || 3;
        return pa !== pb ? pa - pb : a.id - b.id; // stable tiebreaker → idempotent
      });

    if (!movable.length) continue;

    // Cursor floor: after the last in-flight task (in_progress/done/failed only —
    // blocked tasks are ignored and don't occupy queue space)
    // and after any task already claimed by an earlier resource iteration on this resource
    const fixedEnd   = tasks
      .filter(t => onResource(t) && t.end_date && !['todo', 'blocked'].includes(t.status))
      .reduce((mx, t) => Math.max(mx, new Date(t.end_date).getTime()), 0);
    const claimedEnd = tasks
      .filter(t => onResource(t) && patchMap.has(t.id))
      .reduce((mx, t) => Math.max(mx, new Date(patchMap.get(t.id).end).getTime()), 0);

    let cursor = Math.max(fixedEnd, claimedEnd, Date.now());

    for (const task of movable) {
      // Fall back to task.duration (hours) when no dates have been set yet
      const dur = task.start_date && task.end_date
        ? new Date(task.end_date).getTime() - new Date(task.start_date).getTime()
        : (task.duration || 1) * 3600000;

      // Respect depends_on: never start before the dependency finishes
      if (task.depends_on_id) cursor = Math.max(cursor, resolvedEnd(task.depends_on_id));

      // Advance to the next valid moment (respects window + blockouts)
      cursor = nextSlotInWindow(cursor, resource, blockouts);

      // Fit-to-window: if the task won't fit in the remaining window and we're
      // mid-window, skip to the next window start so more of the task falls within
      // availability rather than straddling the boundary from an arbitrary mid-point
      const wEnd   = winEnd(cursor, resource);
      const wStart = winStart(cursor, resource);
      if (cursor + dur > wEnd && cursor > wStart) {
        cursor = nextSlotInWindow(wEnd, resource, blockouts);
      }

      patchMap.set(task.id, { start: fmt(cursor), end: fmt(cursor + dur) });
      cursor += dur;
    }
  }

  // PATCH only tasks whose time actually changed; return the count of changes
  let changed = 0;
  for (const [id, { start, end }] of patchMap.entries()) {
    const task = tasks.find(t => t.id === id);
    if (!task || start !== task.start_date) {
      await api.patch(`/tasks/${id}`, { start_date: start, end_date: end });
      changed++;
    }
  }
  return changed;
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
  return `<tr>
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
  if (pct <= 0)   return { bg: '#f8f9fa', fg: '#adb5bd' }; // empty
  if (pct < 100)  return { bg: '#d1e7dd', fg: '#0a3622' }; // partial (unavailable day with 0 committed: shouldn't show, but safe)
  if (pct === 100) return { bg: '#fd7e14', fg: 'white' };  // exactly 1 task
  return { bg: '#dc3545', fg: 'white' };                   // >1 task = conflict
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
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:#f8f9fa;border:1px solid #dee2e6"></span>Free</span>
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:#fd7e14"></span>1 task</span>
      <span class="gantt-legend-item"><span class="gantt-swatch" style="background:#dc3545"></span>2+ tasks ⚠</span>
      <span style="color:var(--text-muted);font-size:0.75rem;margin-left:auto">Hover a cell for details</span>
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

    // Fetch availability data for shading; fall back to empty if requests fail
    let windowsByResource = {}, blockoutsByResource = {};
    try {
      const [wList, bList] = await Promise.all([
        Promise.all(resources.map(r => api.get(`/resources/${r.id}/windows`))),
        Promise.all(resources.map(r => api.get(`/resources/${r.id}/blockouts`))),
      ]);
      windowsByResource  = Object.fromEntries(resources.map((r, i) => [r.id, wList[i]]));
      blockoutsByResource = Object.fromEntries(resources.map((r, i) => [r.id, bList[i]]));
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
          <button id="btn-realign" class="btn btn-ghost" style="font-size:0.75rem;padding:0.2rem 0.5rem">↺ Re-align</button>
          <span style="color:var(--text-muted);font-size:0.75rem">Today highlighted blue · hover bar for times</span>
        </span>
      </div>
      <div class="gantt-scroll-wrapper">
        <table class="gantt-table" style="width:${GANTT_LABEL_W + totalHours * SCHED_HOUR_WIDTH}px">
          <thead>${thead}</thead>
          <tbody>${tbody}</tbody>
        </table>
      </div>`;

    body.querySelector('#btn-realign').addEventListener('click', async () => {
      const btn = body.querySelector('#btn-realign');
      btn.disabled = true;
      btn.textContent = 'Re-aligning…';
      try {
        const count = await realignSchedule(resources, tasks, projects);
        if (count === 0) {
          alert('Tasks are already in priority order — no changes needed.');
          btn.disabled = false;
          btn.textContent = '↺ Re-align';
        } else {
          await render();
        }
      } catch (err) {
        alert(`Re-align failed: ${err.message}`);
        btn.disabled = false;
        btn.textContent = '↺ Re-align';
      }
    });
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
