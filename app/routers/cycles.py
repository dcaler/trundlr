import re

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.database import get_db
from app.models import (
    CycleStep,
    CycleStepResource,
    CycleTemplate,
    Project,
    Resource,
    Task,
    TaskResource,
    TaskStatus,
)
from app.schemas import (
    CycleInstantiate,
    CycleStepCreate,
    CycleStepRead,
    CycleStepUpdate,
    CycleTemplateCreate,
    CycleTemplateRead,
    CycleTemplateUpdate,
    TaskRead,
)
from app.validation import DBId

router = APIRouter(prefix="/api/cycle-templates", tags=["cycles"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _step_resource_ids(step_id: int, session: Session) -> list[int]:
    return list(session.exec(
        select(CycleStepResource.resource_id).where(CycleStepResource.step_id == step_id)
    ).all())


def _set_step_resources(step_id: int, resource_ids: list[int], session: Session) -> None:
    for sr in session.exec(
        select(CycleStepResource).where(CycleStepResource.step_id == step_id)
    ).all():
        session.delete(sr)
    for rid in resource_ids:
        session.add(CycleStepResource(step_id=step_id, resource_id=rid))


def _step_read(step: CycleStep, session: Session) -> CycleStepRead:
    return CycleStepRead(
        **step.model_dump(), resource_ids=_step_resource_ids(step.id, session)
    )


def _template_read(template: CycleTemplate, session: Session) -> CycleTemplateRead:
    steps = session.exec(
        select(CycleStep)
        .where(CycleStep.template_id == template.id)
        .order_by(CycleStep.position, CycleStep.id)
    ).all()
    return CycleTemplateRead(
        id=template.id,
        name=template.name,
        steps=[_step_read(s, session) for s in steps],
    )


def _get_template_or_404(template_id: int, session: Session) -> CycleTemplate:
    template = session.get(CycleTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Cycle template not found")
    return template


def _get_step_or_404(step_id: int, session: Session) -> CycleStep:
    step = session.get(CycleStep, step_id)
    if not step:
        raise HTTPException(status_code=404, detail="Cycle step not found")
    return step


def _validate_resources(resource_ids: list[int], session: Session) -> None:
    for rid in resource_ids:
        if not session.get(Resource, rid):
            raise HTTPException(status_code=404, detail=f"Resource {rid} not found")


# ── templates ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[CycleTemplateRead])
def list_templates(session: Session = Depends(get_db)):
    templates = session.exec(
        select(CycleTemplate).order_by(CycleTemplate.name)
    ).all()
    return [_template_read(t, session) for t in templates]


@router.post("/", response_model=CycleTemplateRead, status_code=201)
def create_template(data: CycleTemplateCreate, session: Session = Depends(get_db)):
    template = CycleTemplate(name=data.name)
    session.add(template)
    session.commit()
    session.refresh(template)
    return _template_read(template, session)


@router.patch("/{template_id}", response_model=CycleTemplateRead)
def update_template(
    data: CycleTemplateUpdate, template_id: int = DBId(), session: Session = Depends(get_db)
):
    template = _get_template_or_404(template_id, session)
    template.name = data.name
    session.add(template)
    session.commit()
    session.refresh(template)
    return _template_read(template, session)


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: int = DBId(), session: Session = Depends(get_db)):
    template = _get_template_or_404(template_id, session)
    steps = session.exec(
        select(CycleStep).where(CycleStep.template_id == template_id)
    ).all()
    for step in steps:
        _set_step_resources(step.id, [], session)
        session.delete(step)
    session.flush()
    session.delete(template)
    session.commit()


# ── steps ─────────────────────────────────────────────────────────────────────

@router.post("/{template_id}/steps", response_model=CycleStepRead, status_code=201)
def add_step(
    data: CycleStepCreate, template_id: int = DBId(), session: Session = Depends(get_db)
):
    _get_template_or_404(template_id, session)
    _validate_resources(data.resource_ids, session)
    step = CycleStep(
        template_id=template_id,
        title=data.title,
        duration=data.duration,
        command=data.command,
        position=data.position,
    )
    session.add(step)
    session.flush()
    _set_step_resources(step.id, data.resource_ids, session)
    session.commit()
    session.refresh(step)
    return _step_read(step, session)


@router.patch("/steps/{step_id}", response_model=CycleStepRead)
def update_step(
    data: CycleStepUpdate, step_id: int = DBId(), session: Session = Depends(get_db)
):
    step = _get_step_or_404(step_id, session)
    updates = data.model_dump(exclude_unset=True)
    if "resource_ids" in updates:
        _validate_resources(updates["resource_ids"], session)
        _set_step_resources(step.id, updates.pop("resource_ids"), session)
    for key, value in updates.items():
        setattr(step, key, value)
    session.add(step)
    session.commit()
    session.refresh(step)
    return _step_read(step, session)


@router.delete("/steps/{step_id}", status_code=204)
def delete_step(step_id: int = DBId(), session: Session = Depends(get_db)):
    step = _get_step_or_404(step_id, session)
    _set_step_resources(step.id, [], session)
    session.flush()
    session.delete(step)
    session.commit()


# ── instantiation ─────────────────────────────────────────────────────────────

def _next_cycle_number(step_titles: list[str], project_id: int, session: Session) -> int:
    """Highest trailing integer already used by any of these step titles in the
    project, + 1. New cycles share one number so steps stay aligned.

    A task counts as "<title> <n>" only when it is exactly the step title
    followed by a space and an integer (e.g. "Draft 3").
    """
    existing = session.exec(
        select(Task.title).where(Task.project_id == project_id)
    ).all()
    highest = 0
    patterns = [re.compile(rf"^{re.escape(t)} (\d+)$") for t in step_titles]
    for title in existing:
        for pat in patterns:
            m = pat.match(title)
            if m:
                highest = max(highest, int(m.group(1)))
                break
    return highest + 1


@router.post("/{template_id}/instantiate", response_model=List[TaskRead], status_code=201)
def instantiate_cycle(
    data: CycleInstantiate, template_id: int = DBId(), session: Session = Depends(get_db)
):
    template = _get_template_or_404(template_id, session)
    if not session.get(Project, data.project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    steps = session.exec(
        select(CycleStep)
        .where(CycleStep.template_id == template_id)
        .order_by(CycleStep.position, CycleStep.id)
    ).all()
    if not steps:
        raise HTTPException(status_code=422, detail="Cycle template has no steps")

    n = _next_cycle_number([s.title for s in steps], data.project_id, session)

    created: list[Task] = []
    prev_id: int | None = None
    for step in steps:
        task = Task(
            title=f"{step.title} {n}",
            duration=step.duration,
            command=step.command,
            status=TaskStatus.todo,
            project_id=data.project_id,
            depends_on_id=prev_id,  # chain each step to the previous; first is None
        )
        session.add(task)
        session.flush()
        for rid in _step_resource_ids(step.id, session):
            session.add(TaskResource(task_id=task.id, resource_id=rid))
        prev_id = task.id
        created.append(task)

    session.commit()
    for t in created:
        session.refresh(t)
    return [
        TaskRead(
            **t.model_dump(),
            resource_ids=list(session.exec(
                select(TaskResource.resource_id).where(TaskResource.task_id == t.id)
            ).all()),
        )
        for t in created
    ]
