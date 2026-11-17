<#
  Court Life - deploy the backend to Google Cloud (Mumbai, asia-south1).

  Creates (only if missing) and then deploys:
    - Cloud SQL Postgres 16 (db-f1-micro)  + database + user
    - Cloud Storage bucket for case documents
    - Service account with the minimum roles
    - Artifact Registry repo, image built by Cloud Build (no Docker needed locally)
    - Cloud Run service "courtlife-api" (scales to zero, billed per request)
    - Cloud Scheduler jobs that drive reminders (every minute) and court sync (every 15 min)

  Usage (PowerShell, from I:\court-life-backend):
      .\deploy_gcp.ps1 -ProjectId courtlife-prod
  Re-run the same command to deploy a new version; existing resources are reused.
  Needs: gcloud CLI logged in (gcloud auth login), and gcp.env.yaml filled in (copy gcp.env.example.yaml).
#>
param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [string]$Region = "asia-south1",
    [string]$EnvFile = "gcp.env.yaml"
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$Service   = "courtlife-api"
$SqlName   = "courtlife-db"
$DbName    = "courtlife"
$DbUser    = "courtlife"
$Repo      = "courtlife"
$SaName    = "courtlife-api"
$Bucket    = "$ProjectId-courtlife-docs"
$SecretsFile = ".gcp-secrets.json"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }

# Run gcloud; stop with a clear message if it fails
function G {
    & gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args -join ' ') failed (exit $LASTEXITCODE)" }
}
# Run gcloud quietly and return $true if it succeeded (used for 'does it exist?' checks)
function GExists {
    $ErrorActionPreference = "Continue"
    & gcloud @args *> $null
    return ($LASTEXITCODE -eq 0)
}
function NewSecret([int]$bytes = 32) {
    $b = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    return ([Convert]::ToBase64String($b) -replace '[+/=]', '')
}

# ---------------------------------------------------------------- checks
Step "Checking prerequisites"
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud is not installed. Install the Google Cloud CLI first (see DEPLOY_GCP.md)."
}
if (-not (Test-Path $EnvFile)) {
    throw "$EnvFile not found. Copy gcp.env.example.yaml to $EnvFile and fill in your keys."
}
G config set project $ProjectId --quiet | Out-Null
G config set run/region $Region --quiet | Out-Null
$ProjectNumber = (& gcloud projects describe $ProjectId --format="value(projectNumber)").Trim()
if (-not $ProjectNumber) { throw "Project $ProjectId not found or no access. Check the Project ID." }
Ok "Project $ProjectId ($ProjectNumber), region $Region"

# Secrets generated once and reused on every deploy (kept only on this laptop, never committed)
if (Test-Path $SecretsFile) {
    $secrets = Get-Content $SecretsFile -Raw | ConvertFrom-Json
} else {
    $secrets = [pscustomobject]@{ db_password = (NewSecret 24); jwt_secret = (NewSecret 48); webhook_secret = (NewSecret 32) }
    $secrets | ConvertTo-Json | Set-Content $SecretsFile -Encoding UTF8
    Ok "Generated database password and app secrets in $SecretsFile (keep this file safe, do not share)"
}

# ---------------------------------------------------------------- APIs
Step "Enabling Google Cloud APIs (first time takes 1-2 minutes)"
G services enable run.googleapis.com sqladmin.googleapis.com cloudbuild.googleapis.com `
    artifactregistry.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com iam.googleapis.com
Ok "APIs enabled"

# ---------------------------------------------------------------- database
Step "Cloud SQL Postgres ($SqlName)"
if (GExists sql instances describe $SqlName) {
    Ok "Instance exists"
} else {
    Write-Host "    Creating instance - this takes about 10 minutes, please wait..."
    G sql instances create $SqlName --database-version=POSTGRES_16 --edition=enterprise --tier=db-f1-micro `
        --region=$Region --storage-type=SSD --storage-size=10 --storage-auto-increase `
        --backup-start-time=20:30 --maintenance-window-day=SUN --maintenance-window-hour=21
    Ok "Instance created (daily backup at 02:00 IST)"
}
if (-not (GExists sql databases describe $DbName --instance=$SqlName)) {
    G sql databases create $DbName --instance=$SqlName | Out-Null
    Ok "Database $DbName created"
}
$users = & gcloud sql users list --instance=$SqlName --format="value(name)"
if ($users -contains $DbUser) {
    G sql users set-password $DbUser --instance=$SqlName --password=$($secrets.db_password) | Out-Null
} else {
    G sql users create $DbUser --instance=$SqlName --password=$($secrets.db_password) | Out-Null
}
Ok "Database user ready"
$SqlConn = (& gcloud sql instances describe $SqlName --format="value(connectionName)").Trim()

# ---------------------------------------------------------------- storage
Step "Cloud Storage bucket for documents (gs://$Bucket)"
if (GExists storage buckets describe "gs://$Bucket") {
    Ok "Bucket exists"
} else {
    G storage buckets create "gs://$Bucket" --location=$Region --uniform-bucket-level-access --public-access-prevention | Out-Null
    Ok "Bucket created (private)"
}

# ---------------------------------------------------------------- identity
Step "Service account and permissions"
$Sa = "$SaName@$ProjectId.iam.gserviceaccount.com"
if (-not (GExists iam service-accounts describe $Sa)) {
    G iam service-accounts create $SaName --display-name="Court Life API" | Out-Null
    Start-Sleep -Seconds 10   # new accounts take a moment to become usable in policies
}
G projects add-iam-policy-binding $ProjectId --member="serviceAccount:$Sa" --role="roles/cloudsql.client" --condition=None --quiet | Out-Null
G storage buckets add-iam-policy-binding "gs://$Bucket" --member="serviceAccount:$Sa" --role="roles/storage.objectAdmin" | Out-Null
# Cloud Build runs as the default compute account on new projects; let it build and push images
$BuildSa = "$ProjectNumber-compute@developer.gserviceaccount.com"
foreach ($role in "roles/artifactregistry.writer", "roles/storage.objectViewer", "roles/logging.logWriter") {
    G projects add-iam-policy-binding $ProjectId --member="serviceAccount:$BuildSa" --role=$role --condition=None --quiet | Out-Null
}
Ok "Permissions set"

# ---------------------------------------------------------------- image
Step "Building the backend image with Cloud Build (3-5 minutes)"
if (-not (GExists artifacts repositories describe $Repo --location=$Region)) {
    G artifacts repositories create $Repo --repository-format=docker --location=$Region --description="Court Life images" | Out-Null
}
$Tag = Get-Date -Format "yyyyMMdd-HHmmss"
$Image = "$Region-docker.pkg.dev/$ProjectId/$Repo/api:$Tag"
G builds submit --tag $Image .
Ok "Image $Image"

# ---------------------------------------------------------------- settings
Step "Preparing settings"
$envLines = Get-Content $EnvFile | Where-Object { $_ -match '^\s*[A-Z0-9_]+\s*:' }
$userKeys = $envLines | ForEach-Object { ($_ -split ':', 2)[0].Trim() }
$managed = [ordered]@{
    "ENVIRONMENT"              = "production"
    "DATABASE_URL"             = "postgresql+asyncpg://${DbUser}:$($secrets.db_password)@/${DbName}?host=/cloudsql/$SqlConn"
    "JWT_SECRET"               = $secrets.jwt_secret
    "WEBHOOK_SECRET"           = $secrets.webhook_secret
    "STORAGE_BACKEND"          = "gcs"
    "GCS_BUCKET"               = $Bucket
    "RUN_SCHEDULER_IN_PROCESS" = "false"   # Cloud Scheduler calls /cron/tick instead
    "WEB_CONCURRENCY"          = "1"
    "PASSWORD_LOGIN_ENABLED"   = "false"
}
if ($userKeys -notcontains "CORS_ORIGINS") { $managed["CORS_ORIGINS"] = "https://$ProjectId.web.app,https://$ProjectId.firebaseapp.com" }
$existingUrl = ""
if (GExists run services describe $Service --region=$Region) {
    $existingUrl = (& gcloud run services describe $Service --region=$Region --format="value(status.url)").Trim()
    if ($existingUrl -and ($userKeys -notcontains "PUBLIC_BASE_URL")) { $managed["PUBLIC_BASE_URL"] = $existingUrl }
}
$tmp = Join-Path $env:TEMP "courtlife-env-$Tag.yaml"
$out = New-Object System.Collections.Generic.List[string]
foreach ($k in $managed.Keys) {
    if ($userKeys -notcontains $k) { $out.Add("${k}: `"$($managed[$k])`"") }
}
foreach ($l in $envLines) { $out.Add($l) }
[System.IO.File]::WriteAllLines($tmp, $out)

# ---------------------------------------------------------------- deploy
Step "Deploying Cloud Run service $Service"
try {
    G run deploy $Service --image=$Image --region=$Region --service-account=$Sa `
        --add-cloudsql-instances=$SqlConn --env-vars-file=$tmp `
        --cpu=1 --memory=1Gi --min-instances=0 --max-instances=3 --concurrency=40 --timeout=300 `
        --allow-unauthenticated --quiet
} finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue   # contains secrets
}
$Url = (& gcloud run services describe $Service --region=$Region --format="value(status.url)").Trim()
if (-not $existingUrl -and ($userKeys -notcontains "PUBLIC_BASE_URL")) {
    G run services update $Service --region=$Region --update-env-vars="PUBLIC_BASE_URL=$Url" --quiet | Out-Null
}
Ok "API live at $Url"

# ---------------------------------------------------------------- scheduler
Step "Cloud Scheduler jobs (reminders every minute, court sync every 15 minutes)"
$jobs = @(
    @{ name = "courtlife-reminders"; schedule = "* * * * *";    query = "reminders,collect" },
    @{ name = "courtlife-court-sync"; schedule = "*/15 * * * *"; query = "sync" }
)
foreach ($j in $jobs) {
    $target = "$Url/api/v1/cron/tick?jobs=$($j.query)"
    $common = @("--location=$Region", "--schedule=$($j.schedule)", "--time-zone=Asia/Kolkata", "--uri=$target",
                "--http-method=POST", "--attempt-deadline=120s")
    if (GExists scheduler jobs describe $j.name --location=$Region) {
        G scheduler jobs update http $j.name @common "--update-headers=X-Cron-Token=$($secrets.webhook_secret)" --quiet | Out-Null
    } else {
        G scheduler jobs create http $j.name @common "--headers=X-Cron-Token=$($secrets.webhook_secret)" --quiet | Out-Null
    }
    Ok "$($j.name): $($j.schedule)"
}

# ---------------------------------------------------------------- check
Step "Health check"
try {
    $health = Invoke-RestMethod "$Url/health" -TimeoutSec 60
    Ok ("Health: " + ($health | ConvertTo-Json -Compress))
} catch {
    Write-Host "    Health check did not answer yet - open $Url/health in a minute." -ForegroundColor Yellow
}
Write-Host "`nDONE. Backend URL: $Url" -ForegroundColor Green
Write-Host "Next: deploy the frontend with VITE_API_URL=$Url (see DEPLOY_GCP.md, part 3)."
