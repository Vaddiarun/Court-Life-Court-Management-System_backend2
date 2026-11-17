"""Metadata, provider webhooks and operator job triggers."""

import hmac

from fastapi import APIRouter, Form, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.api.deps import DB, SuperAdmin
from app.core.config import settings
from app.i18n import LANGUAGES, translations_available
from app.models.enums import Channel, DocumentCategory, ReminderType, Role
from app.providers.court.base import is_valid_cnr, normalize_cnr
from app.services.reminders import dispatch_due, handle_call_status
from app.services.sync import collect_refreshed, mark_cnr_changed, request_refreshes

router = APIRouter(tags=["system"])


def _check_token(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, settings.WEBHOOK_SECRET):
        raise HTTPException(401, "Invalid webhook token")


@router.get("/meta/languages")
async def languages():
    have = translations_available()
    return [{**lang, "notification_templates": lang["code"] in have} for lang in LANGUAGES]


@router.get("/meta/enums")
async def enums():
    return {
        "roles": [r.value for r in Role],
        "reminder_types": [r.value for r in ReminderType],
        "channels": [c.value for c in Channel],
        "document_categories": [c.value for c in DocumentCategory],
        "default_reminder_offsets_days": [7, 3, 1, 0],
    }


@router.post("/webhooks/exotel/call-status")
async def exotel_call_status(
    db: DB,
    token: str = Query(...),
    CallSid: str = Form(...),
    Status: str = Form(...),
    ConversationDuration: int | None = Form(None),
):
    _check_token(token)
    found = await handle_call_status(db, CallSid, Status, ConversationDuration)
    return {"ok": True, "matched": found}


@router.get("/webhooks/exotel/greeting", response_class=PlainTextResponse)
async def exotel_greeting(token: str = Query(...), CustomField: str = Query("")):
    """Exotel flow 'Greeting' applet (dynamic text) fetches this URL; we return the
    reminder text to speak. CustomField is 'reference|language|message' as set when
    the call was placed."""
    _check_token(token)
    parts = CustomField.split("|", 2)
    text = parts[2] if len(parts) == 3 else ""
    return text or "This is a reminder from Court Life. Please check the app for details."


@router.post("/webhooks/twilio/call-status")
async def twilio_call_status(
    db: DB,
    token: str = Query(...),
    CallSid: str = Form(...),
    CallStatus: str = Form(...),
    CallDuration: int | None = Form(None),
):
    """Twilio StatusCallback (completed / busy / no-answer / failed / canceled)."""
    _check_token(token)
    status_ = "failed" if CallStatus == "canceled" else CallStatus
    found = await handle_call_status(db, CallSid, status_, CallDuration)
    return {"ok": True, "matched": found}


class CourtWebhookIn(BaseModel):
    cnr: str


@router.post("/webhooks/court-provider")
async def court_provider_webhook(body: CourtWebhookIn, db: DB, token: str = Query(...)):
    """Portfolio-monitoring callback: the provider tells us a CNR changed."""
    _check_token(token)
    cnr = normalize_cnr(body.cnr)
    if not is_valid_cnr(cnr):
        raise HTTPException(422, "Invalid CNR")
    return {"ok": True, "cases_marked": await mark_cnr_changed(db, cnr)}


@router.post("/admin/jobs/sync")
async def run_sync_now(admin: SuperAdmin, db: DB):
    """Operator trigger for the scheduled sync (normally run by Celery beat)."""
    requested = await request_refreshes(db)
    collected = await collect_refreshed(db)
    return {"refresh_requested": requested, **collected}


@router.post("/admin/jobs/dispatch")
async def run_dispatch_now(admin: SuperAdmin, db: DB):
    return await dispatch_due(db)
