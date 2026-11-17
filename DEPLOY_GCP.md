# Deploying Court Life on Google Cloud (Mumbai)

| Part | Service | Notes |
|---|---|---|
| API | Cloud Run `courtlife-api` (asia-south1) | scales to zero, billed per request |
| Reminders + court sync | Cloud Scheduler -> `POST /api/v1/cron/tick` | every minute / every 15 min |
| Database | Cloud SQL Postgres 16, `db-f1-micro` | daily backup 02:00 IST |
| Documents | Cloud Storage bucket `<project>-courtlife-docs` | private |
| Frontend | Firebase Hosting `https://<project>.web.app` | free tier |

## Part 1 - one-time setup (you)
1. https://console.cloud.google.com -> sign in with the client account -> activate the free trial (card needed).
2. Create project `courtlife-prod`; note the **Project ID**.
3. Install the Google Cloud CLI: https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe
4. New PowerShell window: `gcloud auth login` then `gcloud config set project <PROJECT_ID>`.

## Part 2 - backend
```powershell
cd I:\court-life-backend
copy gcp.env.example.yaml gcp.env.yaml      # then open gcp.env.yaml and fill in the keys
.\deploy_gcp.ps1 -ProjectId <PROJECT_ID>
```
First run ~20 minutes (the database takes ~10). It prints the API URL at the end, e.g.
`https://courtlife-api-abc123-el.a.run.app`. Re-run the same command to ship a new version.

Files the script creates on your laptop - keep them private, never commit or share:
- `gcp.env.yaml` (your keys)  - `.gcp-secrets.json` (database password, JWT and webhook secrets)

## Part 3 - frontend
```powershell
npm install -g firebase-tools
firebase login
cd I:\cort_ui_frontend
.\deploy_firebase.ps1 -ProjectId <PROJECT_ID> -ApiUrl <API URL from part 2>
```
Open `https://<PROJECT_ID>.web.app` and sign in with the super admin phone + OTP.
(The backend already allows requests from `https://<PROJECT_ID>.web.app`.)

## Updating keys later
Edit `gcp.env.yaml` and re-run `.\deploy_gcp.ps1 -ProjectId <PROJECT_ID>`.
- SMS after DLT: `SMS_PROVIDER: "fast2sms"`, `SMS_SENDER_ID`, `FAST2SMS_API_KEY`, and
  `FAST2SMS_TEMPLATES: '{"otp":"...","hearing":"...","reminder":"..."}'`
- Calls: `VOICE_PROVIDER: "twilio"` + `TWILIO_*` (status callbacks use the Cloud Run URL automatically)

## Logs and troubleshooting
- Logs: Console -> Cloud Run -> courtlife-api -> Logs, or `gcloud run services logs read courtlife-api --region asia-south1`
- Scheduler runs: Console -> Cloud Scheduler (each job shows last run status)
- Database: Console -> SQL -> courtlife-db (backups tab)

## Cost (approx.)
Cloud SQL db-f1-micro ~$9-11/month; Cloud Run, Scheduler, Storage and Firebase mostly inside free tiers
for a single firm. New accounts get a $300 / 90-day credit. Set a budget alert:
Console -> Billing -> Budgets & alerts -> e.g. Rs 2,000/month.
