// ── Helper tab — runner download & setup instructions ─────────────────────

registerView('/helper', async (el) => {
  // Fetch cpu/gpu resources to show IDs
  let computeResources = [];
  try {
    const all = await api.get('/resources/');
    computeResources = all.filter(r => r.kind === 'cpu' || r.kind === 'gpu');
  } catch (_) {}

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

  const apiUrl = window.location.origin;

  el.innerHTML = `
    <h1>Runner — Setup Guide</h1>
    <p style="color:var(--text-muted);max-width:680px">
      The <strong>runner</strong> is a lightweight Python daemon that runs on any machine with network
      access to Trundlr. It manages the task queue for a single cpu or gpu resource — picking up
      <code>todo</code> tasks, executing their shell commands in the project directory, and writing
      results back via the API.
    </p>

    <hr style="margin:1.5rem 0">

    <h2>1 — Available resources</h2>
    <p style="color:var(--text-muted)">Use the <strong>ID</strong> below as <code>RUNNER_RESOURCE_ID</code>.</p>
    ${resourceTable}

    <hr style="margin:1.5rem 0">

    <h2>2 — Download</h2>
    <p style="color:var(--text-muted)">The runner is a single stdlib-only Python script — no <code>pip install</code> required.</p>
    <p>
      <a href="/runner.py" download class="btn btn-primary" style="display:inline-block;text-decoration:none">
        ⬇ Download runner.py
      </a>
    </p>

    <hr style="margin:1.5rem 0">

    <h2>3 — Installation</h2>
    <p style="color:var(--text-muted)">Copy the file to the machine that will run tasks (e.g. oddjob), then make it executable:</p>
    <pre style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:1rem;overflow-x:auto"># Python 3.8+ required — no additional packages needed
chmod +x runner.py</pre>

    <hr style="margin:1.5rem 0">

    <h2>4 — Configuration &amp; startup</h2>

    <table style="margin-bottom:1rem">
      <thead><tr><th>Variable</th><th>Default</th><th>Description</th></tr></thead>
      <tbody>
        <tr><td><code>RUNNER_RESOURCE_ID</code></td><td><em>required</em></td><td>ID of the resource to manage (see table above)</td></tr>
        <tr><td><code>RUNNER_API_URL</code></td><td><code>${escHtml(apiUrl)}</code></td><td>Base URL of the Trundlr server</td></tr>
        <tr><td><code>RUNNER_POLL_INTERVAL</code></td><td><code>10</code></td><td>Seconds between polls when queue is empty</td></tr>
        <tr><td><code>RUNNER_LOG_TAIL_LINES</code></td><td><code>100</code></td><td>Lines of output stored in the task log</td></tr>
      </tbody>
    </table>

    <pre style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:1rem;overflow-x:auto"># Run in the foreground (replace 3 with your resource ID):
RUNNER_RESOURCE_ID=3 RUNNER_API_URL=${escHtml(apiUrl)} python3 runner.py

# Or export first:
export RUNNER_RESOURCE_ID=3
export RUNNER_API_URL=${escHtml(apiUrl)}
python3 runner.py

# Keep running after logout (detach with screen):
screen -dmS trundlr-runner bash -c "RUNNER_RESOURCE_ID=3 RUNNER_API_URL=${escHtml(apiUrl)} python3 runner.py"
screen -r trundlr-runner   # re-attach to see logs</pre>

    <hr style="margin:1.5rem 0">

    <h2>5 — How it works</h2>
    <ol style="line-height:1.9;max-width:640px;color:var(--text-muted)">
      <li>On startup the runner resets any tasks left <code>in_progress</code> from a previous crashed run to <code>failed</code>.</li>
      <li>It polls <code>POST /api/runner/{id}/claim</code> to atomically grab the next <code>todo</code> task ordered by project priority then scheduled start time.</li>
      <li>The task's <strong>Command</strong> is executed via the shell in the project's <strong>Directory</strong>.</li>
      <li>stdout + stderr are written to <code>{project_directory}/task-{id}.log</code> on the runner's host.</li>
      <li>On completion the runner PATCHes the task: status → <code>done</code> or <code>failed</code>, exit code, duration, and the last ${escHtml(String(100))} lines of output (visible in the task edit panel).</li>
    </ol>
  `;
});
