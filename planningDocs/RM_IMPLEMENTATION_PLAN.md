# trundlr — Implementation Plan

A lightweight task & resource management app: web UI + REST API, manages projects, tasks, and a mixed pool of resources (humans + CPU/GPU records, tracked manually), with **timeline/calendar views** and **capacity/utilization tracking**. Ships in a Docker container.

**Stack:** FastAPI + SQLModel/SQLite, vanilla JS or lightweight frontend, pytest, Docker.

---

## How tasks are assigned to models

Each step below is tagged with the recommended Claude model. The logic balances capability against cost (API list prices, May 2026: Haiku 4.5 **$1/$5**, Sonnet 4.6 **$3/$15**, Opus 4.7 **$5/$25** per million input/output tokens).

| Model | Use it for | Why |
|-------|-----------|-----|
| **Haiku** | Boilerplate, scaffolding, config files, mechanical test stubs, repetitive CRUD | Cheapest; near-frontier on well-specified, low-ambiguity work |
| **Sonnet** | Most application code — endpoints, business logic, frontend, integration tests | Best balance of quality and cost; the workhorse default |
| **Opus** | Genuinely hard design calls: the scheduling/capacity engine, conflict-detection algorithm, data-model decisions with downstream impact | Reserve the premium model for work where a subtle mistake is expensive to unwind |

Rule of thumb applied below: **default to Sonnet, drop to Haiku when the task is mechanical, escalate to Opus only when the design is load-bearing.**

---

## Phase 0 — Project scaffolding



### Step 0.1 — Repo structure & dependencies — **Haiku**
Create directory layout, `requirements.txt` (fastapi, uvicorn, sqlmodel, pytest, httpx), `.gitignore`, empty package files.
- **Test:** `pip install -r requirements.txt` succeeds; `python -c "import fastapi, sqlmodel"` exits 0.


### Step 0.2 — App entrypoint & health check — **Haiku**
Minimal FastAPI app with `GET /health` returning `{"status": "ok"}`.
- **Test:** `pytest` — assert `GET /health` returns 200 and correct JSON.

---

## Phase 1 — Data layer

### Step 1.1 — Core data model — **Opus**
Define `Project`, `Resource` (kind: human/cpu/gpu, capacity), `Task` (status, start/end dates, load, FK to project + resource). The capacity semantics (hours/day for humans vs parallel slots for compute) are the foundation everything else rests on — get this right once.
- **Test:** create one of each entity in an in-memory SQLite session; assert relationships resolve and constraints hold (e.g. task requires a valid project_id).


### Step 1.2 — DB engine, session, migrations-on-startup — **Haiku**
Engine setup, `create_db_and_tables()`, FastAPI dependency that yields a session.
- **Test:** startup creates tables in a temp DB file; session dependency yields a working session.


### Step 1.3 — Seed/fixture data — **Haiku**
Script inserting sample projects, a few humans, a CPU node and a GPU node, and demo tasks.
- **Test:** run seed against temp DB; assert expected row counts per table.

---

## Phase 2 — REST API (CRUD)

read @planningDocs/RM_IMPLEMENTATION_PLAN.md
read the end of @planningDocs/build_log.md
do the following step and note the results in planningDocs/build_log.md
### Step 2.1 — Projects endpoints — **Sonnet**
`GET/POST/PATCH/DELETE /api/projects`. Pydantic request/response schemas.
- **Test:** full CRUD round-trip via `httpx` test client; 404 on missing id; 422 on bad payload.


### Step 2.2 — Resources endpoints — **Sonnet**
CRUD for resources incl. `kind` and `capacity` validation (capacity > 0).
- **Test:** CRUD round-trip; reject invalid `kind`; reject capacity ≤ 0.

### Step 2.3 — Tasks endpoints — **Sonnet**
CRUD for tasks; assign/unassign resource; set schedule window; status transitions.
- **Test:** create task under project; assign to resource; reject end_date < start_date; reject assignment to nonexistent resource.

---

## Phase 3 — Scheduling & capacity engine (the core differentiator)

### Step 3.1 — Capacity/utilization calculation — **Opus**
Given a resource and a date range, compute daily committed load (sum of overlapping task loads) vs capacity, and a utilization percentage. Must correctly handle humans (hours) and compute (parallel slots) under one interface.
- **Test:** table-driven cases — single task, overlapping tasks, partial-day-range overlaps, zero-task days; assert exact utilization numbers. Edge cases: task spanning month boundary, open-ended task (no end_date).


### Step 3.2 — Over-allocation / conflict detection — **Opus**
Flag any day where committed load exceeds capacity; report which tasks contribute. This is the algorithm users will trust or distrust — worth the premium model.
- **Test:** construct an overbooked GPU (3 tasks needing 2 slots each on a 4-slot node) and assert the right days + contributing tasks are flagged; assert a fully-booked-but-not-over case is NOT flagged (off-by-one guard).


### Step 3.3 — Schedule API endpoints — **Sonnet**
`GET /api/resources/{id}/schedule?from=&to=` and `GET /api/utilization?from=&to=` returning per-resource, per-day data for the frontend.
- **Test:** endpoint returns correctly shaped JSON matching the engine's output; date-range validation.

---

## Phase 4 — Web interface

### Step 4.1 — App shell, routing, API client — **Sonnet**
Base layout, navigation (Projects / Resources / Schedule), a small fetch wrapper.
- **Test:** smoke test that `/` serves HTML 200 and static assets load; (optional) Playwright check that nav renders.

### Step 4.2 — Project & task management views — **Sonnet**
Create/edit projects, board or list of tasks per project, assign resource + set dates inline.
- **Test:** Playwright: create a project, add a task, assign it — assert it persists via the API.

### Step 4.3 — Timeline / calendar view — **Sonnet**
Gantt-style per-resource timeline across a date range; tasks render as bars in their windows.
- **Test:** Playwright: seed a known task and assert a bar appears in the correct row/column; (logic) assert date→pixel mapping function with unit tests.

### Step 4.4 — Utilization dashboard — **Sonnet**
Per-resource capacity bars / heatmap; over-allocated days visually flagged (ties to 3.2).
- **Test:** render against seeded overbooked resource; assert the flagged day shows the warning state.

---

## Phase 5 — Packaging & delivery


### Step 5.1 — Dockerfile & .dockerignore — **Haiku**
Multi-stage or slim single-stage image; runs uvicorn; exposes port; persists SQLite via volume.
- **Test:** `docker build` succeeds; container starts and `GET /health` responds from the host.

### Step 5.2 — docker-compose & env config — **Haiku**
Compose file with volume mount for the DB and configurable port/env.
- **Test:** `docker compose up` brings the app to a healthy state; data survives a container restart.

### Step 5.3 — README & API docs — **Sonnet**
Quickstart, env vars, API overview (FastAPI auto-generates `/docs`).
- **Test:** follow the README from scratch on a clean checkout and reach a running app (manual gate).

---

## Phase 6 — Hardening (recommended before real use)

### Step 6.1 — End-to-end test suite — **Sonnet**
Full-flow test: create project → resources → tasks → query schedule → detect conflict.
- **Test:** the E2E test itself, run in CI against the built container.

### Step 6.2 — CI pipeline — **Haiku**
GitHub Actions: install, lint, run pytest, build Docker image on push.
- **Test:** pipeline goes green on a clean commit.

### Step 6.3 — Input validation & error-handling audit — **Opus**
Review boundary conditions across the scheduling engine and API surface; consistent error responses; guard against malformed date ranges and capacity edge cases. A focused correctness pass where subtle gaps are costly.
- **Test:** fuzz/parametrized tests throwing malformed payloads and extreme date ranges; assert no 500s and sane 4xx responses.

---

## Summary of model assignments

| Phase | Opus | Sonnet | Haiku |
|-------|:----:|:------:|:-----:|
| 0 Scaffolding | | | 0.1, 0.2 |
| 1 Data layer | 1.1 | | 1.2, 1.3 |
| 2 REST CRUD | | 2.1, 2.2, 2.3 | |
| 3 Scheduling engine | 3.1, 3.2 | 3.3 | |
| 4 Web UI | | 4.1, 4.2, 4.3, 4.4 | |
| 5 Packaging | | 5.3 | 5.1, 5.2 |
| 6 Hardening | 6.3 | 6.1 | 6.2 |

**Totals:** Opus ×4 (the load-bearing design + correctness work), Sonnet ×9 (the bulk of application code), Haiku ×6 (mechanical scaffolding & config).

This keeps spend concentrated on Sonnet (the cost-effective default), pushes cheap mechanical work to Haiku, and spends Opus tokens only where a subtle error in the scheduling/capacity logic would be expensive to discover later.
