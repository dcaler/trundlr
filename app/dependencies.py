"""Task dependencies: a task waits until every task it depends on is done.

The links live in the TaskDependency join table. These helpers are shared by
the API, the scheduler, the runner claim and CalDAV.
"""

from typing import Iterable, Mapping, Optional

from sqlmodel import Session, select

from app.models import Task, TaskDependency, TaskStatus


def dependency_map(session: Session, task_ids: Optional[Iterable[int]] = None) -> dict[int, list[int]]:
    """task id → sorted ids of the tasks it depends on (only tasks that have any)."""
    stmt = select(TaskDependency)
    if task_ids is not None:
        ids = list(task_ids)
        if not ids:
            return {}
        stmt = stmt.where(TaskDependency.task_id.in_(ids))
    out: dict[int, list[int]] = {}
    for row in session.exec(stmt).all():
        out.setdefault(row.task_id, []).append(row.depends_on_id)
    for deps in out.values():
        deps.sort()
    return out


def dependency_ids(session: Session, task_id: int) -> list[int]:
    return dependency_map(session, [task_id]).get(task_id, [])


def dependency_fields(dep_ids: list[int]) -> dict:
    """Response fields: depends_on_ids, plus the deprecated single
    depends_on_id (the lowest id, or None) kept for older API clients."""
    return {"depends_on_ids": dep_ids, "depends_on_id": dep_ids[0] if dep_ids else None}


def set_dependencies(session: Session, task_id: int, dep_ids: Iterable[int]) -> None:
    for row in session.exec(select(TaskDependency).where(TaskDependency.task_id == task_id)).all():
        session.delete(row)
    session.flush()
    for dep in sorted(set(dep_ids)):
        session.add(TaskDependency(task_id=task_id, depends_on_id=dep))


def would_cycle(session: Session, task_id: int, dep_ids: Iterable[int]) -> bool:
    """True if making task_id depend on dep_ids would create a loop, i.e.
    task_id is reachable from any of dep_ids by following dependencies."""
    graph = dependency_map(session)
    stack, seen = list(dep_ids), set()
    while stack:
        cur = stack.pop()
        if cur == task_id:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(graph.get(cur, []))
    return False


def unmet_dependencies(dep_ids: Iterable[int], tasks_by_id: Mapping[int, Task]) -> list[int]:
    """The dependencies that are not done (a missing task counts as not done)."""
    return [d for d in dep_ids
            if (t := tasks_by_id.get(d)) is None or t.status != TaskStatus.done]


def unlink_deleted_task(session: Session, task_id: int) -> list[Task]:
    """Remove every dependency link to or from task_id before it is deleted.

    Tasks that depended on it are forced to blocked and flagged
    dependency_broken, so someone reviews them rather than the scheduler
    silently running them without the deleted step. Returns those tasks.
    """
    dependents: list[Task] = []
    rows = session.exec(select(TaskDependency).where(
        (TaskDependency.depends_on_id == task_id) | (TaskDependency.task_id == task_id)
    )).all()
    for row in rows:
        if row.depends_on_id == task_id:
            t = session.get(Task, row.task_id)
            if t is not None:
                t.status = TaskStatus.blocked
                t.dependency_broken = True
                session.add(t)
                dependents.append(t)
        session.delete(row)
    session.flush()
    return dependents
