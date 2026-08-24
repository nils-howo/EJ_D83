"""GAEB → Easyjob Import Tool — FastAPI + Jinja2 + HTMX.

Starten: uvicorn server:app --reload --port 8090
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from easyjob_api import EjLiveClient
from state import BASE_DIR, get_session
import db

from routes.auth import router as auth_router
from routes.matching import router as matching_router
from routes.d83 import router as d83_router
from routes.admin import router as admin_router
from routes.import_ import router as import_router
from routes.projects import router as projects_router


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

def _to_login(request: Request):
    """Zur Anmeldung schicken — je nach Art der Anfrage unterschiedlich.

    Bei htmx- oder fetch-Aufrufen darf es KEINE 303 auf /login sein: Browser folgen
    ihr transparent, und die Login-Seite würde als HTML in das Ziel-Element gerendert
    (nach einem Server-Neustart landete das Anmeldeformular so mitten in der
    Gruppenansicht). Stattdessen 401 + HX-Redirect, das löst einen echten
    Seitenwechsel aus.
    """
    hx   = request.headers.get("hx-request")
    mode = request.headers.get("sec-fetch-mode", "")
    xhr  = request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
    if hx or xhr or (mode and mode != "navigate"):
        resp = Response(status_code=401)
        resp.headers["HX-Redirect"] = "/login"
        return resp
    return RedirectResponse("/login", status_code=303)


@app.middleware("http")
async def _require_auth(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in ("/login", "/favicon.ico"):
        return await call_next(request)
    if not request.session.get("authenticated"):
        return _to_login(request)
    # Serverseitige Session (Matcher/Projekt/EJ-Client) lebt im RAM (TTL 15 h) und
    # trägt am Folgetag weiter, solange der Server läuft. Ist sie weg (Neustart /
    # TTL abgelaufen), lässt sich der EJ-Client mangels Passwort NICHT rekonstruieren
    # (Passwörter liegen bewusst NICHT im Cookie) und der Heavy-State ist ohnehin
    # verloren → sauber neu anmelden statt mit halbem Zustand weiterzulaufen.
    ss = get_session(request.session)
    if ss.ej_client is None:
        request.session.clear()
        return _to_login(request)
    return await call_next(request)


# SessionMiddleware muss NACH @app.middleware registriert werden (LIFO-Stack).
_SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if len(_SESSION_SECRET) < 16 or _SESSION_SECRET == "dev-secret-change-me":
    raise RuntimeError(
        "SESSION_SECRET fehlt oder ist zu kurz/unsicher. Bitte in der .env eine lange "
        "Zufallszeichenkette setzen (erzeugen mit: "
        "python -c \"import secrets; print(secrets.token_hex(32))\")."
    )
# Hinter TLS-Proxy → Secure-Cookie. Für lokalen HTTP-Test: COOKIE_SECURE=false.
_cookie_secure = os.environ.get("COOKIE_SECURE", "true").strip().lower() != "false"
# Antworten komprimieren. Der Mapping-Dialog ist bei 13 Blättern über 700 KB HTML,
# davon über die Hälfte Einrückung — gzip drückt das auf ~43 KB. Bewusst hier statt
# über Jinjas trim_blocks/lstrip_blocks: die würden die Whitespace-Semantik ALLER
# Templates ändern und können Textknoten zusammenziehen, die durch einen Umbruch
# getrennt waren. add_middleware fügt außen an, gzip liegt also über allem.
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    session_cookie="gaeb_session",
    max_age=15 * 3600,          # 15 h — Weiterarbeiten am Folgetag
    https_only=_cookie_secure,
    same_site="lax",
)

# ─── Admin-Endpunkte ──────────────────────────────────────────────────────────

from fastapi import Request
from fastapi.responses import JSONResponse

@app.post("/api/admin/resync")
async def admin_resync(request: Request, force: bool = False):
    """Manueller Sync-Trigger via ODBC. force=true löscht Artikel/Personal vorher.
    Nur für Admins — der Endpoint ist destruktiv (force re-migriert die lokale DB)."""
    if not get_session(request.session).is_admin:
        return JSONResponse({"ok": False, "error": "Nur für Admins"}, status_code=403)
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
app.include_router(import_router)
app.include_router(projects_router)

# ─── Direkt starten ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8090, reload=True)
