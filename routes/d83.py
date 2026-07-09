"""D83-Import-Routen: /d83 und alle /api/d83/*."""
import asyncio
import logging
import traceback

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from easyjob_api import EjLiveClient
from gaeb_parser import GaebProject, parse_gaeb
from state import get_session, templates

router = APIRouter()


# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

def _gaeb_groups(project: GaebProject, level: int = 0) -> list[dict]:
    """Extrahiert Hauptgruppen + Gruppen aus einem GAEB-Projekt.

    Immer von unten (Artikel-Ebene):
      level=0: HG = tiefste Kategorie (path[-1]), keine Untergruppen
      level=1: HG = path[-2], G = path[-1]
    """
    logging.info("_gaeb_groups: %d items, level=%d", len(project.items), level)
    for i, _it in enumerate(project.items[:3]):
        logging.info("  item[%d]: oz=%r path=%r", i, _it.oz, _it.category_path)

    hg_map: dict[str, dict] = {}
    for item in project.items:
        path     = item.category_path
        oz_parts = (item.oz or "").split(".")

        if not path:
            hg_label, hg_num = "(ohne Gruppe)", ""
            g_label,  g_num  = "", ""
            lb_label, lb_num = "", ""
        elif level == 1 and len(path) >= 2:
            hg_label = path[-2]
            hg_num   = ".".join(oz_parts[:-2]) if len(oz_parts) >= 3 else oz_parts[0]
            g_label  = path[-1]
            g_num    = ".".join(oz_parts[:-1]) if len(oz_parts) >= 2 else ""
            lb_label = path[-3] if len(path) >= 3 else ""
            lb_num   = ".".join(oz_parts[:-3]) if len(oz_parts) >= 4 else ""
        else:
            hg_label = path[-1]
            hg_num   = ".".join(oz_parts[:-1]) if len(oz_parts) >= 2 else oz_parts[0]
            g_label  = ""
            g_num    = ""
            lb_label = path[-2] if len(path) >= 2 else ""
            lb_num   = ".".join(oz_parts[:-2]) if len(oz_parts) >= 3 else ""

        if hg_label not in hg_map:
            hg_map[hg_label] = {
                "name": hg_label, "num": hg_num, "count": 0, "sub": {}, "positions": [],
                "parent_name": lb_label, "parent_num": lb_num,
            }
        hg_map[hg_label]["count"] += 1
        if g_label:
            if g_label not in hg_map[hg_label]["sub"]:
                hg_map[hg_label]["sub"][g_label] = {"name": g_label, "num": g_num, "count": 0, "positions": []}
            hg_map[hg_label]["sub"][g_label]["count"] += 1
            hg_map[hg_label]["sub"][g_label]["positions"].append({
                "oz": item.oz, "desc": item.description, "qty": item.qty, "unit": item.unit,
            })
        else:
            hg_map[hg_label]["positions"].append({
                "oz": item.oz, "desc": item.description, "qty": item.qty, "unit": item.unit,
            })

    result = []
    for hg in sorted(hg_map.values(), key=lambda x: (x["num"], x["name"])):
        hg["sub"] = sorted(hg["sub"].values(), key=lambda x: (x["num"], x["name"]))
        result.append(hg)
    logging.info("_gaeb_groups result: %s", [(h["name"], len(h["sub"])) for h in result])
    return result


def _groups_display(groups: list, mode: str) -> dict:
    return {"mode": mode, "groups": groups}


# ─── Routen ───────────────────────────────────────────────────────────────────

@router.get("/d83", response_class=HTMLResponse)
async def d83_page(request: Request):
    ss = get_session(request.session)

    if not ss.ej_client and ss.ej_url and ss.ej_user and ss.ej_pass:
        try:
            loop0 = asyncio.get_event_loop()
            ss.ej_client = await loop0.run_in_executor(
                None, lambda: EjLiveClient(ss.ej_url, ss.ej_user, ss.ej_pass)
            )
            logging.info("d83: EJ-Client auto-initialisiert (%s)", ss.ej_url)
        except Exception as _ei:
            logging.error("d83: EJ-Client Init fehlgeschlagen: %s", _ei)

    if ss.ej_client:
        loop = asyncio.get_event_loop()
        try:
            types = await loop.run_in_executor(None, ss.ej_client.project_types_list)
            if types:
                ss.d83_proj_types = types
            else:
                logging.warning("d83: project_types_list returned empty")
        except Exception as _e:
            logging.error("d83: project_types_list failed: %s", _e)
        try:
            import datetime as _dt
            today  = _dt.date.today().isoformat()
            events = await loop.run_in_executor(None, lambda: ss.ej_client.event_calendars_search(""))
            future = sorted(
                [e for e in (events or []) if (e.get("end") or "") >= today],
                key=lambda e: e.get("start") or ""
            )
            if future:
                ss.d83_events = future
        except Exception as _e:
            logging.error("d83: event_calendars_search failed: %s", _e)
    else:
        logging.warning("d83: ss.ej_client is None — no EJ connection")

    return templates.TemplateResponse(request, "d83.html", {
        "S":          ss,
        "groups":     ss.d83_groups,
        "proj_types": ss.d83_proj_types,
        "events":     ss.d83_events,
    })


@router.post("/api/d83/upload", response_class=HTMLResponse)
async def d83_upload(request: Request, file: UploadFile = File(...)):
    import pathlib as _pl, tempfile as _tf
    ss = get_session(request.session)
    try:
        data = await file.read()
        suf  = _pl.Path(file.filename or "upload").suffix or ".xml"
        with _tf.NamedTemporaryFile(suffix=suf, delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        ss.d83_project = parse_gaeb(tmp)
        ss.d83_name    = file.filename or "D83"
        level = 1 if ss.d83_import_mode == "groups" else 0
        ss.d83_groups  = _gaeb_groups(ss.d83_project, level)
        _pl.Path(tmp).unlink(missing_ok=True)
        ctx = _groups_display(ss.d83_groups, ss.d83_import_mode)
        return templates.TemplateResponse(request, "partials/d83_groups.html",
                                          {**ctx, "d83_name": ss.d83_name})
    except Exception as e:
        traceback.print_exc()
        return HTMLResponse(f'<div class="error-msg">Fehler beim Einlesen: {e}</div>')


@router.post("/api/d83/group/remove", response_class=HTMLResponse)
async def d83_group_remove(
    request: Request,
    hg_idx: int = Form(...),
    g_idx:  int = Form(-1),
):
    ss = get_session(request.session)
    if 0 <= hg_idx < len(ss.d83_groups):
        if g_idx < 0:
            ss.d83_groups.pop(hg_idx)
        else:
            sub = ss.d83_groups[hg_idx]["sub"]
            if 0 <= g_idx < len(sub):
                sub.pop(g_idx)
            if not sub and not ss.d83_groups[hg_idx].get("count", 0):
                ss.d83_groups.pop(hg_idx)
    ctx = _groups_display(ss.d83_groups, ss.d83_import_mode)
    return templates.TemplateResponse(request, "partials/d83_groups.html",
                                      {**ctx, "d83_name": ss.d83_name})


@router.get("/api/d83/groups-display", response_class=HTMLResponse)
async def d83_groups_display(request: Request, mode: str = "positions"):
    ss = get_session(request.session)
    level = 1 if mode == "groups" else 0
    prev_level = 1 if ss.d83_import_mode == "groups" else 0
    ss.d83_import_mode = mode
    if level != prev_level and ss.d83_project:
        ss.d83_groups = _gaeb_groups(ss.d83_project, level)
    ctx = _groups_display(ss.d83_groups, mode)
    return templates.TemplateResponse(request, "partials/d83_groups.html",
                                      {**ctx, "d83_name": ss.d83_name})


@router.post("/api/d83/position/remove", response_class=HTMLResponse)
async def d83_position_remove(
    request:  Request,
    hg_idx:   int = Form(...),
    g_idx:    int = Form(-1),
    pos_idx:  int = Form(...),
):
    ss = get_session(request.session)
    if 0 <= hg_idx < len(ss.d83_groups):
        hg = ss.d83_groups[hg_idx]
        if g_idx < 0:
            positions = hg.get("positions", [])
        else:
            sub = hg.get("sub", [])
            positions = sub[g_idx].get("positions", []) if 0 <= g_idx < len(sub) else []
        if 0 <= pos_idx < len(positions):
            positions.pop(pos_idx)
        if g_idx >= 0 and 0 <= g_idx < len(hg.get("sub", [])):
            hg["sub"][g_idx]["count"] = len(hg["sub"][g_idx].get("positions", []))
        hg["count"] = len(hg.get("positions", [])) + sum(
            s.get("count", 0) for s in hg.get("sub", [])
        )
    ctx = _groups_display(ss.d83_groups, ss.d83_import_mode)
    return templates.TemplateResponse(request, "partials/d83_groups.html",
                                      {**ctx, "d83_name": ss.d83_name})


@router.get("/api/d83/address-search")
async def d83_address_search(request: Request, q: str = "", limit: int = 12):
    ss = get_session(request.session)
    if not ss.ej_client:
        return JSONResponse([])
    if len(q) < 2:
        return JSONResponse([])
    try:
        loop    = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, lambda: ss.ej_client.addresses_search(q, limit)
        )
        return JSONResponse(results or [])
    except Exception as _e:
        logging.error("d83/address-search failed: %s", _e)
        return JSONResponse([])


@router.get("/api/d83/event-search")
async def d83_event_search(request: Request, q: str = "", limit: int = 15):
    ss = get_session(request.session)
    if not ss.ej_client:
        return JSONResponse([])
    try:
        loop    = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, lambda: ss.ej_client.event_calendars_search(q)
        )
        return JSONResponse((results or [])[:limit])
    except Exception as _e:
        logging.error("d83/event-search failed: %s", _e)
        return JSONResponse([])


@router.post("/api/d83/create-project", response_class=HTMLResponse)
async def d83_create_project(
    request:           Request,
    proj_name:         str  = Form(...),
    ref_number:        str  = Form(""),
    start_date:        str  = Form(...),
    end_date:          str  = Form(...),
    id_address:        int  = Form(1),
    id_delivery:       int  = Form(0),
    job_caption:       str  = Form("Job 1"),
    id_project_type:   int  = Form(9),
    id_event_calendar: int  = Form(0),
    import_mode:       str  = Form("positions"),
):
    ss = get_session(request.session)
    if not ss.d83_groups:
        return '<div class="error-msg">Bitte zuerst eine D83-Datei laden.</div>'
    if not ss.ej_client:
        return '<div class="error-msg">EJ-Verbindung nicht konfiguriert.</div>'

    groups = ss.d83_groups
    log: list[dict] = []

    id_payment_condition = 2
    if id_address:
        val = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ss.ej_client.get_address_payment_condition(id_address)
        )
        if val:
            id_payment_condition = val

    # 1. Projekt anlegen
    try:
        loop = asyncio.get_event_loop()
        def _create():
            body = {
                "IdProject":           0,
                "Caption":             proj_name,
                "StartDate":           f"{start_date}T00:00:00",
                "EndDate":             f"{end_date}T00:00:00",
                "IdUser_Arranger":     ss.ej_user_id,
                "IdAddress_Customer":  id_address,
                "IdContact_Customer":  0,
                "IdAddressDelivery":   id_delivery or id_address,
                "IdContactDelivery":   0,
                "IdProjectType":       id_project_type,
                "IdPriority":          2,
                "IdPaymentCondition":  id_payment_condition,
                "IdJobState":          2,
                "IdJobService":        2,
                "JobCaption":          job_caption,
                "IdStock":             1,
                "IdEventCalendar":     id_event_calendar,
                "Opportunity":         0,
                "IdCurrencyBase":      1,
                "IdCurrencyTarget":    1,
                "IdCostCenter":        0,
                "IdCompany":           1,
                "IdCompanyStructure":  0,
                "RefNumber":           ref_number,
            }
            return ss.ej_client._client._post(
                "/api.json/v2/rental/projects/create", body=body
            )
        resp       = await loop.run_in_executor(None, _create)
        id_project = resp.get("ID") or resp.get("IdProject") or 0
        log.append({"ok": True, "text": f'Projekt "{proj_name}" angelegt (ID: {id_project})', "indent": False})
    except Exception as e:
        log.append({"ok": False, "text": f"Projekt-Anlage fehlgeschlagen: {e}", "indent": False})
        return templates.TemplateResponse(request, "partials/d83_result.html", {"log": log})

    # 2. Job-ID per DB holen, Gruppen anlegen
    try:
        import pyodbc
        from datetime import datetime
        cn  = pyodbc.connect(ss.ej_db_conn)
        cur = cn.cursor()
        cur.execute(
            "SELECT TOP 1 IdJob, IdJobPartOutDefault, IdJobPartInDefault "
            "FROM Job WHERE IdProject = ? ORDER BY IdJob DESC",
            id_project,
        )
        row = cur.fetchone()
        if not row:
            log.append({"ok": False, "text": "Job nicht in DB gefunden — Gruppen übersprungen.", "indent": False})
            cn.close()
            return templates.TemplateResponse(request, "partials/d83_result.html", {"log": log})

        id_job, id_part_out, id_part_in = row
        log.append({"ok": True, "text": f'Job gefunden (ID: {id_job})', "indent": False})

        if ref_number:
            cur.execute("UPDATE Job SET RefNumber = ? WHERE IdJob = ?", ref_number, id_job)

        now = datetime.now()
        uid = ss.ej_user_id

        cur.execute("DELETE FROM StockType2JobGroup WHERE IdJob=?", id_job)
        cur.execute("DELETE FROM StockType2JobGroupParent WHERE IdJob=?", id_job)

        def _insert_hg(caption: str, sort: int) -> int:
            cur.execute(
                "INSERT INTO StockType2JobGroupParent "
                "(IdJob, Caption, SortOrder, UseGroupPrice, Price, Discount, "
                " CreationTime, UpdateTime, IdUserCreated, IdUserUpdated) "
                "VALUES (?, ?, ?, 0, 0, 0, ?, ?, ?, ?)",
                id_job, caption, sort, now, now, uid, uid,
            )
            cur.execute(
                "SELECT IdStockType2JobGroupParent FROM StockType2JobGroupParent "
                "WHERE IdJob=? AND SortOrder=?", id_job, sort,
            )
            return int(cur.fetchone()[0])

        def _insert_g(caption: str, sort: int, id_parent: int):
            cur.execute(
                "INSERT INTO StockType2JobGroup "
                "(IdJob, Caption, SortOrder, IdStockType2JobGroupParent, "
                " IdJobPartOutDefault, IdJobPartInDefault, "
                " CreationTime, UpdateTime, IdUserCreated, IdUserUpdated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                id_job, caption, sort, id_parent,
                id_part_out, id_part_in, now, now, uid, uid,
            )

        if import_mode == "groups":
            for sort_hg, grp in enumerate(groups, start=1):
                hg_cap = f'[{grp["num"]}] {grp["name"]}' if grp.get("num") else grp["name"]
                id_hg  = _insert_hg(hg_cap, sort_hg)
                log.append({"ok": True, "text": f'Hauptgruppe "{hg_cap}"', "indent": False})
                subs = grp.get("sub", [])
                for sort_g, sub in enumerate(subs, start=1):
                    g_cap = f'[{sub["num"]}] {sub["name"]}' if sub.get("num") else sub["name"]
                    _insert_g(g_cap, sort_g, id_hg)
                log.append({"ok": True, "text": f'{len(subs)} Gruppen angelegt', "indent": True})
        else:
            for sort_hg, grp in enumerate(groups, start=1):
                hg_cap = f'[{grp["num"]}] {grp["name"]}' if grp.get("num") else grp["name"]
                id_hg  = _insert_hg(hg_cap, sort_hg)
                log.append({"ok": True, "text": f'Hauptgruppe "{hg_cap}"', "indent": False})

                positions = list(grp.get("positions", []))
                for sub in grp.get("sub", []):
                    positions += sub.get("positions", [])
                for sort_g, pos in enumerate(positions, start=1):
                    g_cap = f'[{pos["oz"]}] {pos["desc"]}' if pos.get("oz") else pos["desc"]
                    _insert_g(g_cap, sort_g, id_hg)
                log.append({"ok": True, "text": f'{len(positions)} Gruppen angelegt', "indent": True})

        cn.commit()
        cn.close()
    except Exception as e:
        traceback.print_exc()
        log.append({"ok": False, "text": f"DB-Fehler: {e}", "indent": False})

    return templates.TemplateResponse(request, "partials/d83_result.html", {"log": log})


@router.post("/api/d83/settings", response_class=HTMLResponse)
async def d83_settings(request: Request, ej_db_conn: str = Form("")):
    ss = get_session(request.session)
    if ej_db_conn.strip():
        ss.ej_db_conn = ej_db_conn.strip()
    return '<span class="save-ok">✓ Gespeichert</span>'
