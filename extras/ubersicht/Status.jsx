import { run } from 'uebersicht';

// Set your Trundlr URL here:
const API = 'http://100.87.86.57:8251';

export const command = `curl -sf '${API}/api/projects/' -o /tmp/trundlr_p.json \
  && curl -sf '${API}/api/tasks/' -o /tmp/trundlr_t.json \
  && printf '{"projects":' && cat /tmp/trundlr_p.json \
  && printf ',"tasks":' && cat /tmp/trundlr_t.json \
  && printf '}'`;

export const refreshFrequency = 120000; // 2 minutes

// 🎯 🥇 🥈 🥉 🐌 💤 🛌

export const className = `
  top: 10px;
  left: 10px;
  width: 320px;
  box-sizing: border-box;
  margin: auto;
  padding: 0px 10px 10px;
  background-color: rgba(0, 0, 0, 0.15);
  background-image: url('logo.png');
  background-repeat: no-repeat;
  background-size: 176px 84px;
  background-position: 50% 20px;
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
  1: { badge: 'P1', label: 'On Fire',      bg: '#dc3545', color: 'white' },
  2: { badge: 'P2', label: 'Front Burner', bg: '#fd7e14', color: 'white' },
  3: { badge: 'P3', label: 'Back Burner',  bg: '#ffc107', color: '#333'  },
  4: { badge: 'P4', label: 'Crock Pot',    bg: '#adb5bd', color: '#333'  },
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

  // Compute latest end_date across all tasks per project
  const projectEnd = {};
  for (const t of tasks) {
    if (t.end_date && (!projectEnd[t.project_id] || t.end_date > projectEnd[t.project_id]))
      projectEnd[t.project_id] = t.end_date;
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

  // Sleeping: not archived, but no running or next task
  const activeIds = new Set(active.map(a => a.p.id));
  const sleeping = projects.filter(p => !p.archived && !activeIds.has(p.id));

  // Group by priority
  const byPriority = {};
  for (const item of active) {
    const pri = item.p.priority || 4;
    (byPriority[pri] = byPriority[pri] || []).push(item);
  }

  const blocks = [1, 2, 3, 4].filter(pri => byPriority[pri]).map(pri => (
    <span key={pri}>
      <PriBadge pri={pri} />
      <ul>
        {byPriority[pri].map(({ p, running, lastDone, next }) => {
          const statusTask = running || lastDone;
          const subItems = [];
          const taskLink = {cursor: 'pointer', textDecoration: 'underline', textDecorationColor: 'rgba(204,238,255,0.4)'};
          if (statusTask) {
            const prefix = statusTask.status === 'in_progress' ? '▶' : '✓';
            subItems.push(<li key="s"><span onClick={() => run(`open '${API}/#/tasks/${statusTask.id}'`)} style={taskLink}>{prefix} {statusTask.title}</span></li>);
          }
          if (next) {
            const eta = next.end_date   ? `: ${fmtDate(next.end_date)}`
                      : next.start_date ? `: ${fmtDate(next.start_date)}`
                      :                   '';
            subItems.push(<li key="n"><span onClick={() => run(`open '${API}/#/tasks/${next.id}'`)} style={taskLink}>→ {next.title}{eta}</span></li>);
          }
          return (
            <li key={p.id} style={{marginBottom: '5px'}}>
              <span onClick={() => run(`open '${API}/#/projects/${p.id}'`)} style={{cursor: 'pointer', textDecoration: 'underline', textDecorationColor: 'rgba(204,238,255,0.4)'}}>
                {projectEnd[p.id] ? <span style={{opacity: 0.55, fontStyle: 'italic', marginRight: '4px'}}>(Ends {fmtDate(projectEnd[p.id])})</span> : null}{p.name}
              </span>
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

        {sleeping.length > 0 && (
          <span>
            <em>🛌 Sleeping</em>
            <ul>
              {sleeping.map(p => (
                <li key={p.id} style={{marginBottom: '3px'}}>
                  <span onClick={() => run(`open '${API}/#/projects/${p.id}'`)} style={{cursor: 'pointer', textDecoration: 'underline', textDecorationColor: 'rgba(204,238,255,0.4)'}}>
                    {projectEnd[p.id] ? <span style={{opacity: 0.55, fontStyle: 'italic', marginRight: '4px'}}>(Ends {fmtDate(projectEnd[p.id])})</span> : null}{p.name}
                  </span>
                </li>
              ))}
            </ul>
          </span>
        )}
      </p>
    </div>
  );
};
