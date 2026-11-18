"""Metadata, provider webhooks and operator job triggers."""

import hmac
import uuid

from fastapi import APIRouter, Form, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DB, SuperAdmin
from app.core.config import settings
from app.i18n import LANGUAGES, translations_available
from app.models import VoiceMessage
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
async def exotel_greeting(
    db: DB,
    token: str = Query(...),
    CustomField: str = Query(""),
    CallSid: str = Query(""),
):
    """Exotel flow 'Greeting' applet (dynamic text) fetches this URL; we return the text to
    speak. The call carries CustomField 'vm:<id>'; CallSid is the fallback lookup."""
    _check_token(token)
    text = ""
    row = None
    if CustomField.startswith("vm:"):
        try:
            row = await db.get(VoiceMessage, uuid.UUID(CustomField[3:].strip()))
        except ValueError:
            row = None
    if row is None and CallSid:
        row = (await db.execute(select(VoiceMessage).where(VoiceMessage.call_sid == CallSid))).scalars().first()
    if row is not None:
        text = row.text
    elif CustomField.count("|") >= 2:  # older calls: 'reference|language|message'
        text = CustomField.split("|", 2)[2]
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


@router.post("/cron/tick")
async def cron_tick(
    db: DB,
    jobs: str = Query("reminders,collect", description="comma list: reminders, collect, sync"),
    x_cron_token: str | None = Header(None),
):
    """Called by Cloud Scheduler (or any cron) when the API runs without the in-process
    scheduler, e.g. on Cloud Run with request-based billing. Protected by WEBHOOK_SECRET
    sent in the X-Cron-Token header.
      every minute:      jobs=reminders,collect
      every 15 minutes:  jobs=sync
    """
    _check_token(x_cron_token)
    wanted = {j.strip() for j in jobs.split(",") if j.strip()}
    unknown = wanted - {"reminders", "collect", "sync"}
    if unknown:
        raise HTTPException(422, f"Unknown job(s): {', '.join(sorted(unknown))}")
    out: dict = {}
    if "reminders" in wanted:
        out["reminders"] = await dispatch_due(db)
        await db.commit()
    if "sync" in wanted:
        out["refresh_requested"] = await request_refreshes(db)
        await db.commit()
    if "collect" in wanted:
        out["collected"] = await collect_refreshed(db)
        await db.commit()
    return out
