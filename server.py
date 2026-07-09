"""GAEB → Easyjob Import Tool — FastAPI + Jinja2 + HTMX.

Starten: uvicorn server:app --reload --port 8090
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from easyjob_api import EjLiveClient
from state import BASE_DIR, get_session
import db

from routes.auth import router as auth_router
from routes.matching import router as matching_router
from routes.d83 import router as d83_router
from routes.admin import router as admin_router


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

async def _nightly_sync() -> None:
    """Nächtliche Synchronisation: Artikel + Personal aus EJ SQL Server → DB."""
    import sync_odbc
    loop = asyncio.get_event_loop()
    try:
        stats = await loop.run_in_executor(None, sync_odbc.run_full_sync)
        db.log_sync(
            run_type="nightly",
            articles_new=stats.get("articles_new", 0),
            articles_updated=stats.get("articles_updated", 0),
            personal_new=stats.get("personal_new", 0),
            personal_updated=stats.get("personal_updated", 0),
            notes="; ".join(
                f"{k}: {v}" for k, v in stats.items() if "error" in k
            ) or "",
        )
        logging.info("nightly_sync abgeschlossen: %s", stats)
    except Exception as _e:
        logging.error("nightly_sync fehlgeschlagen: %s", _e)
        db.log_sync(run_type="nightly", notes=f"Fehler: {_e}")


@asynccontextmanager
async def lifespan(application: FastAPI):
    # ── Startup ──
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    loop = asyncio.get_event_loop()

    # DB initialisieren + JSON-Migration (falls Tabellen leer)
    await loop.run_in_executor(None, db.init_db)
    stats = await loop.run_in_executor(None, db.migrate_from_json)
    if stats:
        logging.info("DB-Migration: %s", stats)
    else:
        logging.info("DB bereits befüllt — keine Migration nötig")

    # APScheduler: nächtlicher Sync um 02:00
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        scheduler = AsyncIOScheduler()
        scheduler.add_job(_nightly_sync, "cron", hour=2, minute=0,
                          id="nightly_sync", replace_existing=True)
        scheduler.start()
        logging.info("APScheduler gestartet (nächtlicher Sync um 02:00)")
    except ImportError:
        scheduler = None
        logging.warning("apscheduler nicht installiert — kein nächtlicher Sync")

    yield

    # ── Shutdown ──
    if scheduler:
        scheduler.shutdown(wait=False)


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(title="GAEB → Easyjob", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ─── Auth-Middleware ─────────────────────────────────────────────────────────

@app.middleware("http")
async def _require_auth(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in ("/login", "/favicon.ico"):
        return await call_next(request)
    if not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    # UserSession nach Server-Neustart aus Cookie wiederherstellen
    ss = get_session(request.session)
    if ss.ej_client is None and request.session.get("ej_user"):
        ss.ej_url     = request.session.get("ej_url", "")
        ss.ej_user    = request.session.get("ej_user", "")
        ss.ej_pass    = request.session.get("ej_pass", "")
        ss.ej_db_conn = request.session.get("db_conn", "")
        ss.ej_user_id = int(request.session.get("ej_user_id", 0))
        if ss.ej_url and ss.ej_user and ss.ej_pass:
            try:
                loop = asyncio.get_event_loop()
                ss.ej_client = await loop.run_in_executor(
                    None, lambda: EjLiveClient(ss.ej_url, ss.ej_user, ss.ej_pass)
                )
                logging.info("EJ-Client aus Session wiederhergestellt (%s@%s)", ss.ej_user, ss.ej_url)
            except Exception as _e:
                logging.error("EJ-Client Wiederherstellung fehlgeschlagen: %s", _e)
    return await call_next(request)


# SessionMiddleware muss NACH @app.middleware registriert werden (LIFO-Stack).
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-secret-change-me"),
    session_cookie="gaeb_session",
    max_age=8 * 3600,
)

# ─── Admin-Endpunkte ──────────────────────────────────────────────────────────

from fastapi.responses import JSONResponse

@app.post("/api/admin/resync")
async def admin_resync(force: bool = False):
    """Manueller Sync-Trigger via ODBC. force=true löscht Artikel/Personal vorher."""
    import sync_odbc
    loop = asyncio.get_event_loop()
    if force:
        await loop.run_in_executor(None, lambda: db.migrate_from_json(force=True))
    stats = await loop.run_in_executor(None, sync_odbc.run_full_sync)
    db.log_sync(
        run_type="manual" + ("-force" if force else ""),
        articles_new=stats.get("articles_new", 0),
        articles_updated=stats.get("articles_updated", 0),
        personal_new=stats.get("personal_new", 0),
        personal_updated=stats.get("personal_updated", 0),
        notes="; ".join(f"{k}: {v}" for k, v in stats.items() if "error" in k) or "",
    )
    return JSONResponse({"ok": True, "stats": stats})


# ─── Router ───────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(matching_router)
app.include_router(d83_router)
app.include_router(admin_router)

# ─── Direkt starten ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8090, reload=True)
