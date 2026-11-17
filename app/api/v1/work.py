"""Tasks and case notes (internal / team / client visibility)."""

import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select

from app.api.deps import DB, Staff, TenantUser, audit, get_staff_case, get_tenant_member, get_visible_case, is_manager
from app.models import Note, Task
from app.models.enums import NoteVisibility, Role
from app.schemas import NoteIn, NoteOut, NoteUpdate, Page, TaskIn, TaskOut, TaskUpdate

router = APIRouter(tags=["tasks & notes"])


# ------------------------------------------------------------------ tasks
async def _get_task(db, user, task_id) -> Task:
    task = await db.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise HTTPException(404, "Task not found")
    if user.role == Role.JUNIOR_ADVOCATE and user.id not in (task.assigned_to_id, task.created_by_id):
        raise HTTPException(404, "Task not found")
    return task


@router.post("/tasks", response_model=TaskOut, status_code=201)
async def create_task(body: TaskIn, user: Staff, db: DB, request: Request):
    if body.case_id:
        await get_staff_case(db, user, body.case_id)
    if body.assigned_to_id:
        assignee = await get_tenant_member(db, user.tenant_id, body.assigned_to_id)
        if assignee.role == Role.CLIENT:
            raise HTTPException(422, "Tasks cannot be assigned to clients")
        if user.role == Role.JUNIOR_ADVOCATE and assignee.id != user.id:
            raise HTTPException(403, "Junior advocates can only assign tasks to themselves")
    task = Task(tenant_id=user.tenant_id, created_by_id=user.id, **body.model_dump())
    db.add(task)
    await db.flush()
    await audit(db, user, "task.created", "task", task.id, request=request)
    return task


@router.get("/tasks", response_model=Page[TaskOut])
async def list_tasks(
    user: Staff,
    db: DB,
    case_id: uuid.UUID | None = None,
    assigned_to_id: uuid.UUID | None = None,
    status: str | None = None,
    mine: bool = False,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    stmt = select(Task).where(Task.tenant_id == user.tenant_id)
    if user.role == Role.JUNIOR_ADVOCATE or mine:
        stmt = stmt.where((Task.assigned_to_id == user.id) | (Task.created_by_id == user.id))
    if case_id:
        stmt = stmt.where(Task.case_id == case_id)
    if assigned_to_id:
        stmt = stmt.where(Task.assigned_to_id == assigned_to_id)
    if status:
        stmt = stmt.where(Task.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = (await db.execute(stmt.order_by(Task.due_at.asc().nulls_last(), Task.created_at).limit(limit).offset(offset))).scalars().all()
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(task_id: uuid.UUID, body: TaskUpdate, user: Staff, db: DB, request: Request):
    task = await _get_task(db, user, task_id)
    changes = body.model_dump(exclude_unset=True)
    if user.role == Role.JUNIOR_ADVOCATE and set(changes) - {"status", "description"} and task.created_by_id != user.id:
        raise HTTPException(403, "Junior advocates can only update status on tasks assigned to them")
    if changes.get("assigned_to_id"):
        assignee = await get_tenant_member(db, user.tenant_id, changes["assigned_to_id"])
        if assignee.role == Role.CLIENT:
            raise HTTPException(422, "Tasks cannot be assigned to clients")
    for k, v in changes.items():
        setattr(task, k, v)
    await audit(db, user, "task.updated", "task", task.id, {"fields": sorted(changes)}, request)
    await db.flush()
    return task


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: uuid.UUID, user: Staff, db: DB, request: Request):
    task = await _get_task(db, user, task_id)
    if not is_manager(user) and task.created_by_id != user.id:
        raise HTTPException(403, "Not allowed")
    await audit(db, user, "task.deleted", "task", task.id, request=request)
    await db.delete(task)


# ------------------------------------------------------------------ notes
def _visible_levels(user) -> set[str]:
    if is_manager(user):
        return {v.value for v in NoteVisibility}
    if user.role == Role.JUNIOR_ADVOCATE:
        return {NoteVisibility.TEAM, NoteVisibility.CLIENT}
    return {NoteVisibility.CLIENT}


@router.get("/cases/{case_id}/notes", response_model=list[NoteOut])
async def list_notes(case_id: uuid.UUID, user: TenantUser, db: DB):
    await get_visible_case(db, user, case_id)
    stmt = select(Note).where(Note.case_id == case_id, Note.visibility.in_(_visible_levels(user)))
    return (await db.execute(stmt.order_by(Note.created_at.desc()))).scalars().all()


@router.post("/cases/{case_id}/notes", response_model=NoteOut, status_code=201)
async def add_note(case_id: uuid.UUID, body: NoteIn, user: Staff, db: DB, request: Request):
    case = await get_staff_case(db, user, case_id)
    if body.visibility not in _visible_levels(user):
        raise HTTPException(403, "You cannot create notes with this visibility")
    note = Note(tenant_id=case.tenant_id, case_id=case.id, body=body.body, visibility=body.visibility, author_id=user.id)
    db.add(note)
    await db.flush()
    await audit(db, user, "note.created", "note", note.id, {"case_id": str(case.id), "visibility": note.visibility}, request)
    return note


async def _own_note(db, user, note_id) -> Note:
    note = await db.get(Note, note_id)
    if note is None or note.tenant_id != user.tenant_id or note.visibility not in _visible_levels(user):
        raise HTTPException(404, "Note not found")
    await get_staff_case(db, user, note.case_id)
    if note.author_id != user.id and user.role != Role.FIRM_ADMIN:
        raise HTTPException(403, "Only the author or a firm admin can change this note")
    return note


@router.patch("/notes/{note_id}", response_model=NoteOut)
async def update_note(note_id: uuid.UUID, body: NoteUpdate, user: Staff, db: DB, request: Request):
    note = await _own_note(db, user, note_id)
    changes = body.model_dump(exclude_unset=True)
    if "visibility" in changes and changes["visibility"] not in _visible_levels(user):
        raise HTTPException(403, "You cannot set this visibility")
    for k, v in changes.items():
        setattr(note, k, v)
    await audit(db, user, "note.updated", "note", note.id, request=request)
    await db.flush()
    return note


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(note_id: uuid.UUID, user: Staff, db: DB, request: Request):
    note = await _own_note(db, user, note_id)
    await audit(db, user, "note.deleted", "note", note.id, request=request)
    await db.delete(note)
