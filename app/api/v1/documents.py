"""Case documents (PRD 3.9). Clients only see documents explicitly shared with them."""

import hashlib
import mimetypes
import re
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from app.api.deps import DB, Staff, TenantUser, audit, get_staff_case, get_visible_case
from app.core.config import settings
from app.models import Document
from app.models.enums import DocumentCategory, Role
from app.providers.storage import get_storage
from app.schemas import DocumentOut, DocumentUpdate
from app.services.notify import notify_users
from app.services.sync import client_ids_for_case

router = APIRouter(tags=["documents"])

ALLOWED_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain",
}


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ()]", "_", name or "file").strip()
    return name[:200] or "file"


async def _get_doc(db, user, doc_id) -> Document:
    doc = await db.get(Document, doc_id)
    if doc is None or doc.tenant_id != user.tenant_id:
        raise HTTPException(404, "Document not found")
    await get_visible_case(db, user, doc.case_id)
    if user.role == Role.CLIENT and not doc.shared_with_client:
        raise HTTPException(404, "Document not found")
    return doc


@router.post("/cases/{case_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    case_id: uuid.UUID,
    user: Staff,
    db: DB,
    request: Request,
    file: UploadFile = File(...),
    category: DocumentCategory = Form(DocumentCategory.OTHER),
    description: str | None = Form(None),
    shared_with_client: bool = Form(False),
):
    case = await get_staff_case(db, user, case_id)
    content_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(415, f"File type {content_type} is not allowed")
    data = await file.read(settings.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(data) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.MAX_UPLOAD_MB} MB")
    if not data:
        raise HTTPException(422, "Empty file")
    doc_id = uuid.uuid4()
    filename = _safe_name(file.filename)
    key = f"{case.tenant_id}/{case.id}/{doc_id}/{filename}"
    await get_storage().put(key, data, content_type)
    doc = Document(
        id=doc_id,
        tenant_id=case.tenant_id,
        case_id=case.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        storage_key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        category=category,
        description=description,
        shared_with_client=shared_with_client,
        uploaded_by_id=user.id,
    )
    db.add(doc)
    if shared_with_client:
        await notify_users(
            db,
            await client_ids_for_case(db, case.id),
            "document_shared",
            "document.shared.title",
            "document.shared.body",
            {"filename": filename, "case_title": case.title},
            case.id,
        )
    await audit(db, user, "document.uploaded", "document", doc.id, {"case_id": str(case.id), "filename": filename}, request)
    await db.flush()
    return doc


@router.get("/cases/{case_id}/documents", response_model=list[DocumentOut])
async def list_documents(case_id: uuid.UUID, user: TenantUser, db: DB, category: DocumentCategory | None = None):
    case = await get_visible_case(db, user, case_id)
    stmt = select(Document).where(Document.case_id == case.id)
    if user.role == Role.CLIENT:
        stmt = stmt.where(Document.shared_with_client.is_(True))
    if category:
        stmt = stmt.where(Document.category == category)
    return (await db.execute(stmt.order_by(Document.created_at.desc()))).scalars().all()


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def get_document(doc_id: uuid.UUID, user: TenantUser, db: DB):
    return await _get_doc(db, user, doc_id)


@router.get("/documents/{doc_id}/download")
async def download_document(doc_id: uuid.UUID, user: TenantUser, db: DB, request: Request):
    doc = await _get_doc(db, user, doc_id)
    data = await get_storage().get(doc.storage_key)
    await audit(db, user, "document.downloaded", "document", doc.id, request=request)
    return Response(
        content=data,
        media_type=doc.content_type,
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'},
    )


@router.patch("/documents/{doc_id}", response_model=DocumentOut)
async def update_document(doc_id: uuid.UUID, body: DocumentUpdate, user: Staff, db: DB, request: Request):
    doc = await _get_doc(db, user, doc_id)
    changes = body.model_dump(exclude_unset=True)
    newly_shared = changes.get("shared_with_client") and not doc.shared_with_client
    for k, v in changes.items():
        setattr(doc, k, v)
    if newly_shared:
        case = await get_visible_case(db, user, doc.case_id)
        await notify_users(
            db,
            await client_ids_for_case(db, doc.case_id),
            "document_shared",
            "document.shared.title",
            "document.shared.body",
            {"filename": doc.filename, "case_title": case.title},
            doc.case_id,
        )
    await audit(db, user, "document.updated", "document", doc.id, {"fields": sorted(changes)}, request)
    await db.flush()
    return doc


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: uuid.UUID, user: Staff, db: DB, request: Request):
    doc = await _get_doc(db, user, doc_id)
    if user.role == Role.JUNIOR_ADVOCATE and doc.uploaded_by_id != user.id:
        raise HTTPException(403, "Junior advocates can only delete their own uploads")
    await get_storage().delete(doc.storage_key)
    await audit(db, user, "document.deleted", "document", doc.id, {"filename": doc.filename}, request)
    await db.delete(doc)
