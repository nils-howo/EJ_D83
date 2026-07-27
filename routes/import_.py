"""Import-Workflow: /import und /api/import/* — kombinierter GAEB-Match + EJ-Projekt-Anlage."""
import asyncio
import logging
import pathlib
import tempfile
import traceback
from datetime import datetime

import pyodbc
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

import db as _db
from easyjob_api import EjLiveClient
from gaeb_parser import GaebProject, parse_gaeb
import math as _math
from matcher import (UnifiedMatcher, load_articles_db, load_resources_db,
                     make_article_from_ej, MatchResult,
                     parse_traverse_info, traverse_piece_count,
                     TRAVERSE_STANDARD_LENGTH_M, is_motor_position,
                     is_kalkulations_position)
import time as _time
from state import HAENGEPUNKT_NR
from state import get_session, templates

router = APIRouter()


# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

def _all_positions(blocks: list) -> list[dict]:
    """Alle aktiven Positions-Dicts aus einer Block-Liste (respektiert Alt-Wahl)."""
    result = []
    for block in blocks:
        if block.get("has_alt"):
            if block.get("render_primary", True):
                result.append(block["primary"])
            if block.get("render_alt", True) and block.get("alt"):
                result.append(block["alt"])
        else:
            result.append(block["primary"])
    return result


def _pos_dict(item) -> dict:
    lt = (item.long_text or "").strip()
    return {
        "oz":               item.oz,
        "desc":             item.description,
        "qty":              item.qty,
        "unit":             item.unit,
        "item_id":          item.item_id,
        "is_alt":           item.is_alt,
        "long_text":        lt,
        "long_text_images": item.long_text_images,
    }


def _to_blocks(positions: list[dict], alt_active: dict) -> list[dict]:
    blocks, i = [], 0
    while i < len(positions):
        pos = positions[i]
        if not pos.get("is_alt") and i + 1 < len(positions) and positions[i + 1].get("is_alt"):
            alt     = positions[i + 1]
            alt_key = f"{pos['item_id']}|{alt['item_id']}"
            chosen  = alt_active.get(alt_key, "primary")
            blocks.append({
                "has_alt":        True,
                "alt_key":        alt_key,
                "alt_choice":     chosen,
                "primary":        pos,
                "alt":            alt,
                "render_primary": chosen in ("primary", "both"),
                "render_alt":     chosen in ("alt", "both"),
            })
            i += 2
        else:
            blocks.append({"has_alt": False, "primary": pos})
            i += 1
    return blocks


def _import_gaeb_groups(project: GaebProject, level: int = 0, alt_active: dict | None = None) -> list[dict]:
    """Extrahiert Haupt-/Untergruppen aus dem GAEB-Projekt, mit item_id + Alt-Block-Paaren."""
    if alt_active is None:
        alt_active = {}

    # Hinweistexte (Remarks): positionsgebunden (vor der folgenden Position) bzw.
    # nachlaufend (der Gruppe zugeordnet). Nur Anzeige, werden nicht gebucht.
    remarks_by_item: dict[str, list] = {}
    trailing_remarks: list = []
    for r in project.remarks:
        rd = {"title": r.title, "long_text": r.long_text, "images": r.images}
        if r.next_item_id:
            remarks_by_item.setdefault(r.next_item_id, []).append(rd)
        else:
            trailing_remarks.append(((r.category_path[-1] if r.category_path else ""), rd))

    def _pd(it):
        d = _pos_dict(it)
        d["remarks"] = remarks_by_item.get(it.item_id, [])
        return d

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
                "name": hg_label, "num": hg_num, "count": 0, "sub": {}, "_positions": [],
                "parent_name": lb_label, "parent_num": lb_num, "remarks": [],
            }
        hg_map[hg_label]["count"] += 1
        if g_label:
            if g_label not in hg_map[hg_label]["sub"]:
                hg_map[hg_label]["sub"][g_label] = {
                    "name": g_label, "num": g_num, "count": 0, "_positions": [], "remarks": [],
                }
            hg_map[hg_label]["sub"][g_label]["count"] += 1
            hg_map[hg_label]["sub"][g_label]["_positions"].append(_pd(item))
        else:
            hg_map[hg_label]["_positions"].append(_pd(item))

    # Nachlaufende Hinweise (ohne folgende Position) der Gruppe zuordnen.
    for grp_label, rd in trailing_remarks:
        placed = False
        for hg in hg_map.values():
            if hg["name"] == grp_label:
                hg["remarks"].append(rd); placed = True; break
            for sub in hg["sub"].values():
                if sub["name"] == grp_label:
                    sub["remarks"].append(rd); placed = True; break
            if placed:
                break

    result = []
    for hg in sorted(hg_map.values(), key=lambda x: (x["num"], x["name"])):
        subs = sorted(hg["sub"].values(), key=lambda x: (x["num"], x["name"]))
        for sub in subs:
            sub["blocks"] = _to_blocks(sub.pop("_positions"), alt_active)
        hg["sub"]    = subs
        hg["blocks"] = _to_blocks(hg.pop("_positions"), alt_active)
        result.append(hg)
    return result


def _active_item_ids(ss) -> set[str]:
    """Item-IDs der aktuell aktiven Positionen — respektiert die Alt-Auswahl
    (render_primary/render_alt) aus ss.d83_groups. Dieselbe Logik wie beim Import."""
    ids: set[str] = set()
    for hg in ss.d83_groups:
        for pos in _all_positions(hg.get("blocks", [])):
            ids.add(pos["item_id"])
        for sub in hg.get("sub", []):
            for pos in _all_positions(sub.get("blocks", [])):
                ids.add(pos["item_id"])
    return ids


def _calc_import_metrics(ss, curves: dict | None = None) -> dict:
    from matcher import Resource as _Resource, resolve_time_factor
    if not ss.d83_project:
        return {}
    if curves is None:
        curves = _db.load_time_factor_curves_db()
    einsatztage = ss.einsatztage
    # Nur aktive Positionen zählen (deaktivierte Alternativen ausgeschlossen).
    # Fallback auf alle Items, falls Gruppen noch nicht aufgebaut sind.
    active = _active_item_ids(ss)
    items  = [it for it in ss.d83_project.items if not active or it.item_id in active]
    total = len(items)
    matched = confident = art_count = res_count = 0
    cost_mat = cost_pers = 0.0
    for it in items:
        mr  = ss.matches.get(it.item_id)
        bq  = ss.d83_booking_qtys.get(it.item_id) or {}
        qty = float(bq.get("qty", it.qty))
        if mr and mr.matched and mr.score > 0 and mr.method != "kalkpos":
            matched += 1
            if mr.score >= 85:
                confident += 1
            if mr.article:
                art_count += 1
                if getattr(mr.article, "mietpreis", 0):
                    factor = resolve_time_factor(curves, mr.article.id_time_factor, einsatztage)
                    cost_mat += qty * mr.article.mietpreis * factor
            elif isinstance(mr.matched, _Resource):
                res_count += 1
                if mr.matched.tagessatz:
                    cost_pers += qty * mr.matched.tagessatz
        for b in ss.bundles.get(it.item_id, []):
            bres = b.get("resource")
            bart = b.get("article")
            bqty = float(b.get("qty", 1))
            if bres and isinstance(bres, _Resource):
                res_count += 1
                if bres.tagessatz:
                    cost_pers += bqty * bres.tagessatz
            elif bart:
                art_count += 1
                if getattr(bart, "mietpreis", 0):
                    factor = resolve_time_factor(curves, bart.id_time_factor, einsatztage)
                    cost_mat += bqty * bart.mietpreis * factor
    return dict(
        total=total, matched=matched, confident=confident,
        art_count=art_count, res_count=res_count,
        cost_mat=cost_mat, cost_pers=cost_pers,
    )


def _import_ctx(ss) -> dict:
    curves = _db.load_time_factor_curves_db()
    return {
        "mode":               ss.d83_import_mode,
        "groups":             ss.d83_groups,
        "local_jobs":         ss.d83_local_jobs,
        "group_jobs":         ss.d83_group_jobs,
        "standard_job_name":  ss.d83_standard_job_name,
        "matches":            ss.matches,
        "bundles":            ss.bundles,
        "booking_qtys":       ss.d83_booking_qtys,
        "import_filename":    ss.import_filename,
        "alt_active":         ss.d83_alt_active,
        "einsatztage":        ss.einsatztage,
        "time_factor_curves": curves,
        "preliminaries":      (ss.d83_project.preliminaries if ss.d83_project else []),
        "imp_metrics":        _calc_import_metrics(ss, curves),
    }


# ─── Seiten-Route ─────────────────────────────────────────────────────────────

@router.get("/import", response_class=HTMLResponse)
async def import_page(request: Request):
    ss = get_session(request.session)

    if not ss.ej_client and ss.ej_url and ss.ej_user and ss.ej_pass:
        try:
            loop0 = asyncio.get_event_loop()
            ss.ej_client = await loop0.run_in_executor(
                None, lambda: EjLiveClient(ss.ej_url, ss.ej_user, ss.ej_pass)
            )
            logging.info("import: EJ-Client auto-initialisiert (%s)", ss.ej_url)
        except Exception as _ei:
            logging.error("import: EJ-Client Init fehlgeschlagen: %s", _ei)

    if ss.ej_client:
        loop = asyncio.get_event_loop()
        try:
            types = await loop.run_in_executor(None, ss.ej_client.project_types_list)
            if types:
                ss.d83_proj_types = types
        except Exception as _e:
            logging.error("import: project_types_list failed: %s", _e)
        try:
            import datetime as _dt
            today  = _dt.date.today().isoformat()
            events = await loop.run_in_executor(
                None, lambda: ss.ej_client.event_calendars_search("")
            )
            future = sorted(
                [e for e in (events or []) if (e.get("end") or "") >= today],
                key=lambda e: e.get("start") or "",
            )
            if future:
                ss.d83_events = future
        except Exception as _e:
            logging.error("import: event_calendars_search failed: %s", _e)

    return templates.TemplateResponse(request, "import.html", {
        "S":          ss,
        "proj_types": ss.d83_proj_types,
        "events":     ss.d83_events,
        **_import_ctx(ss),
    })


# ─── Upload + Auto-Matching ───────────────────────────────────────────────────

@router.post("/api/import/upload", response_class=HTMLResponse)
async def import_upload(request: Request, file: UploadFile = File(...)):
    ss = get_session(request.session)
    try:
        data = await file.read()
        suf  = pathlib.Path(file.filename or "upload").suffix or ".xml"
        with tempfile.NamedTemporaryFile(suffix=suf, delete=False) as tf:
            tf.write(data)
            tmp = tf.name

        project = parse_gaeb(tmp)
        pathlib.Path(tmp).unlink(missing_ok=True)

        ss.d83_project     = project
        ss.d83_name        = file.filename or "X83"
        ss.import_filename = file.filename or "X83"
        ss.x83_bytes       = data

        # Job-State zurücksetzen
        ss.d83_local_jobs        = []
        ss.d83_group_jobs        = {}
        ss.d83_next_lid          = 2
        ss.d83_standard_job_name = ""
        ss.d83_alt_active        = {}
        ss.d83_booking_qtys      = {}

        loop = asyncio.get_event_loop()

        # Matcher bei jedem Upload neu aufbauen, damit die Mapping-Quellen-Toggles
        # (Training-Mappings / GUI-Korrekturen) auf das Matching wirken.
        articles  = await loop.run_in_executor(None, load_articles_db)
        resources = await loop.run_in_executor(None, load_resources_db)
        ss.matcher = UnifiedMatcher(articles, resources)
        ss.matcher.apply_mapping_filter(ss.use_train_mappings, ss.use_gui_mappings)

        n_items = len(project.items)
        ss.progress.running = True
        ss.progress.done    = 0
        ss.progress.total   = n_items

        def _do_match():
            results:      dict = {}
            bundles:      dict = {}
            booking_qtys: dict = {}
            hp_art = None
            hp_idx = ss.matcher._num_to_idx.get(HAENGEPUNKT_NR)
            if hp_idx is not None:
                hp_art = ss.matcher._pool[hp_idx]
            for item in project.items:
                if is_kalkulations_position(item.description):
                    results[item.item_id] = MatchResult(None, 0, "kalkpos", False)
                    ss.progress.done += 1
                    continue
                try:
                    top = ss.matcher.match(
                        item.description,
                        category_path=item.category_path,
                        qty=item.qty,
                        unit=item.unit,
                        long_text=item.long_text,
                        limit=1,
                    )
                    if top:
                        mr, matched_art = top[0]
                        results[item.item_id] = mr

                        # Traverse-Berechnung: Stücklänge aus Beschreibung oder Standard 3 m
                        ti        = parse_traverse_info(item.description)
                        piece_len = (ti.length_m if ti and ti.length_m else None) or TRAVERSE_STANDARD_LENGTH_M
                        pieces    = traverse_piece_count(float(item.qty), item.unit or "", piece_len)
                        if pieces is not None:
                            booking_qtys[item.item_id] = {"qty": float(pieces), "lfm_converted": True, "piece_len": piece_len}
                        else:
                            booking_qtys[item.item_id] = {"qty": float(item.qty), "lfm_converted": False, "piece_len": None}

                        # Hängepunkt-Pauschale bei Motor-Positionen
                        if is_motor_position(item.description) and hp_art:
                            bundles.setdefault(item.item_id, []).append(
                                {"article": hp_art, "qty": item.qty}
                            )

                        for extra_num in ss.matcher.get_bundle_extras(item.description):
                            idx = ss.matcher._num_to_idx.get(extra_num)
                            if idx is not None:
                                extra_art = ss.matcher._pool[idx]
                                bundles.setdefault(item.item_id, []).append(
                                    {"article": extra_art, "qty": item.qty}
                                )
                except Exception:
                    pass
                ss.progress.done += 1
            return results, bundles, booking_qtys

        async def _run_bg():
            try:
                new_matches, new_bundles, new_bqtys = await loop.run_in_executor(None, _do_match)
                ss.matches         = new_matches
                ss.bundles         = new_bundles
                ss.d83_booking_qtys = new_bqtys
                level = 1 if ss.d83_import_mode == "groups" else 0
                ss.d83_groups = _import_gaeb_groups(project, level, ss.d83_alt_active)
                logging.info("import/upload: %d items, %d matches", n_items, len(ss.matches))
            except Exception:
                traceback.print_exc()
            finally:
                ss.progress.running = False

        asyncio.ensure_future(_run_bg())

        return HTMLResponse(
            '<div id="imp-progress"'
            ' hx-get="/api/import/match-progress"'
            ' hx-trigger="every 600ms"'
            ' hx-swap="outerHTML">'
            f'<p style="font-size:.85rem;color:#555;margin-bottom:6px">Analysiere {n_items} Positionen…</p>'
            '<div class="imp-spinner" style="margin:0 auto"></div>'
            '</div>'
        )
    except Exception as e:
        traceback.print_exc()
        return HTMLResponse(f'<div class="error-msg">Fehler beim Einlesen: {e}</div>')


# ─── Fortschritt ─────────────────────────────────────────────────────────────

@router.get("/api/import/match-progress", response_class=HTMLResponse)
async def import_match_progress(request: Request):
    ss = get_session(request.session)
    if ss.progress.running:
        done  = ss.progress.done
        total = ss.progress.total or 1
        pct   = int(done / total * 100)
        return HTMLResponse(
            '<div id="imp-progress"'
            ' hx-get="/api/import/match-progress"'
            ' hx-trigger="every 600ms"'
            ' hx-swap="outerHTML">'
            f'<p style="font-size:.85rem;color:#555;margin-bottom:6px">'
            f'{done} von {total} Positionen ({pct}%)</p>'
            '<div class="imp-spinner" style="margin:0 auto"></div>'
            '</div>'
        )
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


# ─── Alt-Toggle ───────────────────────────────────────────────────────────────

@router.post("/api/import/alt/{alt_key:path}", response_class=HTMLResponse)
async def import_alt_toggle(alt_key: str, request: Request, choice: str = Form(...)):
    ss = get_session(request.session)
    ss.d83_alt_active[alt_key] = choice
    # Gruppen neu aufbauen damit render_primary/render_alt aktuell sind
    if ss.d83_project:
        level = 1 if ss.d83_import_mode == "groups" else 0
        ss.d83_groups = _import_gaeb_groups(ss.d83_project, level, ss.d83_alt_active)
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


# ─── Match-Verwaltung ─────────────────────────────────────────────────────────

@router.get("/api/import/clear-match/{item_id}", response_class=HTMLResponse)
async def import_clear_match(request: Request, item_id: str):
    ss = get_session(request.session)
    ss.matches.pop(item_id, None)
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


@router.post("/api/import/set-match/{item_id}", response_class=HTMLResponse)
async def import_set_match(
    item_id:  str,
    request:  Request,
    ej_num:   str   = Form(default=""),
    raw_json: str   = Form(default=""),
    qty:      float = Form(default=1.0),
):
    import json as _json
    ss = get_session(request.session)
    if not ej_num or not raw_json:
        return '<p class="error-msg">Bitte zuerst einen Artikel auswählen.</p>'
    raw = _json.loads(raw_json)
    art = make_article_from_ej(raw, None, ss.matcher)
    ss.matches[item_id] = MatchResult(matched=art, score=99.0, method="manual", confident=True)
    # Buchungsmenge: Traverse-Check auf GAEB-Position
    if ss.d83_project:
        item = next((it for it in ss.d83_project.items if it.item_id == item_id), None)
        if item:
            ti        = parse_traverse_info(item.description)
            piece_len = (ti.length_m if ti and ti.length_m else None) or TRAVERSE_STANDARD_LENGTH_M
            pieces    = traverse_piece_count(float(item.qty), item.unit or "", piece_len)
            if pieces is not None:
                ss.d83_booking_qtys[item_id] = {"qty": float(pieces), "lfm_converted": True, "piece_len": piece_len}
            else:
                ss.d83_booking_qtys[item_id] = {"qty": float(item.qty), "lfm_converted": False, "piece_len": None}
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


@router.post("/api/import/add-match/{item_id}", response_class=HTMLResponse)
async def import_add_match(
    item_id:  str,
    request:  Request,
    ej_num:   str   = Form(default=""),
    raw_json: str   = Form(default=""),
    qty:      float = Form(default=1.0),
):
    import json as _json
    ss = get_session(request.session)
    if not ej_num or not raw_json:
        return '<p class="error-msg">Bitte zuerst einen Artikel auswählen.</p>'
    raw = _json.loads(raw_json)
    art = make_article_from_ej(raw, None, ss.matcher)
    bundle = ss.bundles.setdefault(item_id, [])
    bundle.append({"article": art, "qty": qty})
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


@router.post("/api/import/remove-bundle/{item_id}/{idx}", response_class=HTMLResponse)
async def import_remove_bundle(item_id: str, idx: int, request: Request):
    ss = get_session(request.session)
    bundle = ss.bundles.get(item_id, [])
    if 0 <= idx < len(bundle):
        bundle.pop(idx)
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


@router.post("/api/import/position/remove", response_class=HTMLResponse)
async def import_position_remove(request: Request, item_id: str = Form(...)):
    ss = get_session(request.session)
    for hg in ss.d83_groups:
        for blocks in [hg.get("blocks", [])] + [s.get("blocks", []) for s in hg.get("sub", [])]:
            for i, block in enumerate(blocks):
                ids = {block["primary"]["item_id"]}
                if block.get("alt"):
                    ids.add(block["alt"]["item_id"])
                if item_id in ids:
                    blocks.pop(i)
                    hg["count"] = max(0, hg["count"] - 1)
                    return templates.TemplateResponse(
                        request, "partials/import_groups.html", _import_ctx(ss)
                    )
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


@router.post("/api/import/group/remove", response_class=HTMLResponse)
async def import_group_remove(
    request: Request,
    hg_idx:  int = Form(...),
    g_idx:   int = Form(-1),
):
    ss = get_session(request.session)
    if 0 <= hg_idx < len(ss.d83_groups):
        if g_idx < 0:
            ss.d83_groups.pop(hg_idx)
        else:
            subs = ss.d83_groups[hg_idx].get("sub", [])
            if 0 <= g_idx < len(subs):
                subs.pop(g_idx)
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


# ─── Gruppen-Ansicht ─────────────────────────────────────────────────────────

@router.get("/api/import/groups-display", response_class=HTMLResponse)
async def import_groups_display(request: Request, mode: str = "positions"):
    ss = get_session(request.session)
    level     = 1 if mode == "groups" else 0
    prev_level = 1 if ss.d83_import_mode == "groups" else 0
    ss.d83_import_mode = mode
    if level != prev_level and ss.d83_project:
        ss.d83_groups     = _import_gaeb_groups(ss.d83_project, level, ss.d83_alt_active)
        ss.d83_group_jobs = {}
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


# ─── Job-Verwaltung ───────────────────────────────────────────────────────────

@router.post("/api/import/local-add-job", response_class=HTMLResponse)
async def import_local_add_job(
    request:  Request,
    job_name: str = Form(""),
    grp_name: str = Form(""),
):
    ss   = get_session(request.session)
    name = job_name.strip() or f"Job {ss.d83_next_lid}"
    lid  = ss.d83_next_lid
    ss.d83_local_jobs.append({"lid": lid, "name": name})
    ss.d83_next_lid += 1
    if grp_name:
        ss.d83_group_jobs[grp_name] = lid
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


@router.post("/api/import/local-assign", response_class=HTMLResponse)
async def import_local_assign(
    request:  Request,
    grp_name: str = Form(...),
    lid:      int = Form(...),
):
    ss = get_session(request.session)
    if lid == 1:
        ss.d83_group_jobs.pop(grp_name, None)
    else:
        ss.d83_group_jobs[grp_name] = lid
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


@router.post("/api/import/rename-local-job", response_class=HTMLResponse)
async def import_rename_local_job(
    request:  Request,
    lid:      int = Form(...),
    new_name: str = Form(...),
):
    ss   = get_session(request.session)
    name = new_name.strip()
    if name:
        for job in ss.d83_local_jobs:
            if job["lid"] == lid:
                job["name"] = name
                break
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


@router.post("/api/import/rename-standard-job", response_class=HTMLResponse)
async def import_rename_standard_job(
    request:  Request,
    new_name: str = Form(...),
):
    ss = get_session(request.session)
    ss.d83_standard_job_name = new_name.strip()
    return templates.TemplateResponse(
        request, "partials/import_groups.html", _import_ctx(ss)
    )


@router.get("/api/import/ej/dialog/{item_id}", response_class=HTMLResponse)
async def import_ej_dialog(item_id: str, request: Request):
    import json as _json
    ss = get_session(request.session)
    if not ss.d83_project:
        raise HTTPException(404, "Kein Projekt geladen")
    item = next((it for it in ss.d83_project.items if it.item_id == item_id), None)
    if not item:
        raise HTTPException(404, "Position nicht gefunden")

    suggestions: list[dict] = []
    suggestions_error: str = ""
    if ss.matcher:
        try:
            loop = asyncio.get_event_loop()
            top = await loop.run_in_executor(
                None,
                lambda: ss.matcher.match(
                    item.match_query, limit=5,
                    qty=float(item.qty), unit=item.unit,
                ),
            )
            for mr, art in top:
                raw_score = mr.score if isinstance(mr.score, (int, float)) else 0
                suggestions.append({
                    "nummer":      getattr(art, "nummer", ""),
                    "bezeichnung": getattr(art, "bezeichnung", ""),
                    "kategorie":   getattr(art, "warengruppe", ""),
                    "inv":         str(getattr(art, "mietinventar", "")),
                    "score":       min(int(raw_score), 100),
                    "_raw_json":   _json.dumps({
                        "Number":      getattr(art, "nummer", ""),
                        "Caption":     getattr(art, "bezeichnung", ""),
                        "IdStockType": 0,
                    }),
                })
        except Exception as _exc:
            suggestions_error = str(_exc)

    return templates.TemplateResponse(request, "partials/ej_dialog.html", {
        "item_id":           item_id,
        "item_desc":         item.description,
        "default_q":         item.description[:60],
        "default_qty":       float(item.qty),
        "results":           [],
        "suggestions":       suggestions,
        "suggestions_error": suggestions_error,
        "form_action":       f"/api/import/set-match/{item_id}",
        "form_target":       "#import-groups",
        "add_action":        f"/api/import/add-match/{item_id}",
        "search_url":        f"/api/import/ej/search/{item_id}",
    })


@router.get("/api/import/ej/search/{item_id}", response_class=HTMLResponse)
async def import_ej_search(item_id: str, request: Request, q: str = ""):
    import json as _json
    ss = get_session(request.session)
    if not ss.ej_client or not q.strip():
        return templates.TemplateResponse(request, "partials/ej_results.html", {
            "results": [], "item_id": item_id,
        })
    ck = q.strip().lower()
    if ck not in ss.ej_cache:
        loop = asyncio.get_event_loop()
        ss.ej_cache[ck] = await loop.run_in_executor(
            None, lambda: ss.ej_client.search(q, limit=40)
        )
    raw = ss.ej_cache.get(ck, [])
    results = []
    for r in raw:
        num = str(r.get("Number", ""))
        inv = ""
        if ss.matcher:
            lidx = ss.matcher._num_to_idx.get(num)
            if lidx is not None:
                inv = str(ss.matcher._pool[lidx].mietinventar)
        results.append({
            "nummer":      num,
            "bezeichnung": r.get("Caption", ""),
            "kategorie":   r.get("Category", ""),
            "inv":         inv,
            "_raw_json":   _json.dumps(r),
        })
    return templates.TemplateResponse(request, "partials/ej_results.html", {
        "results": results, "item_id": item_id,
    })


# ─── Ressourcen-Dialog ────────────────────────────────────────────────────────

@router.get("/api/import/resource/dialog/{item_id}", response_class=HTMLResponse)
async def import_resource_dialog(item_id: str, request: Request):
    from matcher import Resource as _Resource
    ss = get_session(request.session)
    if not ss.d83_project or not ss.matcher:
        raise HTTPException(404, "Kein Projekt geladen")
    item = next((it for it in ss.d83_project.items if it.item_id == item_id), None)
    if not item:
        raise HTTPException(404, "Position nicht gefunden")

    suggestions = []
    try:
        loop = asyncio.get_event_loop()
        # match() mit großem Limit → Ressourcen mit GUI-Boost floaten nach oben
        top = await loop.run_in_executor(
            None,
            lambda: ss.matcher.match(item.match_query, limit=30),
        )
        for mr, obj in top:
            if isinstance(obj, _Resource):
                suggestions.append({
                    "id":            obj.id,
                    "funktion":      obj.funktion,
                    "ressourcenart": obj.ressourcenart,
                    "tagessatz":     obj.tagessatz,
                    "score":         min(int(mr.score), 100),
                })
                if len(suggestions) >= 5:
                    break
    except Exception:
        pass

    # "Ersetzen" anbieten sobald irgendetwas als Primär-Match gesetzt ist
    cur_match = ss.matches.get(item_id)
    replace_action = None
    if cur_match and cur_match.matched and cur_match.score > 0 and cur_match.method != "kalkpos":
        replace_action = f"/api/import/set-resource/{item_id}"

    return templates.TemplateResponse(request, "partials/resource_dialog.html", {
        "item_id":        item_id,
        "item_desc":      item.description,
        "suggestions":    suggestions,
        "default_qty":    float(item.qty),
        "search_url":     f"/api/import/resource/search/{item_id}",
        "add_action":     f"/api/import/add-resource/{item_id}",
        "replace_action": replace_action,
    })


@router.get("/api/import/resource/search/{item_id}", response_class=HTMLResponse)
async def import_resource_search(item_id: str, request: Request, q: str = ""):
    from matcher import Resource as _Resource
    ss = get_session(request.session)
    results = []
    q = q.strip().lower()
    if ss.matcher:
        for obj in ss.matcher._pool:
            if isinstance(obj, _Resource) and (not q or q in obj.funktion.lower()):
                results.append({
                    "id":            obj.id,
                    "funktion":      obj.funktion,
                    "ressourcenart": obj.ressourcenart,
                    "tagessatz":     obj.tagessatz,
                })
                if len(results) >= 50:
                    break
    return templates.TemplateResponse(request, "partials/resource_results.html", {
        "results": results,
    })


@router.post("/api/import/add-resource/{item_id}", response_class=HTMLResponse)
async def import_add_resource(
    item_id:     str,
    request:     Request,
    resource_id: int   = Form(...),
    qty:         float = Form(1.0),
):
    from matcher import Resource as _Resource
    ss = get_session(request.session)
    res = next(
        (obj for obj in ss.matcher._pool if isinstance(obj, _Resource) and obj.id == resource_id),
        None,
    )
    if res is None:
        return HTMLResponse('<p class="error-msg">Ressource nicht gefunden.</p>')
    ss.bundles.setdefault(item_id, []).append({"resource": res, "qty": qty})
    return templates.TemplateResponse(request, "partials/import_groups.html", _import_ctx(ss))


@router.post("/api/import/set-resource/{item_id}", response_class=HTMLResponse)
async def import_set_resource(
    item_id:     str,
    request:     Request,
    resource_id: int   = Form(...),
    qty:         float = Form(1.0),
):
    from matcher import Resource as _Resource
    from db import save_gui_resource_mapping
    ss = get_session(request.session)
    if not ss.matcher:
        return HTMLResponse('<p class="error-msg">Kein Matcher geladen.</p>')
    res = next(
        (obj for obj in ss.matcher._pool if isinstance(obj, _Resource) and obj.id == resource_id),
        None,
    )
    if res is None:
        return HTMLResponse('<p class="error-msg">Ressource nicht gefunden.</p>')
    ss.matches[item_id] = MatchResult(matched=res, score=99.0, method="manual", confident=True)
    ss.d83_booking_qtys[item_id] = {"qty": float(qty), "lfm_converted": False, "piece_len": None}
    # Ressource-Mapping lernen
    if ss.d83_project:
        item = next((it for it in ss.d83_project.items if it.item_id == item_id), None)
        if item and item.description.strip():
            save_gui_resource_mapping(item.description, resource_id)
            ss.matcher.add_learned_resource(item.description, resource_id)
    return templates.TemplateResponse(request, "partials/import_groups.html", _import_ctx(ss))


@router.post("/api/import/set-qty/{item_id}", response_class=HTMLResponse)
async def import_set_qty(item_id: str, request: Request, qty: float = Form(...)):
    ss = get_session(request.session)
    bq = ss.d83_booking_qtys.get(item_id)
    if bq:
        bq["qty"] = qty
    else:
        ss.d83_booking_qtys[item_id] = {"qty": qty, "lfm_converted": False, "piece_len": None}
    return templates.TemplateResponse(request, "partials/import_groups.html", _import_ctx(ss))


@router.get("/api/import/project-search")
async def import_project_search(request: Request, q: str = "", limit: int = 15):
    """Sucht bestehende EJ-Projekte für den Modus 'Jobs zu bestehendem Projekt'."""
    ss = get_session(request.session)
    if not ss.ej_client or len(q) < 2:
        return JSONResponse([])
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, lambda: ss.ej_client.projects_search(q, limit)
        )
        return JSONResponse(results or [])
    except Exception as _e:
        logging.error("import/project-search failed: %s", _e)
        return JSONResponse([])


def _parse_einsatztage(raw, fallback: float = 2.0) -> float:
    """Robustes Parsen der Einsatztage: akzeptiert Punkt und Komma als Dezimaltrenner
    (z.B. '2,5' oder '2.5'). Leere/ungültige Eingabe → fallback. Minimum 0.1."""
    try:
        v = float(str(raw).replace(",", ".").strip())
    except (ValueError, TypeError):
        return fallback
    return max(0.1, v)


@router.post("/api/import/set-einsatztage", response_class=HTMLResponse)
async def import_set_einsatztage(request: Request, einsatztage: str = Form(...)):
    """Setzt die Einsatztage (Berechnungstage) für die Preisvorschau. Wird bei der
    Projekt-Anlage 1:1 als Job.CommitmentDays übernommen, damit EJ Artikel mit
    Berechnungsgrundlage (TimeFactor-Progression) korrekt einbucht. Dezimal erlaubt."""
    ss = get_session(request.session)
    ss.einsatztage = _parse_einsatztage(einsatztage, ss.einsatztage)
    return templates.TemplateResponse(request, "partials/import_groups.html", _import_ctx(ss))


@router.post("/api/import/set-res-price/{item_id}", response_class=HTMLResponse)
async def import_set_res_price(item_id: str, request: Request, price: float = Form(...)):
    ss = get_session(request.session)
    bq = ss.d83_booking_qtys.get(item_id)
    if bq:
        bq["day_pay"] = price
    else:
        ss.d83_booking_qtys[item_id] = {"qty": 1.0, "lfm_converted": False, "piece_len": None, "day_pay": price}
    return templates.TemplateResponse(request, "partials/import_groups.html", _import_ctx(ss))


@router.post("/api/import/set-bundle-qty/{item_id}/{idx}", response_class=HTMLResponse)
async def import_set_bundle_qty(
    item_id: str,
    idx:     int,
    request: Request,
    qty:     float = Form(...),
):
    ss     = get_session(request.session)
    bundle = ss.bundles.get(item_id, [])
    if 0 <= idx < len(bundle):
        bundle[idx]["qty"] = qty
    return templates.TemplateResponse(request, "partials/import_groups.html", _import_ctx(ss))


# ─── D84 Hilfsfunktion: Datei bereinigen ────────────────────────────────────

def _clean_gaeb_for_export(root, ns: str) -> None:
    """Minimaler D84-Export: nur Item-ID + Qty + UP + IT, alle Texte/Bilder/Metadaten raus.

    Das andere Tool lädt erst D83 (Beschreibungen) und dann D84 (Preise) dazu —
    deshalb braucht die D84 nur die Kostendaten, nicht die Langtexte.
    """
    ns_prefix = f'{{{ns}}}' if ns else ''

    # Pro Item: alles außer Qty, UP, IT entfernen
    ITEM_KEEP = {'Qty', 'UP', 'IT'}
    for item_el in root.iter(f'{ns_prefix}Item'):
        for child in list(item_el):
            local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if local not in ITEM_KEEP:
                item_el.remove(child)

    # Überall: Text- und Metadaten-Blöcke entfernen, die im D84 (Angebot) nichts
    # verloren haben — die D84 braucht nur OZ/Preise/Bieterangaben.
    REMOVE_ANYWHERE = {
        'image', 'UPBkdn', 'CtlgAssign', 'CtlgCode', 'CtlgID', 'CtlgBkdn',
        'AddText', 'CompleteText', 'OutlineText', 'DetailTxt', 'TextComplement',
        'ComplTSB', 'Description', 'LongText', 'OutlTxt', 'TextOutlTxt',
        'OutlineAddText', 'DetailAddText', 'Text', 'ComplBody',
        # Ausschreibungs-Metadaten (nicht Teil des Angebots):
        'Remark', 'LblTx', 'Ctlg', 'CtlgName', 'CtlgType',
        # überflüssige Header-Labels:
        'LblUPComp1', 'LblUPComp2', 'LblUPComp3', 'LblUPComp4',
        'NoUPComps', 'OutlCompl', 'CurLbl', 'LblBoQ',
    }
    for parent in list(root.iter()):
        local_p = parent.tag.split('}')[-1] if '}' in parent.tag else parent.tag
        if local_p == 'Item':
            continue  # Item-Kinder wurden oben schon behandelt
        for child in list(parent):
            local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if local in REMOVE_ANYWHERE:
                parent.remove(child)


# D84-Export nur über Projektseite (/api/projects/{id}/export-d84)


# ─── Projekt anlegen (Background-Task + Progress) ────────────────────────────

async def _do_create_bg(
    ss,
    proj_name:            str,
    ref_number:           str,
    start_date:           str,
    end_date:             str,
    id_address:           int,
    id_delivery:          int,
    job_caption:          str,
    id_project_type:      int,
    id_event_calendar:    int,
    import_mode:          str,
    id_payment_condition: int,
    einsatztage:          float = 2.0,
    existing_project_id:  int = 0,
    only_groups:          bool = False,
    id_contact:           int = 0,
) -> None:
    """Läuft im Hintergrund; schreibt Ergebnis in ss.create_progress.log.

    existing_project_id > 0 → Modus 'Jobs zu bestehendem Projekt hinzufügen':
    Es wird kein neues Projekt angelegt; alle Jobs (Standard + Extra) werden als
    NEUE Jobs in das bestehende Projekt eingefügt.
    """
    is_existing = existing_project_id > 0
    cp   = ss.create_progress
    log: list[dict] = []
    loop = asyncio.get_running_loop()
    cn   = None

    try:
        # ── 1. Projekt anlegen (oder bestehendes verwenden) ──────────────────
        if is_existing:
            id_project = existing_project_id
            cp.step = "Projekt prüfen"
            log.append({"ok": True, "text": f'Bestehendes Projekt: "{proj_name}" (ID: {id_project})', "indent": False})
        else:
            cp.step = "Projekt anlegen"
            try:
                def _create():
                    body = {
                        "IdProject":          0,
                        "Caption":            proj_name,
                        "StartDate":          f"{start_date}T00:00:00",
                        "EndDate":            f"{end_date}T00:00:00",
                        "IdUser_Arranger":    ss.ej_user_id,
                        "IdAddress_Customer": id_address,
                        # projects/create ignoriert IdContact_Customer komplett (wird
                        # nach der Anlage gezielt per DB gesetzt, siehe unten). Ein
                        # IdContactDelivery != 0 löst in EJ sogar einen HTTP 500 aus —
                        # daher beide Kontakt-Felder hier bewusst auf 0.
                        "IdContact_Customer": 0,
                        "IdAddressDelivery":  id_delivery or id_address,
                        "IdContactDelivery":  0,
                        "IdProjectType":      id_project_type,
                        "IdPriority":         2,
                        "IdPaymentCondition": id_payment_condition,
                        "IdJobState":         2,
                        "IdJobService":       2,
                        "JobCaption":         job_caption,
                        "IdStock":            1,
                        "IdEventCalendar":    id_event_calendar,
                        "Opportunity":        0,
                        "IdCurrencyBase":     1,
                        "IdCurrencyTarget":   1,
                        "IdCostCenter":       0,
                        "IdCompany":          1,
                        "IdCompanyStructure": 0,
                        "RefNumber":          ref_number,
                    }
                    return ss.ej_client._client._post(
                        "/api.json/v2/rental/projects/create", body=body
                    )

                resp       = await loop.run_in_executor(None, _create)
                id_project = resp.get("ID") or resp.get("IdProject") or 0
                # Absicherung: Ohne gültige Projekt-ID (z.B. fehlendes Anlage-Recht,
                # das EJ nur mit 200 ohne ID quittiert) sofort abbrechen — sonst
                # würden Jobs/Buchungen gegen eine ungültige ID (0) laufen.
                if not id_project:
                    log.append({"ok": False, "text": "Projekt-Anlage fehlgeschlagen: "
                                "keine Projekt-ID von EasyJob erhalten "
                                "(fehlt evtl. das Recht zum Anlegen?)", "indent": False})
                    return
                log.append({"ok": True, "text": f'Projekt "{proj_name}" angelegt (ID: {id_project})', "indent": False})
            except Exception as e:
                log.append({"ok": False, "text": f"Projekt-Anlage fehlgeschlagen: {e}", "indent": False})
                return

        # ── 2. Jobs anlegen ──────────────────────────────────────────────────
        # Neues Projekt: der erste Job wurde automatisch mit angelegt (wiederverwenden).
        # Bestehendes Projekt: alle Jobs (auch der Standard-Job) werden neu angelegt.
        cp.step = "Jobs anlegen"
        first_job_id: int = 0
        lid_map: dict[int, int] = {}
        all_job_ids: set[int] = set()

        try:
            cn  = pyodbc.connect(ss.ej_db_conn)
            cur = cn.cursor()
            id_deliv = id_delivery or id_address

            if is_existing:
                resp_std = await loop.run_in_executor(
                    None,
                    lambda: ss.ej_client.jobs_create(
                        id_project=id_project,
                        caption=job_caption,
                        start_date=start_date,
                        end_date=end_date,
                        id_address_delivery=id_deliv,
                    ),
                )
                first_job_id = int(resp_std.get("ID") or resp_std.get("IdJob") or 0)
                if not first_job_id:
                    log.append({"ok": False, "text": f"Standard-Job konnte nicht angelegt werden: {resp_std}", "indent": False})
                    return
                log.append({"ok": True, "text": f'Job "{job_caption}" angelegt (ID: {first_job_id})', "indent": False})
            else:
                cur.execute(
                    "SELECT TOP 1 IdJob FROM Job WHERE IdProject = ? ORDER BY IdJob ASC",
                    id_project,
                )
                row = cur.fetchone()
                if not row:
                    log.append({"ok": False, "text": "Erster Job nicht in DB gefunden.", "indent": False})
                    return
                first_job_id = int(row[0])
                log.append({"ok": True, "text": f'Erster Job: "{job_caption}" (ID: {first_job_id})', "indent": False})

            lid_map = {1: first_job_id}
            # Extra-Jobs basieren auf dem Job-Namen (bestehend) bzw. Projektnamen (neu)
            job_name_base = job_caption if is_existing else proj_name

            for extra in ss.d83_local_jobs:
                caption = f"{job_name_base} {extra['name']}"
                resp_j  = await loop.run_in_executor(
                    None,
                    lambda c=caption: ss.ej_client.jobs_create(
                        id_project=id_project,
                        caption=c,
                        start_date=start_date,
                        end_date=end_date,
                        id_address_delivery=id_deliv,
                    ),
                )
                new_id = int(resp_j.get("ID") or resp_j.get("IdJob") or 0)
                if not new_id:
                    raise ValueError(f"API gab keine Job-ID zurück: {resp_j}")
                lid_map[extra["lid"]] = new_id
                log.append({"ok": True, "text": f'Job "{caption}" angelegt (ID: {new_id})', "indent": False})

            all_job_ids = set(lid_map.values())

        except Exception as e:
            traceback.print_exc()
            log.append({"ok": False, "text": f"Fehler (Job-Anlage): {e}", "indent": False})
            return

        # ── 3. Gruppen eintragen ─────────────────────────────────────────────
        cp.step = "Gruppen eintragen"
        try:
            now = datetime.now()
            uid = ss.ej_user_id

            # CommitmentDays (Einsatztage) muss VOR dem Artikel-Einbuchen (Schritt 4)
            # gesetzt sein, damit EJ Artikel mit Berechnungsgrundlage (TimeFactor)
            # beim Einbuchen automatisch mit dem richtigen Progressions-Faktor bepreist.
            # Gezielt nur die eigenen (neuen) Jobs, damit im Bestehend-Modus keine
            # fremden Jobs des Projekts verändert werden.
            set_clauses = ["CommitmentDays = ?"]
            params: list = [einsatztage]
            if ref_number and not is_existing:
                set_clauses.append("RefNumber = ?")
                params.append(ref_number)
            placeholders = ",".join("?" for _ in all_job_ids)
            params.extend(all_job_ids)
            cur.execute(
                f"UPDATE Job SET {', '.join(set_clauses)} WHERE IdJob IN ({placeholders})",
                *params,
            )
            log.append({"ok": True, "text": f"Einsatztage: {einsatztage:g}", "indent": False})

            # Kundenkontakt nachtragen: der projects/create-Endpoint setzt zwar
            # IdAddress_Customer, ignoriert aber IdContact_Customer. Deshalb hier
            # gezielt am Projekt setzen (gleiche DB-Verbindung, nur bei neuem
            # Projekt + tatsächlich gewähltem Kontakt).
            if id_contact and not is_existing:
                cur.execute(
                    "UPDATE Project SET IdContact_Customer = ? WHERE IdProject = ?",
                    id_contact, id_project,
                )
                log.append({"ok": True, "text": f"Kundenkontakt gesetzt (ID: {id_contact})", "indent": False})

            for job_id in all_job_ids:
                cur.execute("DELETE FROM StockType2JobGroup WHERE IdJob=?", job_id)
                cur.execute("DELETE FROM StockType2JobGroupParent WHERE IdJob=?", job_id)

            def _insert_hg(job_id: int, caption: str, sort: int) -> int:
                cur.execute(
                    "INSERT INTO StockType2JobGroupParent "
                    "(IdJob, Caption, SortOrder, UseGroupPrice, Price, Discount, "
                    " CreationTime, UpdateTime, IdUserCreated, IdUserUpdated) "
                    "OUTPUT INSERTED.IdStockType2JobGroupParent "
                    "VALUES (?, ?, ?, 0, 0, 0, ?, ?, ?, ?)",
                    job_id, caption, sort, now, now, uid, uid,
                )
                return int(cur.fetchone()[0])

            def _insert_g(job_id: int, caption: str, sort: int, id_parent: int):
                cur.execute(
                    "INSERT INTO StockType2JobGroup "
                    "(IdJob, Caption, SortOrder, IdStockType2JobGroupParent, "
                    " CreationTime, UpdateTime, IdUserCreated, IdUserUpdated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    job_id, caption, sort, id_parent,
                    now, now, uid, uid,
                )

            job_sort:   dict[int, int] = {jid: 0 for jid in all_job_ids}
            job_g_sort: dict[int, int] = {jid: 0 for jid in all_job_ids}
            job_names = {1: job_caption}
            for extra in ss.d83_local_jobs:
                job_names[extra["lid"]] = extra["name"]

            for grp in ss.d83_groups:
                lid    = ss.d83_group_jobs.get(grp["name"], 1)
                job_id = lid_map.get(lid, first_job_id)
                j_name = job_names.get(lid, job_caption)
                job_sort[job_id] += 1

                hg_cap = f'[{grp["num"]}] {grp["name"]}' if grp.get("num") else grp["name"]
                id_hg  = _insert_hg(job_id, hg_cap, job_sort[job_id])
                log.append({"ok": True, "text": f'[{j_name}] {hg_cap}', "indent": False})

                if import_mode == "groups":
                    subs = grp.get("sub", [])
                    for sub in subs:
                        g_cap = f'[{sub["num"]}] {sub["name"]}' if sub.get("num") else sub["name"]
                        job_g_sort[job_id] += 1
                        _insert_g(job_id, g_cap, job_g_sort[job_id], id_hg)
                    log.append({"ok": True, "text": f'{len(subs)} Untergruppen', "indent": True})
                else:
                    positions = _all_positions(grp.get("blocks", []))
                    for sub in grp.get("sub", []):
                        positions += _all_positions(sub.get("blocks", []))
                    for pos in positions:
                        g_cap = f'[{pos["oz"]}] {pos["desc"]}' if pos.get("oz") else pos["desc"]
                        job_g_sort[job_id] += 1
                        _insert_g(job_id, g_cap, job_g_sort[job_id], id_hg)
                    log.append({"ok": True, "text": f'{len(positions)} Positionen', "indent": True})

            cn.commit()
            log.append({"ok": True, "text": "Gruppen angelegt ✓", "indent": False})

        except Exception as e:
            traceback.print_exc()
            log.append({"ok": False, "text": f"DB-Fehler (Gruppen): {e}", "indent": False})
            return

        # ── 4. Artikel einbuchen (parallel) ─────────────────────────────────
        bookings: list[dict] = []
        res_bookings: list[dict] = []   # Ressourcen für den lokalen Snapshot

        try:
            # oz → hg_name für Job-Zuweisung
            oz_to_hg: dict[str, str] = {}
            oz_to_sub_cap: dict[str, str] = {}
            for hg in ss.d83_groups:
                hg_name_g = hg["name"]
                for pos in _all_positions(hg.get("blocks", [])):
                    oz = pos.get("oz", "")
                    if oz:
                        oz_to_hg[oz] = hg_name_g
                for sub in hg.get("sub", []):
                    sub_cap = f'[{sub["num"]}] {sub["name"]}' if sub.get("num") else sub["name"]
                    for pos in _all_positions(sub.get("blocks", [])):
                        oz = pos.get("oz", "")
                        if oz:
                            oz_to_hg[oz] = hg_name_g
                            oz_to_sub_cap[oz] = sub_cap

            # caption → IdStockType2JobGroup je Job
            cap_to_gid: dict[tuple, int] = {}
            for jid in all_job_ids:
                cur.execute(
                    "SELECT Caption, IdStockType2JobGroup FROM StockType2JobGroup WHERE IdJob=?",
                    (jid,),
                )
                for r in cur.fetchall():
                    cap_to_gid[(jid, r[0])] = r[1]

            # Aktive Positionen (respektiert Alt-Auswahl) — gleiche Logik wie Metrik
            active_ids: set[str] = _active_item_ids(ss)
            # Nur-Gruppen-Modus: keine Positionen buchen (weder Artikel noch Personal),
            # nur die in Schritt 3 angelegte Gruppenstruktur bleibt bestehen.
            if only_groups:
                active_ids = set()
                log.append({"ok": True, "text": "Nur-Gruppen-Modus: kein Material/Personal gebucht", "indent": False})

            # Sync: alle DB-Lookups + Aufbau Buchungs-Taskliste (schnell)
            book_tasks: list[tuple] = []
            for item in ss.d83_project.items:
                if item.item_id not in active_ids:
                    continue
                mr = ss.matches.get(item.item_id)
                if not mr or not mr.article:
                    continue

                id_st = mr.article.ej_id
                if not id_st:
                    log.append({
                        "ok": False,
                        "text": f"Artikel {mr.article.nummer} hat keine EJ-ID — bitte Sync ausführen",
                        "indent": True,
                    })
                    continue
                hg_name = oz_to_hg.get(item.oz or "")
                lid_i   = ss.d83_group_jobs.get(hg_name, 1) if hg_name else 1
                job_i   = lid_map.get(lid_i, first_job_id)
                if import_mode == "positions":
                    g_cap = f'[{item.oz}] {item.description}' if item.oz else item.description
                else:
                    g_cap = oz_to_sub_cap.get(item.oz or "")
                grp_i = cap_to_gid.get((job_i, g_cap), 0) if g_cap else 0

                bq_i  = ss.d83_booking_qtys.get(item.item_id, {})
                qty_i = float(bq_i.get("qty", item.qty))
                book_tasks.append((item, mr.article.nummer, id_st, job_i, grp_i, qty_i))

            # Sequenziell mit Progress-Tracking (EJ-Server verträgt keine gleichzeitigen Buchungen)
            cp.step  = "Artikel buchen"
            cp.total = len(book_tasks)
            cp.done  = 0
            sem = asyncio.Semaphore(1)

            async def _book_one(item, art_num, id_st, job_i, grp_i, qty_i):
                async with sem:
                    try:
                        resp_b = await loop.run_in_executor(
                            None,
                            lambda: ss.ej_client.items_book(id_st, job_i, qty_i, grp_i),
                        )
                        cp.done += 1
                        s2j = resp_b.get("IdStockType2Job") or resp_b.get("ID") or 0
                        return {
                            "item_id":          item.item_id,
                            "oz":               item.oz,
                            "description":      item.description,
                            "art_num":          art_num,
                            "ej_stock_type_id": id_st,
                            "ej_s2j_id":        int(s2j) if s2j else 0,
                            "ej_group_id":      grp_i,
                            "qty":              qty_i,
                            "_ok":              True,
                        }
                    except Exception as _be:
                        cp.done += 1
                        return {"_ok": False, "_err": f'[{item.oz}] Buchung fehlgeschlagen: {_be}'}

            results = await asyncio.gather(*[_book_one(*t) for t in book_tasks])

            for r in results:
                if r is None:
                    continue
                if r.get("_ok"):
                    bookings.append(r)
                else:
                    log.append({"ok": False, "text": r["_err"], "indent": True})

            book_count = len(bookings)
            log.append({"ok": True, "text": f'{book_count} Artikel eingebucht ✓', "indent": False})

            # ── 4b. Ressourcen buchen (ResourceFunctionAllocation) ───────────
            cp.step = "Ressourcen buchen"
            from matcher import Resource as _Resource
            res_count = 0

            from datetime import timedelta as _td
            dt_start = datetime.fromisoformat(start_date)
            dt_end_1 = dt_start + _td(days=1)

            def _insert_rfa(res_id: int, job_id_r: int, days: float, grp_id: int,
                            default_day_pay: float = 0.0, default_fixed_cost: float = 0.0,
                            custom_day_pay: float | None = None):
                day_pay    = custom_day_pay if custom_day_pay is not None else default_day_pay
                fixed_cost = round(custom_day_pay * 0.9, 4) if custom_day_pay is not None else default_fixed_cost
                total_price = round(days * day_pay, 4)
                total_costs = round(days * fixed_cost, 4)
                grp_val     = grp_id if grp_id else None
                cur.execute(
                    "INSERT INTO ResourceFunctionAllocation "
                    "(IdResourceFunction, IdResourceRate, IdJob, "
                    " DateStart, DateEnd, DaysInAction, DayPayment, "
                    " HoursInAction, HourPayment, DistanceInAction, DistancePayment, "
                    " FixedCostDayPayment, FixedCostHourPayment, FixedCostDistancePayment, "
                    " TotalPrice, TotalCosts, IdTable, IdObject, "
                    " IdUserCreated, IdUserUpdated, CreationTime, UpdateTime, "
                    " Quantity, QuantityInvoice, Printable, ScheduledByEvent, IdStockType2JobGroup) "
                    "VALUES (?, 1, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, 0, 0, ?, ?, 4, ?, ?, ?, ?, ?, 1, 1, 1, 0, ?)",
                    res_id, job_id_r,
                    dt_start, dt_end_1, days, day_pay,
                    fixed_cost, total_price, total_costs, job_id_r,
                    uid, uid, now, now,
                    grp_val,
                )

            def _res_job_grp(item_obj):
                hg_name_r = oz_to_hg.get(item_obj.oz or "")
                lid_r     = ss.d83_group_jobs.get(hg_name_r, 1) if hg_name_r else 1
                job_id_r  = lid_map.get(lid_r, first_job_id)
                g_cap_r   = (f'[{item_obj.oz}] {item_obj.description}'
                             if import_mode == "positions" and item_obj.oz
                             else oz_to_sub_cap.get(item_obj.oz or ""))
                grp_id_r  = cap_to_gid.get((job_id_r, g_cap_r), 0) if g_cap_r else 0
                return job_id_r, grp_id_r

            for item_id, mr in ss.matches.items():
                if item_id not in active_ids:
                    continue
                if not mr or not isinstance(mr.matched, _Resource):
                    continue
                res      = mr.matched
                bq       = ss.d83_booking_qtys.get(item_id, {})
                days     = float(max(1, bq.get("qty", 1)))
                item_obj = next((it for it in ss.d83_project.items if it.item_id == item_id), None)
                if not item_obj:
                    continue
                job_id_r, grp_id_r = _res_job_grp(item_obj)
                custom_dp = bq.get("day_pay") if bq else None
                try:
                    _insert_rfa(res.id, job_id_r, days, grp_id_r,
                                res.tagessatz, res.eigenkosten, custom_dp)
                    res_count += 1
                    res_bookings.append({
                        "item_id": item_id, "oz": item_obj.oz or "",
                        "description": res.funktion, "ej_group_id": grp_id_r,
                        "resource_id": res.id, "days": days,
                        "day_pay": custom_dp if custom_dp is not None else res.tagessatz,
                    })
                    log.append({"ok": True, "text": f'[{item_obj.oz}] Ressource: {res.funktion} × {days:.1f}d', "indent": True})
                except Exception as _re:
                    log.append({"ok": False, "text": f'[{item_obj.oz}] Ressource {res.funktion} fehlgeschlagen: {_re}', "indent": True})

            for item_id, bundle in ss.bundles.items():
                if item_id not in active_ids:
                    continue
                item_obj = next((it for it in ss.d83_project.items if it.item_id == item_id), None)
                if not item_obj:
                    continue
                job_id_r, grp_id_r = _res_job_grp(item_obj)
                for b in bundle:
                    if not isinstance(b.get("resource"), _Resource):
                        continue
                    res  = b["resource"]
                    days = float(max(1, b.get("qty", 1)))
                    try:
                        _insert_rfa(res.id, job_id_r, days, grp_id_r,
                                    res.tagessatz, res.eigenkosten)
                        res_count += 1
                        res_bookings.append({
                            "item_id": item_id, "oz": item_obj.oz or "",
                            "description": res.funktion, "ej_group_id": grp_id_r,
                            "resource_id": res.id, "days": days,
                            "day_pay": res.tagessatz,
                        })
                        log.append({"ok": True, "text": f'[{item_obj.oz}] Bundle-Ressource: {res.funktion} × {days:.1f}d', "indent": True})
                    except Exception as _re:
                        log.append({"ok": False, "text": f'[{item_obj.oz}] Bundle-Ressource {res.funktion} fehlgeschlagen: {_re}', "indent": True})

            if res_count:
                cn.commit()
                log.append({"ok": True, "text": f'{res_count} Ressourcen eingebucht ✓', "indent": False})

        except Exception as e:
            traceback.print_exc()
            log.append({"ok": False, "text": f"Fehler (Buchung): {e}", "indent": False})

        # ── 5. Lokal speichern ───────────────────────────────────────────────
        cp.step = "Lokal speichern"
        try:
            job_ids_csv = ",".join(str(j) for j in sorted(all_job_ids))
            # EJ-Projektnummer (z.B. „26-0994") einmalig per API holen und lokal
            # ablegen — so braucht die Projekte-Seite später keine Abfrage.
            ej_project_number = ""
            try:
                ej_project_number = await loop.run_in_executor(
                    None, lambda: ss.ej_client.get_project_number(id_project)
                )
            except Exception:
                pass
            project_db_id = await loop.run_in_executor(
                None,
                lambda: _db.save_project(
                    name=proj_name,
                    ej_project_id=id_project,
                    gaeb_name=ss.d83_name,
                    item_count=len(ss.d83_project.items),
                    booking_count=len(bookings),
                    gaeb_bytes=ss.x83_bytes,
                    ej_job_ids=job_ids_csv,
                    ej_project_number=ej_project_number,
                ),
            )
            from matcher import resolve_time_factor as _resolve_tf
            _curves = _db.load_time_factor_curves_db()
            for bk in bookings:
                mr_bk = ss.matches.get(bk["item_id"])
                art_bk = getattr(mr_bk, "article", None) if mr_bk else None
                ep_bk = float(getattr(art_bk, "mietpreis", 0) or 0)
                if art_bk:
                    ep_bk *= _resolve_tf(_curves, getattr(art_bk, "id_time_factor", 0), einsatztage)
                _db.add_project_booking(
                    project_id=project_db_id,
                    item_id=bk["item_id"],
                    oz=bk["oz"],
                    description=bk["description"],
                    art_num=bk["art_num"],
                    ej_stock_type_id=bk["ej_stock_type_id"],
                    ej_s2j_id=bk["ej_s2j_id"],
                    ej_group_id=bk["ej_group_id"],
                    qty=bk["qty"],
                    unit_price=ep_bk,
                )
            # Ressourcen ebenfalls in den Snapshot (vollständiges Abbild): so sind
            # reine Ressourcen-Positionen auch im Fallback-Modus sichtbar.
            for rb in res_bookings:
                _db.add_project_booking(
                    project_id=project_db_id,
                    item_id=rb["item_id"],
                    oz=rb["oz"],
                    description=rb["description"],
                    art_num="",
                    ej_stock_type_id=rb["resource_id"],
                    ej_s2j_id=0,
                    ej_group_id=rb["ej_group_id"],
                    qty=rb["days"],
                    unit_price=rb["day_pay"],
                    kind="resource",
                )
            log.append({"ok": True, "text": f'Lokal gespeichert (DB-ID: {project_db_id}) ✓', "indent": False})
        except Exception as e:
            log.append({"ok": False, "text": f"Lokale Speicherung fehlgeschlagen: {e}", "indent": False})

    except Exception as e:
        traceback.print_exc()
        log.append({"ok": False, "text": f"Unerwarteter Fehler: {e}", "indent": False})
    finally:
        try:
            if cn:
                cn.close()
        except Exception:
            pass
        cp.log     = log
        cp.running = False


@router.post("/api/import/create-project", response_class=HTMLResponse)
async def import_create_project(
    request:           Request,
    proj_name:         str = Form(""),
    ref_number:        str = Form(""),
    start_date:        str = Form(...),
    end_date:          str = Form(...),
    id_address:        int = Form(1),
    id_delivery:       int = Form(0),
    id_contact:          int = Form(0),
    job_caption:       str = Form("Job 1"),
    id_project_type:   int = Form(9),
    id_event_calendar: int = Form(0),
    import_mode:       str = Form("positions"),
    einsatztage:       str = Form("2"),
    target_mode:          str = Form("new"),
    existing_project_id:  int = Form(0),
    existing_project_name: str = Form(""),
    only_groups:          str = Form(""),
):
    ss = get_session(request.session)
    ss.einsatztage = _parse_einsatztage(einsatztage, ss.einsatztage)
    only_groups_flag = (only_groups == "1")

    is_existing = target_mode == "existing" and existing_project_id > 0

    if not ss.d83_groups:
        return HTMLResponse('<div class="error-msg">Bitte zuerst eine X83-Datei laden.</div>')
    if not ss.ej_client:
        return HTMLResponse('<div class="error-msg">EJ-Verbindung nicht konfiguriert.</div>')
    if not ss.d83_project:
        return HTMLResponse('<div class="error-msg">Kein Projekt geladen.</div>')
    if not ss.ej_db_conn:
        return HTMLResponse('<div class="error-msg">DB-Verbindungsstring fehlt (Einstellungen).</div>')
    if target_mode == "existing" and not existing_project_id:
        return HTMLResponse('<div class="error-msg">Bitte ein bestehendes Projekt auswählen.</div>')
    if not is_existing and not proj_name.strip():
        return HTMLResponse('<div class="error-msg">Bitte einen Projektnamen eingeben.</div>')

    # Im Bestehend-Modus den echten Projektnamen (aus der Suche) für Job-Benennung
    # und lokale Speicherung verwenden.
    effective_name = (existing_project_name.strip() or f"Projekt {existing_project_id}") if is_existing else proj_name

    ss.d83_import_mode = import_mode

    # Zahlungsbedingung vorab (nur für neues Projekt relevant)
    loop = asyncio.get_event_loop()
    id_payment_condition = 2
    if id_address and not is_existing:
        try:
            val = await loop.run_in_executor(
                None, lambda: ss.ej_client.get_address_payment_condition(id_address)
            )
            if val:
                id_payment_condition = val
        except Exception:
            pass

    # Progress initialisieren
    cp             = ss.create_progress
    cp.running     = True
    cp.step        = "Initialisierung"
    cp.done        = 0
    cp.total       = 0
    cp.started_at  = loop.time()
    cp.log         = None

    asyncio.create_task(_do_create_bg(
        ss, effective_name, ref_number, start_date, end_date,
        id_address, id_delivery, job_caption,
        id_project_type, id_event_calendar, import_mode,
        id_payment_condition, ss.einsatztage,
        existing_project_id if is_existing else 0,
        only_groups_flag,
        id_contact,
    ))

    return HTMLResponse("""
        <div id="create-progress"
             hx-get="/api/import/create-progress"
             hx-trigger="every 700ms"
             hx-swap="outerHTML"
             style="padding:18px 0;text-align:center;color:#555;font-size:.9rem">
          ⏳ Wird gestartet&hellip;
        </div>
    """)


@router.get("/api/import/create-progress", response_class=HTMLResponse)
async def import_create_progress(request: Request):
    ss = get_session(request.session)
    cp = ss.create_progress

    # Fertig → Ergebnis-Log anzeigen
    if cp.log is not None:
        return templates.TemplateResponse(request, "partials/d83_result.html", {"log": cp.log})

    if not cp.running:
        return HTMLResponse('<div id="create-progress"><p style="color:#888;font-size:.85rem">Nicht gestartet.</p></div>')

    # Noch aktiv → Progress-HTML
    elapsed = asyncio.get_event_loop().time() - cp.started_at

    bar_html = ""
    sub_html = ""
    if cp.total > 0:
        pct = min(100, int(cp.done / cp.total * 100))
        eta_html = ""
        if cp.done > 0 and cp.done < cp.total:
            eta_s    = elapsed / cp.done * (cp.total - cp.done)
            eta_html = f" &middot; ~{eta_s:.0f}s verbleibend"
        bar_html = f"""
          <div style="background:#ddd;border-radius:4px;height:10px;overflow:hidden;margin:10px 0 6px">
            <div style="background:#1a237e;height:100%;width:{pct}%;transition:width .5s"></div>
          </div>"""
        sub_html = f'<div style="font-size:.8rem;color:#666">{cp.done}&thinsp;/&thinsp;{cp.total}{eta_html}</div>'

    return HTMLResponse(f"""
        <div id="create-progress"
             hx-get="/api/import/create-progress"
             hx-trigger="every 700ms"
             hx-swap="outerHTML"
             style="padding:16px 0">
          <div style="font-weight:600;color:#333;margin-bottom:4px">⏳ {cp.step}&hellip;</div>
          {bar_html}
          {sub_html}
          <div style="font-size:.75rem;color:#aaa;margin-top:6px">{elapsed:.1f}s vergangen</div>
        </div>
    """)
