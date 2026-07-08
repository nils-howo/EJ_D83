"""Auth-Routen: /login, /logout."""
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from easyjob_api import EjLiveClient
from state import S, _build_db_conn, templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request:  Request,
    ej_user:  str = Form(""),
    ej_pass:  str = Form(""),
):
    def _fail(msg: str):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": msg}, status_code=422)

    ej_url  = os.environ.get("EJ_BASE_URL", "http://EASYJOB-TEST:8008").rstrip("/")
    db_conn = _build_db_conn(
        os.environ.get("EJ_DB_SERVER", r"EASYJOB-TEST\SQLEXPRESS"),
        os.environ.get("EJ_DB_NAME",   "easyjob"),
        os.environ.get("EJ_DB_UID",    "sa"),
        os.environ.get("EJ_DB_PWD",    ""),
    )

    try:
        import pyodbc as _pyodbc
        cn = _pyodbc.connect(db_conn, timeout=6)
        cn.close()
    except Exception as exc:
        return _fail(f"Datenbankverbindung fehlgeschlagen: {exc}")

    try:
        client  = EjLiveClient(ej_url, ej_user, ej_pass)
        user_id = client.get_current_user_id()
    except Exception as exc:
        return _fail(f"EasyJob-Verbindung fehlgeschlagen: {exc}")

    if not user_id:
        return _fail("Benutzername oder Passwort falsch.")

    S.ej_url     = ej_url
    S.ej_user    = ej_user
    S.ej_pass    = ej_pass
    S.ej_db_conn = db_conn
    S.ej_user_id = user_id
    S.ej_client  = client

    request.session["authenticated"] = True
    request.session["ej_url"]        = ej_url
    request.session["ej_user"]       = ej_user
    request.session["ej_pass"]       = ej_pass
    request.session["db_conn"]       = db_conn
    request.session["ej_user_id"]    = user_id
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
