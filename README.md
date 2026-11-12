# trundlr

A lightweight task and resource management app with timeline/calendar views and capacity/utilization tracking. Manages projects, tasks, and a mixed pool of resources (humans, AI agents, CPU nodes, GPU nodes). CPU/GPU tasks can be executed by a runner daemon (`runner.py`), and each resource is exposed as a CalDAV calendar. Ships as a Docker container.

**Stack:** FastAPI + SQLModel/SQLite · Vanilla JS SPA · pytest · Docker

**Version:** `329c8bf` — a content hash of `app/`, computed at startup (see `_compute_version` in `app/main.py`) and also shown at `/api/health`. This value is not manually bumped; it updates automatically whenever the source under `app/` changes.

---

## Quickstart (Docker)

**Prerequisites:** Docker and Docker Compose installed.

```bash
# 1. Clone the repo
git clone <repo-url>
cd trundlr

# 2. Start the app
docker compose up -d

# 3. Open the UI
xdg-open http://localhost:8251

# 4. Browse the interactive API docs
xdg-open http://localhost:8251/docs
```

Data is persisted in a named Docker volume (`trundlr-data`) and survives container restarts.

---

## Quickstart (local dev)

**Prerequisites:** Python 3.11+.

```bash
# 1. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the server
uvicorn app.main:app --reload --port 8251

# 4. Open the UI
xdg-open http://localhost:8251
```

The database file (`trundlr.db`) is created in the working directory on first startup.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///trundlr.db` | SQLAlchemy connection string. Use `sqlite:////app/data/trundlr.db` for a volume-mounted path inside Docker. Switch to a PostgreSQL URL (`postgresql://user:pass@host/db`) with no code changes. |
| `PORT` | `8251` | Host port mapping in docker-compose (`${PORT:-8251}:8251`). |

Set variables in `.env` (loaded automatically by `docker compose`) or pass them on the command line:

```bash
PORT=9000 DATABASE_URL=sqlite:////data/mydb.db docker compose up -d
```

---

## Docker operations

```bash
# Start (builds image if needed)
docker compose up -d

# View logs
docker compose logs -f app

# Restart (data persists)
docker compose restart

# Stop (data persists in volume)
docker compose stop

# Remove containers, keep volume
docker compose down

# Remove containers AND volume (deletes all data)
docker compose down -v
```

**Backups.** Once a day the app snapshots the database to `backups/` next to it (`/app/data/backups/trundlr-YYYYMMDD-HHMMSS.db` in the container), keeping the newest three. The first check runs ten minutes after start, then hourly. Snapshots share the data volume, so they cover a bad edit or a corrupt file, not a lost disk; copy one off the host for that:

```bash
docker cp trundlr-app:/app/data/backups ./trundlr-backups
```

To restore, stop the app, copy a snapshot over `/app/data/trundlr.db` in the volume, and start it again.

---

## Web interface

| URL | Description |
|-----|-------------|
| `/#/projects` | Create/edit projects; add tasks, assign resources and dates inline |
| `/#/resources` | Add resources (human, AI, CPU, GPU). The name or ✎ opens the resource's page, where all editing happens: details, default availability (marked not in use when weekly windows replace it, or for a pool), weekly windows, blockouts, and its calendar blocks (read-only) |
| `/#/schedule` → Timeline | Gantt-style timeline — tasks as bars across a configurable date range |
| `/#/schedule` → Utilization | Per-resource capacity heatmap; over-allocated days flagged with contributing tasks |

---

## API overview

Interactive docs (Swagger UI) are available at **`/docs`** when the app is running. ReDoc is at **`/redoc`**.

### Projects

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/projects/` | List all projects |
| `POST` | `/api/projects/` | Create a project (`name` required) |
| `GET` | `/api/projects/{id}` | Get a project by id |
| `PATCH` | `/api/projects/{id}` | Partial update (name, description) |
| `DELETE` | `/api/projects/{id}` | Delete project and all its tasks |

### Resources

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/resources/` | List all resources |
| `POST` | `/api/resources/` | Create a resource (`name`, `kind` required) |
| `GET` | `/api/resources/{id}` | Get a resource by id |
| `PATCH` | `/api/resources/{id}` | Partial update |
| `DELETE` | `/api/resources/{id}` | Delete resource; tasks are preserved with the assignment removed |

`kind` must be one of `human`, `ai`, `cpu`, or `gpu`.  
Availability defaults to `available_from`–`available_to` (`"09:00"`–`"17:00"`) on `available_days` (bitmask, bit 0 = Mon; default `31` = Mon–Fri). Per-day windows (`/{id}/windows`) replace that when present; blockouts (`/{id}/blockouts`) and CalDAV-painted blocks subtract from it.  
`spans_ids` lists the lanes a resource is built from (e.g. a `GPU 0+1` lane spans `GPU 0` and `GPU 1`). Conflicting lanes never run at once: a runner won't claim while a conflicting lane has a task in progress, the lanes a resource spans stop claiming once its next task is due (`X-Runner-Idle: draining-for:<task>`) and keep holding until it starts, even if a later reflow moves its planned time. Before that, a spanned lane skips work that would still be running at the spanning task's planned start and takes shorter work instead (`X-Runner-Idle: yielding-for:<task>`). Reflow schedules them back to back. `member_ids` makes a resource a **pool** (e.g. `GPU (any)` with members `GPU 0` and `GPU 1`): queue a task on the pool and whichever member lane is free first runs it. A pool has no runner; each member's runner also claims the pool's tasks, in start order with its own, and a claim moves the task onto that lane (`pool_id` remembers the pool, so setting it back to `todo` returns it to the pool). Reflow plans a pool task on the member that opens up first; a pinned pool task keeps its time but not a particular card. Members must be plain cpu/gpu lanes (not pools or spanning lanes); a pool's own availability fields are not used. After a task ends, conflicting lanes also wait `lane_settle_minutes` (Settings, default 5, `X-Runner-Idle: settling:…`) so the model it left loaded on the shared card can unload. With unload before start (below) this can be set to 0.  
`runner_env` (one `KEY=value` per line) and the `runner_folder` setting fill in the Helper page's runner commands, e.g. a GPU lane's `OLLAMA_URL` and `CUDA_VISIBLE_DEVICES`.  
**Unload before start.** A model outlives the task that loaded it (Ollama's `keep_alive`), so a lane can find its card still full from another lane, and Ollama then loads the new model onto the CPU without any error. So each claim reply lists `unload_urls`: the `OLLAMA_URL` (from `runner_env`) of the claimed lane and of every lane sharing its cards (GPU 0 → its own and GPU 0+1's; GPU 0+1 → all three). Before running the command, the runner unloads every model those instances report in `/api/ps` (`keep_alive: 0`) and waits until they report none, writing what it unloaded to the task log. If a card hasn't cleared within `RUNNER_UNLOAD_TIMEOUT` seconds (default 60), the task is not run: it lands in `error` with the reason (e.g. `card still holds llama3.3:70b on http://localhost:11436`). An instance that doesn't answer is skipped. A lane without `OLLAMA_URL` unloads nothing.

### Tasks

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/tasks/` | List tasks, without `log_tail` (get it from `/api/tasks/{id}`); optional `?project_id=`, `?resource_id=`, and `?open_only=true` (leaves out done/failed tasks except those an open task depends on) |
| `POST` | `/api/tasks/` | Create a task (`title`, `project_id` required) |
| `GET` | `/api/tasks/{id}` | Get a task by id |
| `PATCH` | `/api/tasks/{id}` | Partial update — assign/unassign resource, set dates, change status |
| `DELETE` | `/api/tasks/{id}` | Delete a task |

`status` values: `todo`, `in_progress`, `paused`, `blocked`, `done`, `error`, `failed`. A task that fails while running (a runner reporting `failed`, or resetting a stale task) lands in `error`: it stays on the Tasks board and is emailed. Set it to `failed` once dealt with: it then counts as completed and hides with "Show completed", keeping its exit code and log. Only `done` satisfies a dependency; retry by setting the task back to `todo`. The done and error emails go out only when the status changes, so editing a finished task (its duration, say) sends nothing.  
`duration` is total hours; `start_date`/`end_date` are datetimes and `end_date` must not be before `start_date`.  
Assign resources with `resource_ids` (a task may have several). Set dependencies with `depends_on_ids` (a task may have several, and waits until **all** are `done`; re-flow starts it after the last one ends; `[]` clears them; a loop is rejected). Deleting a dependency removes it from every task that waited on it and sets those tasks to `blocked` with `dependency_broken`. The single `depends_on_id` is **deprecated**: on create it adds one dependency, on update it replaces them all with that one (or clears them if null), and replies give the lowest of `depends_on_ids`. Don't send both in one request.  
`POST /api/schedule/reflow` re-places all unpinned `todo` tasks by project priority and dependencies. The board also re-flows when a task is created, deleted, claimed by a runner or finished, and every `auto_reflow_minutes` (Settings, default 60). Ordering: a spanning-lane task its lanes are already draining for goes first; then project priority, where a task gains one level per `age_priority_days` (Settings, default 7, 0 disables) it has been ready to run, up to three levels and never above Critical, sorting after tasks whose project is natively at that level (on upgrade, waits are estimated from when later-created tasks ran); then spanning-lane tasks ahead of single-lane ones. A running task past its planned end is treated as needing at least another hour, so the plan never assumes it has finished. Pin a task to keep a hand-set time.

### Schedule & utilization

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/resources/{id}/schedule` | Per-day committed load, capacity, and utilization % for one resource |
| `GET` | `/api/resources/{id}/conflicts` | Over-allocated days with overage amount and contributing tasks |
| `GET` | `/api/utilization` | Per-day utilization for all resources |

All three endpoints require `?from=YYYY-MM-DD&to=YYYY-MM-DD` query parameters. `from` must not be after `to`.

**Schedule response shape (`/schedule`):**
```json
[
  { "day": "2026-06-01", "committed": 4.0, "capacity": 8.0, "utilization": 50.0 }
]
```

**Conflicts response shape (`/conflicts`):**
```json
[
  {
    "day": "2026-06-02",
    "committed": 10.0,
    "capacity": 8.0,
    "overage": 2.0,
    "tasks": [{ "id": 1, "title": "Task A" }, { "id": 2, "title": "Task B" }]
  }
]
```

**Utilization response shape (`/utilization`):**
```json
[
  {
    "resource_id": 1,
    "resource_name": "Alice",
    "days": [
      { "day": "2026-06-01", "committed": 4.0, "capacity": 8.0, "utilization": 50.0 }
    ]
  }
]
```

---

## Running tests

```bash
# Activate venv first if running locally
source venv/bin/activate

# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_schedule.py -v
```

All 321 tests run against an in-memory SQLite database — no external services needed.

---

## Project layout

```
trundlr/
├── app/
│   ├── main.py          # FastAPI app, lifespan (DB init), static mount
│   ├── models.py        # SQLModel tables: projects, resources, tasks, cycles, windows, blockouts
│   ├── schemas.py       # Pydantic request/response schemas
│   ├── database.py      # Engine, session factory, get_db, startup migrations
│   ├── scheduling.py    # Capacity/utilization engine + conflict detection
│   ├── gantt.py         # Date→pixel mapping for the Gantt timeline
│   ├── email.py         # SMTP notifications
│   ├── validation.py    # Shared id/date-range bounds
│   ├── seed.py          # Demo data seeder
│   ├── routers/
│   │   ├── projects.py
│   │   ├── resources.py
│   │   ├── tasks.py
│   │   ├── schedule.py
│   │   ├── cycles.py    # Reusable task-chain templates
│   │   ├── caldav.py    # CalDAV server (task + block calendars)
│   │   ├── runner.py    # Claim/reset endpoints for runner.py
│   │   └── settings.py
│   └── static/
│       ├── index.html
│       ├── css/style.css
│       └── js/
│           ├── api.js       # Fetch wrapper
│           ├── app.js       # Hash-based router
│           └── views/
│               ├── projects.js, resources.js, schedule.js
│               └── taskboard.js, archive.js, settings.js, helper.js
├── runner.py            # Task execution daemon for cpu/gpu resources
├── extras/ubersicht/    # macOS desktop status widget
├── tests/               # pytest suite (321 tests)
├── Dockerfile
├── docker-compose.yml
├── .env                 # Default env vars for docker-compose
└── requirements.txt
```
