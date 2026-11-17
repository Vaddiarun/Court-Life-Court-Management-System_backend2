@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "LOG=%~dp0setup_log.txt"
echo ==== Court Life setup %date% %time% ==== > "%LOG%"

echo [1/6] Preparing .env ...
if exist ".env" (
  echo .env already exists>>"%LOG%"
) else (
  copy /Y "env.generated" ".env" >nul
  echo ENV_CREATED>>"%LOG%"
)

echo [2/6] Opening project in Antigravity ...
where antigravity >nul 2>&1
if !errorlevel!==0 (
  start "" antigravity "%~dp0."
  echo ANTIGRAVITY_OPENED_CLI>>"%LOG%"
) else if exist "%LOCALAPPDATA%\Programs\Antigravity\Antigravity.exe" (
  start "" "%LOCALAPPDATA%\Programs\Antigravity\Antigravity.exe" "%~dp0."
  echo ANTIGRAVITY_OPENED_EXE>>"%LOG%"
) else (
  echo ANTIGRAVITY_NOT_FOUND>>"%LOG%"
)

echo [3/6] Checking tools ...
where python >>"%LOG%" 2>&1
python --version >>"%LOG%" 2>&1
where docker >nul 2>&1
if not !errorlevel!==0 (
  echo DOCKER_MISSING>>"%LOG%"
  echo.
  echo Docker Desktop is not installed. Install it from https://www.docker.com/products/docker-desktop/ and run this file again.
  goto end
)

docker info >nul 2>&1
if not !errorlevel!==0 (
  echo DOCKER_STARTING>>"%LOG%"
  echo Starting Docker Desktop, please wait ...
  if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
  set /a tries=0
  :waitdocker
  timeout /t 5 /nobreak >nul
  docker info >nul 2>&1
  if !errorlevel!==0 goto dockerready
  set /a tries+=1
  if !tries! lss 36 goto waitdocker
  echo DOCKER_NOT_RUNNING>>"%LOG%"
  echo Docker did not start. Open Docker Desktop manually, wait for "Engine running", then run this file again.
  goto end
)
:dockerready
echo DOCKER_OK>>"%LOG%"

echo [4/6] Building and starting containers (first time takes a few minutes) ...
docker compose up -d --build >>"%LOG%" 2>&1
if not !errorlevel!==0 (
  echo COMPOSE_FAILED>>"%LOG%"
  echo docker compose failed - see setup_log.txt
  goto end
)

echo [5/6] Waiting for the API to be healthy ...
set /a tries=0
:waitapi
timeout /t 3 /nobreak >nul
curl -s -f http://localhost:8000/health >>"%LOG%" 2>&1
if !errorlevel!==0 goto apiready
set /a tries+=1
if !tries! lss 60 goto waitapi
echo API_NOT_HEALTHY>>"%LOG%"
docker compose logs --tail 80 api >>"%LOG%" 2>&1
echo API did not start - see setup_log.txt
goto end
:apiready
echo.>>"%LOG%"
echo API_HEALTHY>>"%LOG%"

echo [6/6] Creating super admin (admin@courtlife.in / Admin@12345) ...
docker compose exec -T api python -m scripts.create_superadmin admin@courtlife.in "Platform Admin" "Admin@12345" >>"%LOG%" 2>&1
docker compose ps >>"%LOG%" 2>&1
start "" http://localhost:8000/docs
echo ALL_DONE>>"%LOG%"
echo.
echo Done! API docs: http://localhost:8000/docs
echo Login: admin@courtlife.in / Admin@12345

:end
echo ==== finished %time% ====>>"%LOG%"
echo.
pause
