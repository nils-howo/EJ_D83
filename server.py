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
from state import BASE_DIR, S
import db

from routes.auth import router as auth_router
from routes.matching import router as matching_router
from routes.d83 import router as d83_router


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

async def _nightly_sync() -> None:
    """Nächtliche Synchronisation: Artikel + Personal aus EJ-API → DB.
    Solange noch keine API-Anbindung: JSON → DB (idempotent, nur wenn DB leer).
    """
    loop = asyncio.get_event_loop()
    try:
        if S.ej_client:
            # TODO: EJ-API → DB (Artikel + Personal aus S.ej_client laden)
            logging.info("nightly_sync: EJ-Client vorhanden — API-Sync noch nicht implementiert")
        else:
            stats = await loop.run_in_executor(None, db.migrate_from_json)
            if stats:
                logging.info("nightly_sync: JSON-Migration: %s", stats)
    except Exception as _e:
        logging.error("nightly_sync fehlgeschlagen: %s", _e)


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
    # S nach Server-Neustart aus Session wiederherstellen
    if S.ej_client is None and request.session.get("ej_user"):
        S.ej_url     = request.session.get("ej_url", "")
        S.ej_user    = request.session.get("ej_user", "")
        S.ej_pass    = request.session.get("ej_pass", "")
        S.ej_db_conn = request.session.get("db_conn", "")
        S.ej_user_id = int(request.session.get("ej_user_id", 0))
        if S.ej_url and S.ej_user and S.ej_pass:
            try:
                loop = asyncio.get_event_loop()
                S.ej_client = await loop.run_in_executor(
                    None, lambda: EjLiveClient(S.ej_url, S.ej_user, S.ej_pass)
                )
                logging.info("EJ-Client aus Session wiederhergestellt (%s@%s)", S.ej_user, S.ej_url)
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

# ─── Router ───────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(matching_router)
app.include_router(d83_router)

# ─── Direkt starten ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8090, reload=True)
