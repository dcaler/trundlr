// ── Archive view ───────────────────────────────────────────────────────────

async function showArchive(el) {
  el.innerHTML = '<p class="loading">Loading…</p>';
  const projects = await api.get('/projects/?archived=true');

  const rows = projects.map(p => `<tr>
    <td style="font-weight:600">${escHtml(p.name)}</td>
    <td style="color:var(--text-muted)">${escHtml(p.folder || '—')}</td>
    <td style="color:var(--text-muted)">${escHtml(p.description || '—')}</td>
    <td style="white-space:nowrap;text-align:right">
      <button class="btn btn-ghost restore-project-btn" data-id="${p.id}" title="Restore to active projects">↩ Restore</button>
      <button class="btn btn-danger delete-project-btn" data-id="${p.id}">✕</button>
    </td>
  </tr>`).join('');

  el.innerHTML = `
    <h1>Archive</h1>
    <p style="color:var(--text-muted);margin-bottom:1.5rem">Completed projects. Restore a project to move it back to active.</p>

    ${projects.length === 0
      ? '<p style="color:var(--text-muted)">No archived projects yet.</p>'
      : `<table>
          <thead><tr>
            <th>Name</th><th>Folder</th><th>Description</th><th style="width:160px"></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`}
  `;

  el.querySelectorAll('.restore-project-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      try {
        await api.post(`/projects/${btn.dataset.id}/unarchive`, {});
        await showArchive(el);
      } catch (err) { alert(`Error: ${err.message}`); }
    })
  );

  el.querySelectorAll('.delete-project-btn').forEach(btn =>
    btn.addEventListener('click', async () => {
      if (!confirm('Permanently delete this project and all its tasks?')) return;
      try {
        await api.delete(`/projects/${btn.dataset.id}`);
        await showArchive(el);
      } catch (err) { alert(`Error: ${err.message}`); }
    })
  );
}

registerView('/archive', async (el) => {
  await showArchive(el);
});
