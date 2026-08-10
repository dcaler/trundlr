// ── Helper tab — runner download & setup instructions ─────────────────────

registerView('/helper', async (el) => {
  // Fetch cpu/gpu resources to show IDs
  let computeResources = [];
  try {
    const all = await api.get('/resources/');
    computeResources = all.filter(r => r.kind === 'cpu' || r.kind === 'gpu');
  } catch (_) {}

  const apiUrl = window.location.origin;
  const pre = (code) =>
    `<pre style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:1rem;overflow-x:auto;margin:0.5rem 0 0">${escHtml(code)}</pre>`;

  const resourceTable = computeResources.length === 0
    ? '<p style="color:var(--text-muted)">No cpu/gpu resources defined yet — add them in the Resources tab.</p>'
    : `<table style="margin-top:0.5rem">
        <thead><tr><th>ID</th><th>Name</th><th>Kind</th><th>Capacity</th></tr></thead>
        <tbody>
          ${computeResources.map(r => `
            <tr>
              <td><code style="font-size:1rem;font-weight:700">${escHtml(String(r.id))}</code></td>
              <td>${escHtml(r.name)}</td>
              <td>${escHtml(r.kind)}</td>
              <td>${r.capacity != null ? escHtml(String(r.capacity)) + ' slots' : '—'}</td>
            </tr>`).join('')}
        </tbody>
      </table>`;

  // Generate per-resource screen launch commands (two steps each)
  const screenCmds = computeResources.length === 0
    ? '# (no cpu/gpu resources defined yet)'
    : computeResources.map(r => {
        const slug = escHtml(r.name.toLowerCase().replace(/\s+/g, '_'));
        const name = escHtml(r.name);
        const id   = escHtml(String(r.id));
        const kind = escHtml(r.kind.toUpperCase());
        return `# ${kind} — ${name} (resource ID ${id})

# 1. Start the screen session:
screen -S trundlr_${slug}

# 2. Inside the screen, run the runner (then Ctrl-A D to detach):
RUNNER_RESOURCE_ID=${id} RUNNER_API_URL=${escHtml(apiUrl)} python3 runner.py 2>&1 | tee logs/runner-${slug}.log`;
      }).join('\n\n');

  const attachCmds = computeResources.length === 0
    ? '# (no runners to attach to)'
    : computeResources.map(r => {
        const slug = escHtml(r.name.toLowerCase().replace(/\s+/g, '_'));
        return `screen -r trundlr_${slug}   # attach to ${escHtml(r.name)} runner`;
      }).join('\n');

  el.innerHTML = `
    <h1>Runner — Setup Guide</h1>
    <p style="color:var(--text-muted);max-width:680px">
      The <strong>runner</strong> is a lightweight Python daemon that runs on your compute server
      (e.g. oddjob). Run <strong>one instance per resource</strong> — one for CPU, one for each GPU.
      Each instance watches its own queue, executes shell commands in the project directory, and
      writes results back to Trundlr via the API.
    </p>

    <hr style="margin:1.5rem 0">

    <h2>Step 1 — Resources requiring a runner</h2>
    <p style="color:var(--text-muted)">Each row below needs its own runner process. Note the <strong>ID</strong> for Step 4.</p>
    ${resourceTable}

    <hr style="margin:1.5rem 0">

    <h2>Step 2 — Download runner.py</h2>
    <p style="color:var(--text-muted)">
      Download the script <strong>on this machine</strong> (your browser / Mac).
      It is a single stdlib-only Python file — no <code>pip install</code> needed on the compute server.
    </p>
    <p>
      <a href="/runner.py" download class="btn btn-primary" style="display:inline-block;text-decoration:none">
        ⬇ Download runner.py
      </a>
    </p>

    <hr style="margin:1.5rem 0">

    <h2>Step 3 — Copy runner.py to the compute server</h2>
    <p style="color:var(--text-muted)">
      Run these commands <strong>on your Mac</strong> (in Terminal) to create a folder on the compute
      server and copy the script there. Replace <code>oddjob</code> with your server's hostname or IP.
    </p>
    ${pre(`# On your Mac — open Terminal and run:
scp ~/Downloads/runner.py oddjob:/path/to/runner.py`)}
    <p style="color:var(--text-muted);margin-top:0.75rem">
      Then SSH in and confirm Python 3.8+ is available:
    </p>
    ${pre(`ssh oddjob
python3 --version   # must be 3.8 or later`)}

    <hr style="margin:1.5rem 0">

    <h2>Step 4 — Start a runner for each resource</h2>
    <p style="color:var(--text-muted)">
      Run these commands <strong>on the compute server</strong> (after SSH-ing in).
      Each <code>screen</code> session runs independently in the background and survives logout.
    </p>
    <p style="background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent, #888);border-radius:4px;padding:0.75rem 1rem;max-width:680px">
      <strong>Where it runs:</strong> <code>cd</code> into the directory containing
      <code>runner.py</code> before starting the screen session. The commands above use relative
      paths from that directory. Per-task logs are written to its <code>logs/</code> subdirectory
      (<code>logs/task-{id}.log</code>) — never inside a project's working directory.
    </p>
    ${pre(screenCmds)}
    <p style="background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent, #888);border-radius:4px;padding:0.75rem 1rem;max-width:680px;margin-top:0.75rem">
      <strong>API URL:</strong> the runner connects back to the Trundlr server over the network —
      the app does <strong>not</strong> run on the compute machine, so <code>localhost</code> will
      not work there. The <code>RUNNER_API_URL</code> above is pre-filled with this server's address
      (<code>${escHtml(apiUrl)}</code>) from the page you're viewing. If you launch the runner by
      hand, set <code>RUNNER_API_URL</code> explicitly to that address — don't rely on the
      <code>http://localhost:8251</code> default.
    </p>
    <p style="color:var(--text-muted);margin-top:0.75rem">
      To check on a running runner, re-attach its screen session
      (<kbd>Ctrl-A D</kbd> to detach again):
    </p>
    ${pre(attachCmds)}
    <p style="color:var(--text-muted);margin-top:0.75rem">
      To see all running screen sessions: <code>screen -ls</code>
    </p>

    <hr style="margin:1.5rem 0">

    <h2>Configuration reference</h2>
    <table>
      <thead><tr><th>Variable</th><th>Default</th><th>Description</th></tr></thead>
      <tbody>
        <tr><td><code>RUNNER_RESOURCE_ID</code></td><td><em>required</em></td><td>ID of the resource to manage (Step 1)</td></tr>
        <tr><td><code>RUNNER_API_URL</code></td><td><code>${escHtml(apiUrl)}</code></td><td>Network address of this Trundlr server (not <code>localhost</code> — the app runs on a different machine)</td></tr>
        <tr><td><code>RUNNER_POLL_INTERVAL</code></td><td><code>10</code></td><td>Seconds between polls when queue is empty</td></tr>
        <tr><td><code>RUNNER_LOG_TAIL_LINES</code></td><td><code>100</code></td><td>Lines of output stored in the task record</td></tr>
      </tbody>
    </table>

    <hr style="margin:1.5rem 0">

    <h2>How it works</h2>
    <ol style="line-height:1.9;max-width:640px;color:var(--text-muted)">
      <li>On startup the runner resets any tasks left <code>in_progress</code> from a previous crashed run to <code>failed</code>.</li>
      <li>It polls <code>POST /api/runner/{id}/claim</code> to atomically grab the next <code>todo</code> task, ordered by project priority then scheduled start time.</li>
      <li>The task's <strong>Command</strong> is run via the shell in the project's <strong>Directory</strong> (both set in the Projects tab). The Directory <strong>must be an absolute path that already exists</strong> on the runner — the runner refuses to run (marks the task <code>failed</code>) and never creates the directory, so a command can never execute in an unintended location.</li>
      <li>stdout + stderr are written to <code>logs/task-{id}.log</code> inside the Trundlr directory (default <code>&lt;trundlr&gt;/logs/</code>, configurable via <code>RUNNER_LOG_DIR</code>) — never inside the working directory.</li>
      <li>On completion: status → <code>done</code> or <code>failed</code>, exit code, duration, and the last 100 lines of output are written back to Trundlr and visible in the task detail panel.</li>
    </ol>
  `;
});
