# Court Life — Court Management System Backend

FastAPI + PostgreSQL + Celery backend for the multi-tenant Court Management System described in the
*Client PRD & Cost Estimate v1.1*. It covers the full **initial release scope** (PRD §10).

- **Stack:** Python 3.12+, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2, Celery + Redis, JWT, bcrypt
- **API:** 79 operations under `/api/v1`, interactive docs at `/docs`
- **Tests:** 90 tests on a real PostgreSQL database, 95% line coverage

## Quick start

```bash
cp .env.example .env              # set JWT_SECRET / WEBHOOK_SECRET
docker compose up --build         # api :8000, worker, beat, postgres, redis
docker compose exec api python -m scripts.create_superadmin admin@you.in "Platform Admin" 'StrongPassw0rd!'
open http://localhost:8000/docs
```

Without Docker:

```bash
pip install -r requirements-dev.txt
alembic upgrade head
python -m scripts.create_superadmin admin@you.in "Platform Admin" 'StrongPassw0rd!'
uvicorn app.main:app --reload
celery -A app.workers.celery_app worker -l info     # separate terminal
celery -A app.workers.celery_app beat -l info       # separate terminal
```

## Running the tests

```bash
createdb cms_test                       # tests use postgresql://postgres@localhost:5432/cms_test
pytest --cov                            # override with DATABASE_URL=... if needed
```

All third-party services (court API, Exotel, SMTP) are replaced by mocks in tests, so no API credits are spent and no calls are placed.

## Project layout

```
app/
  core/          config, async DB session, JWT/bcrypt security, IST date helpers
  models/        20 tables — every tenant-owned row carries tenant_id
  schemas/       Pydantic request/response models
  api/deps.py    auth, role guards, row-level visibility (firm / assigned / granted)
  api/v1/        auth, admin, firm, cases, hearings, reminders, documents, work (tasks+notes), insights, system
  services/      sync (snapshot+diff engine), reminders (scheduling+dispatch), notify, usage metering
  providers/     court (base, mock, eCourtsIndia), messaging (Exotel voice/SMS, SMTP), storage (local/S3)
  i18n/          23 languages (English + 22 Eighth Schedule) and notification templates
  workers/       Celery app + beat schedule
alembic/         migrations
tests/           90 API + service tests
```

## Roles and access

| Role | Can |
|---|---|
| `super_admin` | Create/suspend firms, plans and allowances, platform reports, usage, audit logs, run jobs |
| `firm_admin` | Everything in their firm: users, roles, cases, settings, firm reports, usage, audit log |
| `lawyer` | Look up and add cases, manage clients, sync, calendar, reminders, documents, assign juniors |
| `junior_advocate` | Only **assigned** cases; team/client notes, tasks assigned to them, own reminders |
| `client` | Only **granted** cases (restricted fields), shared documents, client-visible notes, own notifications |

Cross-tenant access always returns `404`, so it never reveals that a resource exists.

## How the important parts work

**CNR lookup → add (PRD 3.1).** `POST /cases/lookup` validates and normalises the CNR (`DLHC01-000123-2024` → `DLHC010001232024`),
fetches it from the provider and caches it for 30 min. `POST /cases/from-cnr` reuses the cached record, so adding a case you just
looked up doesn't pay for a second detail call. Manual entry (`POST /cases`) has duplicate detection on normalised case number + court
(override with `force=true`).

**Sync (PRD 3.3, 5.1).** The cost-aware design keeps the bill near the PRD's ₹300/month for 1,000 cases rather than ₹16,500:
- *Priority schedule:* hearing today or past → every 12 h; within 3 days → daily; within 30 days → every 3 days; later → weekly; disposed → monthly.
- *Two-phase scheduled sync*, because eCourtsIndia's refresh is asynchronous: beat calls `request_refreshes` (₹0.05) every 30 min,
  then `collect_refreshed` reads the detail once the refresh has settled.
- *Hash-based diff:* a snapshot, change events and alerts are written only when the normalised record changes. Tracked fields are status, stage,
  next hearing date, judge, court, case number, disposal and new orders.
- *Sync Now* reads detail immediately and queues a refresh, with a per-case cooldown (`429` + `Retry-After`).
- New official hearing dates are added to the calendar, and an **auto hearing reminder** (default 3/1/0 days, firm-configurable) replaces the old one.
- `POST /webhooks/court-provider` accepts portfolio-monitoring callbacks.

**Reminders and calls (PRD 3.5, 3.6).** Each reminder expands into jobs, one per recipient × channel × time ("N days before" is sent at 08:00 IST).
Beat dispatches due jobs every minute:
- *Preferences and consent:* each channel's user preferences and recorded consent are checked. SMS and calls need explicit consent with a timestamp.
- *Localisation:* the message is rendered in the recipient's language.
- *Call window:* calls go out only between 09:00 and 21:00 IST, and are deferred otherwise.
- *Exotel status webhook:* calls that end busy or no-answer are retried up to 3 times. Billable minutes are metered.

**Usage and allowances (PRD 8).** Every provider request is metered with the PRD rate card. `GET /usage` and `GET /reports/firm` show the
estimated ₹ amount, the allowance and the overage.

**Audit.** Logins (including failed ones), user and role changes, case, document, note and reminder changes, syncs and downloads are recorded.

## Configuration to finish before production

1. **eCourtsIndia:** set `COURT_PROVIDER=ecourtsindia` and `ECOURTSINDIA_API_KEY`. The adapter uses the documented endpoints
   (`GET /api/partner/case/{cnr}`, `POST …/refresh`, `GET /api/partner/search`). The public docs don't publish the response
   schema, so `_parse_case` reads fields defensively. **Check it against real responses during the PRD proof of concept**, then tighten it
   (tests are in `tests/test_dashboard_i18n_adapter.py`).
2. **Exotel:** create a flow that plays the reminder (dynamic TTS or prerecorded audio per language), set the `EXOTEL_*` vars, and point the
   status callback at `/api/v1/webhooks/exotel/call-status?token=WEBHOOK_SECRET`.
3. **SMS:** complete TRAI DLT registration (entity + templates) before turning on `SMS_PROVIDER=exotel`.
4. **Translations:** templates exist for en, hi, te, ta and ur, and other languages fall back to English. Have native speakers review them (PRD 11).
5. **Security hardening:** strong secrets, HTTPS, S3 with SSE (already set), Postgres row-level security as a second layer, rate limiting at the gateway, and DPDP Act
   consent and retention policies.
