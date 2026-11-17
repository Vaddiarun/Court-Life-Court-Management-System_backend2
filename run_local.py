"""Run Court Life locally on Windows/Mac/Linux without Docker.

Starts an embedded PostgreSQL (pgserver) in .pgdata, applies migrations,
creates the first super admin and launches the API on http://localhost:8000.
"""

import os
import pathlib
import subprocess
import sys
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent
LOG = ROOT / "local_run_log.txt"
os.chdir(ROOT)


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_env_value(key: str) -> str:
    """Read KEY=value from .env (the API itself loads .env via pydantic-settings)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return ""
    value = ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{key}="):  # the last line wins, same as the API
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
    return value


def warn_duplicate_env_keys() -> None:
    """A key set twice in .env silently uses the LAST line - flag it (values are never printed)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    seen: dict[str, list[int]] = {}
    for n, line in enumerate(env_file.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            seen.setdefault(stripped.split("=", 1)[0].strip(), []).append(n)
    for key, lines in seen.items():
        if len(lines) > 1:
            log(f"WARNING: .env sets {key} on lines {', '.join(map(str, lines))} - only line {lines[-1]} is used. Delete the others.")


def run(args: list[str], env: dict) -> int:
    proc = subprocess.run(args, env=env, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(out)
    print(out[-2000:], flush=True)
    return proc.returncode


def main() -> None:
    LOG.write_text("", encoding="utf-8")
    log(f"Python {sys.version.split()[0]} at {sys.executable}")
    warn_duplicate_env_keys()
    import pgserver

    log("Starting embedded PostgreSQL (first time takes ~30s) ...")
    srv = start_postgres(pgserver)
    if srv is None:
        return
    try:
        srv.psql("CREATE DATABASE cms_dev;")
        log("Created database cms_dev")
    except Exception as e:  # already exists
        log(f"Database check: {str(e).strip()[:120]}")
    uri = srv.get_uri("cms_dev")
    log(f"PostgreSQL URI: {uri}")

    env = os.environ.copy()
    env["DATABASE_URL"] = uri.replace("postgresql://", "postgresql+asyncpg://", 1)
    env.setdefault("LOCAL_STORAGE_DIR", str(ROOT / "storage"))
    env["PYTHONUTF8"] = "1"

    log("Applying database migrations ...")
    if run([sys.executable, "-m", "alembic", "upgrade", "head"], env) != 0:
        log("MIGRATION_FAILED")
        return
    log("MIGRATIONS_OK")

    log("Creating super admin admin@courtlife.in ...")
    run([sys.executable, "-m", "scripts.create_superadmin", "admin@courtlife.in", "Platform Admin", "Admin@12345"], env)
    sa_phone = read_env_value("SUPERADMIN_PHONE")
    if sa_phone:
        run([sys.executable, "-m", "scripts.set_phone", "admin@courtlife.in", sa_phone], env)
    else:
        log("TIP: add SUPERADMIN_PHONE=<your mobile> to .env so the super admin can sign in with OTP")

    api = start_api(env)
    if api is None:
        return
    log("RUNNING - keep this window open. Sign in at http://localhost:3000 with your mobile number + OTP")
    # Watchdog: if the embedded Postgres stops (it can, e.g. after Windows sleep), restart it and the API.
    db_down = 0
    code_stamp = code_mtime()
    try:
        while True:
            time.sleep(3)
            stamp = code_mtime()
            if stamp != code_stamp:
                code_stamp = stamp
                log("Code changed - restarting the API ...")
                stop_api(api)
                api = start_api(env)
                if api is None:
                    return
                continue
            if api.poll() is not None:
                log("API stopped - restarting it ...")
                api = start_api(env)
                if api is None:
                    return
                continue
            if port_open(uri):
                db_down = 0
                continue
            db_down += 1
            if db_down < 2:
                continue
            log("PostgreSQL stopped unexpectedly - restarting database and API ...")
            srv = start_postgres(pgserver)
            if srv is None:
                return
            uri = srv.get_uri("cms_dev")
            env["DATABASE_URL"] = uri.replace("postgresql://", "postgresql+asyncpg://", 1)
            stop_api(api)
            api = start_api(env)
            if api is None:
                return
            db_down = 0
            log("RECOVERED - database and API are running again")
    except KeyboardInterrupt:
        api.terminate()


def code_mtime() -> float:
    """Latest modification time of the backend code (app/**/*.py, locale json)."""
    newest = 0.0
    for path in (ROOT / "app").rglob("*"):
        if path.suffix in (".py", ".json"):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                pass
    return newest


def stop_api(api) -> None:
    """TerminateProcess on Windows - unlike Ctrl+C it does not reach Postgres in the same console."""
    if api.poll() is None:
        api.terminate()
        try:
            api.wait(timeout=15)
        except subprocess.TimeoutExpired:
            api.kill()


def port_open(uri: str) -> bool:
    import socket
    from urllib.parse import urlparse

    u = urlparse(uri)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 5432), timeout=2):
            return True
    except OSError:
        return False


def start_postgres(pgserver):
    for attempt in range(1, 7):
        try:
            return pgserver.get_server(ROOT / ".pgdata", cleanup_mode=None)
        except Exception as e:  # noqa: BLE001 - crash recovery after an unclean stop can take a minute
            log(f"PostgreSQL not ready yet (attempt {attempt}/6: {type(e).__name__}) - waiting 15s for recovery ...")
            time.sleep(15)
    log("POSTGRES_FAILED - see .pgdata\\log. Close other Court Life windows and run this again.")
    return None


def start_api(env: dict):
    import urllib.request

    for _ in range(10):  # let a just-stopped API release port 8000
        if not port_open("postgresql://x@127.0.0.1:8000/x"):
            break
        time.sleep(1)
    log("Starting API on http://localhost:8000  (press Ctrl+C to stop)")
    # No uvicorn --reload: on Windows its reloader stops the old worker with a console-wide Ctrl+C,
    # which also shuts down the embedded Postgres. The watchdog below restarts the API on code changes.
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        env=env,
    )
    for _ in range(40):
        time.sleep(1)
        try:
            body = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2).read().decode()
            log(f"API_HEALTHY {body}")
            return api
        except Exception:  # noqa: BLE001
            if api.poll() is not None:
                log("API_EXITED - see the error above")
                return None
    log("API_NOT_HEALTHY")
    return api

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERROR\n" + traceback.format_exc())
