import { run } from 'uebersicht';

// ─────────────────────────────────────────────────────────────────────────────
// trundlr status widget for Übersicht (macOS desktop overlay)
// https://tracesof.net/uebersicht/
//
// Setup:
//   1. Install Übersicht from https://tracesof.net/uebersicht/
//   2. Copy this file into your Übersicht widgets folder
//      (Übersicht menu → Open widgets folder)
//   3. Set API below to your trundlr instance URL
//   4. The widget refreshes every 2 minutes automatically
//
// The widget shows your trundlr projects grouped by priority, with the current
// or last-completed task and the next scheduled task per project.
// Project names and task bullets are clickable and open trundlr in your browser.
// ─────────────────────────────────────────────────────────────────────────────

const API = 'http://YOUR_TRUNDLR_HOST:8251';

export const command = `curl -sf '${API}/api/projects/' -o /tmp/trundlr_p.json \
  && curl -sf '${API}/api/tasks/' -o /tmp/trundlr_t.json \
  && printf '{"projects":' && cat /tmp/trundlr_p.json \
  && printf ',"tasks":' && cat /tmp/trundlr_t.json \
  && printf '}'`;

export const refreshFrequency = 120000; // 2 minutes

export const className = `
  top: 1%;
  left: 1%;
  width: 320px;
  box-sizing: border-box;
  margin: auto;
  padding: 0px 10px 10px;
  background-color: rgba(0, 0, 0, 0.15);
  -webkit-backdrop-filter: blur(20px);
  font-family: Helvetica Neue;
  font-weight: 300;
  color: #cceeff;
  border: 1px solid #6ab7e5;
  border-radius: 20px;
  text-align: justify;
  line-height: 1em;

  h1 {
    font-size: 20px;
    margin: 16px 0 8px;
  }

  em {
    display: block;
    font-weight: 400;
    font-style: normal;
    margin: 12px 0 2px;
  }

  ul {
    margin: 2px 0;
    padding-left: 14px;
  }
`;

const PRIORITY_LABELS = {
  1: { badge: 'P1', label: 'Code Fixes',    bg: '#dc3545', color: 'white' },
  2: { badge: 'P2', label: 'Top Priority',  bg: '#fd7e14', color: 'white' },
  3: { badge: 'P3', label: 'Back-burner',   bg: '#ffc107', color: '#333'  },
  4: { badge: 'P4', label: 'Slow Progress', bg: '#adb5bd', color: '#333'  },
};

const PriBadge = ({ pri }) => {
  const { badge, label, bg, color } = PRIORITY_LABELS[pri];
  return (
    <em>
      <span style={{background: bg, color, padding: '1px 5px', borderRadius: '3px', fontSize: '0.75em', fontWeight: 600, marginRight: '5px'}}>{badge}</span>
      {label}
    </em>
  );
};

const fmtDate = iso => {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
};

export const render = ({ output }) => {
  let projects = [], tasks = [];
  try {
    const d = JSON.parse(output);
    projects = d.projects || [];
    tasks    = d.tasks    || [];
  } catch (_) {}

  projects.sort((a, b) =>
    (a.priority || 4) - (b.priority || 4) || a.name.localeCompare(b.name)
  );

  const byProject = {};
  for (const t of tasks) {
    (byProject[t.project_id] = byProject[t.project_id] || []).push(t);
  }

  // Build enriched project list, drop fully-complete/empty ones
  const active = projects.map(p => {
    const pt = byProject[p.id] || [];
    const running = pt.find(t => t.status === 'in_progress');
    const lastDone = pt
      .filter(t => t.status === 'done')
      .sort((a, b) => new Date(b.end_date || 0) - new Date(a.end_date || 0))[0];
    const next = pt
      .filter(t => t.status === 'todo')
      .sort((a, b) => {
        if (!a.start_date && !b.start_date) return 0;
        if (!a.start_date) return 1;
        if (!b.start_date) return -1;
        return new Date(a.start_date) - new Date(b.start_date);
      })[0];
    if (!running && !next) return null;
    return { p, running, lastDone, next };
  }).filter(Boolean);

  // Group by priority
  const byPriority = {};
  for (const item of active) {
    const pri = item.p.priority || 4;
    (byPriority[pri] = byPriority[pri] || []).push(item);
  }

  const taskLink = { cursor: 'pointer', textDecoration: 'underline', textDecorationColor: 'rgba(204,238,255,0.4)' };

  const blocks = [1, 2, 3, 4].filter(pri => byPriority[pri]).map(pri => (
    <span key={pri}>
      <PriBadge pri={pri} />
      <ul>
        {byPriority[pri].map(({ p, running, lastDone, next }) => {
          const statusTask = running || lastDone;
          const subItems = [];
          if (statusTask) {
            const prefix = statusTask.status === 'in_progress' ? '▶' : '✓';
            subItems.push(
              <li key="s">
                <span onClick={() => run(`open '${API}/#/tasks'`)} style={taskLink}>{prefix} {statusTask.title}</span>
              </li>
            );
          }
          if (next) {
            const eta = next.end_date   ? ` — ${fmtDate(next.end_date)}`
                      : next.start_date ? ` — ${fmtDate(next.start_date)}`
                      :                   '';
            subItems.push(
              <li key="n">
                <span onClick={() => run(`open '${API}/#/tasks'`)} style={taskLink}>→ {next.title}{eta}</span>
              </li>
            );
          }
          return (
            <li key={p.id} style={{marginBottom: '5px'}}>
              <span onClick={() => run(`open '${API}/#/projects'`)} style={taskLink}>{p.name}</span>
              {subItems.length ? <ul>{subItems}</ul> : null}
            </li>
          );
        })}
      </ul>
    </span>
  ));

  return (
    <div>
      <p>
        {blocks.length
          ? blocks
          : <span style={{color: 'rgba(204,238,255,0.35)', fontSize: '12px'}}>⟳ connecting to trundlr…</span>
        }
      </p>
    </div>
  );
};
