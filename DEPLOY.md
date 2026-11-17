# Deploying Court Life

The backend (this folder) and the frontend (`cort_ui_frontend`) deploy separately.

| Part | Host | Cost (approx.) |
|---|---|---|
| Backend API + reminders scheduler | Render web service (Docker), Singapore | Starter plan, ~$7/month |
| Database | Render Postgres 16 | Basic, ~$6/month |
| Uploaded documents | Render disk, 5 GB | ~$1/month |
| Frontend | Vercel | Free (Hobby) |

Why paid plans: the free Render web service sleeps after 15 minutes idle, which stops reminder calls/SMS/emails, and free Render Postgres is deleted after 30 days.

## 1. Put both folders on GitHub
Create two private repos (for example `courtlife-backend` and `courtlife-frontend`) and push each folder.
`.gitignore` already keeps `.env`, `.venv`, `.pgdata` and `node_modules` out.

## 2. Backend on Render
1. render.com → sign up with the client email → **New → Blueprint** → pick `courtlife-backend`.
   Render reads `render.yaml` and creates the API, the database and the disk.
2. Fill in the values it asks for (anything marked `sync: false`). Use `env.production.example.txt` as the checklist:
   - `CORS_ORIGINS` – the Vercel URL from step 3 (you can update it after step 3)
   - `PUBLIC_BASE_URL` – the Render URL of the API, e.g. `https://courtlife-api.onrender.com`
   - `SUPERADMIN_EMAIL`, `SUPERADMIN_PHONE`
   - Message Central, eCourtsIndia, email keys
3. Deploy. On every start the container runs database migrations, makes sure the super admin exists, then starts the API.
4. Check `https://<api>.onrender.com/health` → `{"status":"ok", ...}`.

## 3. Frontend on Vercel
1. vercel.com → **Add New → Project** → import `courtlife-frontend`. Framework: **Vite** (auto-detected).
2. Environment variable: `VITE_API_URL = https://<api>.onrender.com`
3. Deploy. `vercel.json` already sends every route to `index.html` so page refreshes work.
4. Put the Vercel URL into the backend's `CORS_ORIGINS` on Render and redeploy the backend.

## 4. After it is live
- Sign in at the Vercel URL with the super admin phone + OTP.
- Profile → **Delivery channels** shows which channels are live.
- Twilio / Exotel: use `PUBLIC_BASE_URL` for call-status webhooks (no ngrok needed any more).
- Exotel flow greeting URL: `https://<api>.onrender.com/api/v1/webhooks/exotel/greeting?token=<WEBHOOK_SECRET>`

## Turning channels on later (Render → Environment, then redeploy)
| Channel | Variables |
|---|---|
| SMS after DLT | `SMS_PROVIDER=fast2sms`, `SMS_SENDER_ID`, `FAST2SMS_API_KEY`, `FAST2SMS_TEMPLATES` |
| Calls (Twilio) | `VOICE_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |
| Calls (Exotel) | `VOICE_PROVIDER=exotel`, `EXOTEL_*` |
| Email | `EMAIL_PROVIDER=smtp`, `SMTP_*`, `EMAIL_FROM` |

### DLT templates expected by `FAST2SMS_TEMPLATES`
Register these exact shapes on the DLT portal (each `{#var#}` holds at most 30 characters):
- `otp` (Transactional): `{#var#} is your OTP to login to Court Life. Valid for 10 minutes. Do not share it with anyone. - GSK Associates`
- `hearing` (Service Implicit): `Reminder: Your case {#var#} is listed on {#var#} at {#var#}. - GSK Associates`
- `reminder` (Service Implicit): `Reminder: {#var#} on {#var#} at {#var#}. - GSK Associates`

Then add each approved template in Fast2SMS → DLT Manager and copy its **Message ID** into
`FAST2SMS_TEMPLATES={"otp":"...","hearing":"...","reminder":"..."}`.
