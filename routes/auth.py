"""Auth-Routen: /login, /logout."""
import asyncio
import logging
import os
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from easyjob_api import EjLiveClient
from state import get_session, drop_session, _build_db_conn, is_admin_login, templates

router = APIRouter()

# ── Brute-Force-Bremse (in-memory, pro IP) ──────────────────────────────────
_login_fails: dict[str, list[float]] = {}
_FAIL_WINDOW = 300.0   # 5 Minuten Beobachtungsfenster
_FAIL_MAX    = 8       # danach kurz gesperrt


def _recent_fails(ip: str) -> list[float]:
    now   = time.monotonic()
    fails = [t for t in _login_fails.get(ip, []) if now - t < _FAIL_WINDOW]
    _login_fails[ip] = fails
    return fails


def _too_many_attempts(ip: str) -> bool:
    return len(_recent_fails(ip)) >= _FAIL_MAX


def _record_fail(ip: str) -> None:
    _recent_fails(ip).append(time.monotonic())


def _clear_fails(ip: str) -> None:
    _login_fails.pop(ip, None)


def _check_db(db_conn: str) -> None:
    """Blockierender DB-Verbindungstest (läuft im Threadpool, nicht im Event-Loop)."""
    import pyodbc as _pyodbc
    cn = _pyodbc.connect(db_conn, timeout=6)
    cn.close()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request:  Request,
    ej_user:  str = Form(""),
    ej_pass:  str = Form(""),
):
    def _fail(msg: str, status: int = 422):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": msg}, status_code=status)

    ip = request.client.host if request.client else "?"
    if _too_many_attempts(ip):
        return _fail("Zu viele Fehlversuche — bitte einige Minuten warten.", status=429)

    ej_url  = os.environ.get("EJ_BASE_URL", "http://EASYJOB-TEST:8008").rstrip("/")
    db_conn = _build_db_conn(
        os.environ.get("EJ_DB_SERVER", r"EASYJOB-TEST\SQLEXPRESS"),
        os.environ.get("EJ_DB_NAME",   "easyjob"),
        os.environ.get("EJ_DB_UID",    "sa"),
        os.environ.get("EJ_DB_PWD",    ""),
    )

    loop = asyncio.get_event_loop()

    # Blockierende I/O (pyodbc / HTTP) im Threadpool — sonst friert der Event-Loop
    # bei einem hängenden DB-/EJ-Server für alle Nutzer ein.
    try:
        await loop.run_in_executor(None, _check_db, db_conn)
    except Exception as exc:
        logging.warning("Login: DB-Verbindung fehlgeschlagen (%s): %s", ip, exc)
        _record_fail(ip)
        return _fail("Datenbankverbindung fehlgeschlagen.")

    try:
        client  = await loop.run_in_executor(None, lambda: EjLiveClient(ej_url, ej_user, ej_pass))
        user_id = await loop.run_in_executor(None, client.get_current_user_id)
    except Exception as exc:
        logging.warning("Login: EasyJob-Verbindung fehlgeschlagen (%s): %s", ip, exc)
        _record_fail(ip)
        return _fail("EasyJob-Verbindung fehlgeschlagen.")

    if not user_id:
        _record_fail(ip)
        return _fail("Benutzername oder Passwort falsch.")

    _clear_fails(ip)
    is_admin = is_admin_login(ej_user)

    ss = get_session(request.session)
    ss.ej_url     = ej_url
    ss.ej_user    = ej_user
    ss.ej_pass    = ej_pass
    ss.ej_db_conn = db_conn
    ss.ej_user_id = user_id
    ss.ej_client  = client
    ss.is_admin   = is_admin

    # Nur Identität ins Cookie — KEINE Passwörter / Connection-Strings.
    # (Starlette signiert das Cookie, verschlüsselt es aber nicht.)
    request.session["authenticated"] = True
    request.session["ej_user"]       = ej_user
    request.session["is_admin"]      = is_admin
    return RedirectResponse("/import", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    drop_session(request.session)   # Credentials/State serverseitig aus dem RAM löschen
    request.session.clear()         # Cookie leeren
    return RedirectResponse("/login", status_code=303)
