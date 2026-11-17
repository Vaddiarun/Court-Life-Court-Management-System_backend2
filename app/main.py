import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1 import admin, auth, cases, documents, firm, hearings, insights, reminders, system, work
from app.core.config import settings
from app.core.db import engine

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop, task = asyncio.Event(), None
    if settings.RUN_SCHEDULER_IN_PROCESS:
        from app.services.scheduler import scheduler_loop

        task = asyncio.create_task(scheduler_loop(stop))
    yield
    if task:
        stop.set()
        await task


app = FastAPI(
    lifespan=lifespan,
    title=settings.APP_NAME,
    version="1.0.0",
    description=(
        "Multi-tenant Court Management System API: CNR case retrieval, automatic/manual sync, "
        "hearing calendar, reminders with in-app/email/SMS/voice delivery, documents, clients, "
        "dashboards, usage metering and audit history. Official court records remain authoritative; "
        "every synced case exposes its source and last-sync time."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth, admin, firm, cases, hearings, reminders, documents, work, insights, system):
    app.include_router(r.router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["system"])
async def health():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "court_provider": settings.COURT_PROVIDER, "voice_provider": settings.VOICE_PROVIDER}
