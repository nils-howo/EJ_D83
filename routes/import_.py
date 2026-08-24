"""Import-Workflow: /import und /api/import/* — kombinierter GAEB-Match + EJ-Projekt-Anlage."""
import asyncio
import logging
import pathlib
import re
import tempfile
import traceback
from datetime import datetime

import pyodbc
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

import db as _db
import excel_parser as _xl
import json as _json
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
        "is_eventual":      item.is_eventual,
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


def _oz_key(num: str) -> tuple:
    """Sortierschlüssel einer Ordnungszahl — zahlenweise, nicht als Text.

    Als Text sortiert kommt "4.202" vor "4.13" und "1.10" vor "1.2": bei Excel-LVs,
    deren Positionsnummern nicht auf feste Breite aufgefüllt sind, stand die
    Gruppenliste damit in willkürlicher Reihenfolge.

    Ein kürzerer Präfix bleibt vor seinen Untergruppen ("01.02" vor "01.02.01"), weil
    Tupelvergleich beim kürzeren endet — das hält die Hierarchie auch dann richtig,
    wenn die Elterngruppe im Dokument erst nach ihren Kindern auftaucht (kommt in
    GAEB-Dateien vor).
    """
    teile = []
    for t in re.split(r'[.\-]', (num or "").strip()):
        if not t:
            continue
        teile.append((0, int(t), "") if t.isdigit() else (1, 0, t.lower()))
    return tuple(teile)


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

    # Reihenfolge der obersten Ebene (Job bzw. Blatt) in Dokumentreihenfolge. Nötig,
    # weil Excel-Ordnungszahlen pro Blatt wieder bei 1 anfangen: ein OZ-Vergleich über
    # Blätter hinweg würde "02_Personal 1.1" vor "01_Material 2.1" einsortieren.
    root_order: dict[str, int] = {}
    for item in project.items:
        root = item.category_path[0] if item.category_path else ""
        if root not in root_order:
            root_order[root] = len(root_order)

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
                "_root": root_order.get(path[0] if path else "", 0),
                "_seq": len(hg_map),          # Auftreten im Dokument
            }
        hg_map[hg_label]["count"] += 1
        if g_label:
            if g_label not in hg_map[hg_label]["sub"]:
                hg_map[hg_label]["sub"][g_label] = {
                    "name": g_label, "num": g_num, "count": 0, "_positions": [], "remarks": [],
                    "_seq": len(hg_map[hg_label]["sub"]),
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
    # Erst die oberste Ebene in Dokumentreihenfolge, dann die Ordnungszahl zahlenweise.
    # So bleibt die Reihenfolge der Datei erhalten und eine Elterngruppe steht trotzdem
    # vor ihren Untergruppen, auch wenn sie im Dokument später auftaucht.
    # Reihenfolge: oberste Ebene (Blatt/Job) wie im Dokument, dann die Ordnungszahl
    # zahlenweise, und bei gleicher Ordnungszahl wieder das Dokument. Der Name als
    # Tiebreaker hätte alphabetisch sortiert — in LOS2 tragen alle Gruppen eines Blatts
    # dieselbe OZ-Wurzel ("B"), da stand dann Personal vor Rigging.
    for hg in sorted(hg_map.values(),
                     key=lambda x: (x["_root"], _oz_key(x["num"]), x["_seq"])):
        hg.pop("_root", None), hg.pop("_seq", None)
        subs = sorted(hg["sub"].values(), key=lambda x: (_oz_key(x["num"]), x["_seq"]))
        for sub in subs:
            sub.pop("_seq", None)
        for sub in subs:
            sub["blocks"] = _to_blocks(sub.pop("_positions"), alt_active)
        hg["sub"]    = subs
        hg["blocks"] = _to_blocks(hg.pop("_positions"), alt_active)
        result.append(hg)
    return result


def _bqty(raw_qty) -> float:
    """Buchungsmenge — nie 0, da EasyJob eine Menge von 0 nicht zulässt. Offene
    Ausschreibungspositionen (Menge 0, z.B. Spesen/Hotel/Personaltage) werden mit
    1 vorbelegt; die tatsächliche Menge bestimmt der Nutzer im Buchungsfeld selbst."""
    try:
        q = float(raw_qty)
    except (TypeError, ValueError):
        q = 0.0
    return q if q > 0 else 1.0


# EJ-Spalte StockType2JobGroup(.Parent).Caption ist nvarchar(250). Lange GAEB-
# Positionstexte sprengen das → Insert-Fehler. Identisch bei Insert UND Lookup
# kürzen, sonst findet die Buchung die Gruppe über den Caption-Key nicht mehr.
_GROUP_CAPTION_MAX = 250


def _gcap(caption: str) -> str:
    """Gruppen-Caption auf die EJ-Spaltenlänge (250 Zeichen) kürzen."""
    caption = caption or ""
    return caption[:_GROUP_CAPTION_MAX]


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
        # Alternativ-/Eventualpositionen werden gebucht, zählen aber NICHT zur Summe.
        in_total = not (it.is_alt or it.is_eventual)
        if mr and mr.matched and mr.score > 0 and mr.method != "kalkpos":
            matched += 1
            if mr.score >= 85:
                confident += 1
            if mr.article:
                art_count += 1
                if getattr(mr.article, "mietpreis", 0) and in_total:
                    factor = resolve_time_factor(curves, mr.article.id_time_factor, einsatztage)
                    cost_mat += qty * mr.article.mietpreis * factor
            elif isinstance(mr.matched, _Resource):
                res_count += 1
                if mr.matched.tagessatz and in_total:
                    cost_pers += qty * mr.matched.tagessatz
        for b in ss.bundles.get(it.item_id, []):
            bres = b.get("resource")
            bart = b.get("article")
            bqty = float(b.get("qty", 1))
            if bres and isinstance(bres, _Resource):
                res_count += 1
                if bres.tagessatz and in_total:
                    cost_pers += bqty * bres.tagessatz
            elif bart:
                art_count += 1
                if getattr(bart, "mietpreis", 0) and in_total:
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


# ─── Import-Entwürfe: State serialisieren / wiederherstellen ──────────────────

def serialize_import_state(ss) -> dict:
    """Serialisiert den Import-State (matches/bundles als Referenzen + Konfiguration)
    für einen Entwurf. GAEB/Matcher werden NICHT gespeichert (beim Laden neu gebaut).
    Ohne setup-Felder — die ergänzt der Save-Endpoint aus dem Seitenformular."""
    from matcher import Resource as _Resource

    def _ref(obj):
        if obj is None:
            return None
        if isinstance(obj, _Resource):
            # Ressource VOLLSTÄNDIG serialisieren (wie Artikel) — liegt sie beim Laden
            # nicht mehr im Pool, wird sie aus diesen Daten rekonstruiert.
            return {
                "kind": "resource",
                "ref": obj.id,
                "res": {
                    "id":            int(getattr(obj, "id", 0) or 0),
                    "funktion":      getattr(obj, "funktion", "") or "",
                    "ressourcenart": getattr(obj, "ressourcenart", "") or "",
                    "tagessatz":     float(getattr(obj, "tagessatz", 0) or 0),
                    "eigenkosten":   float(getattr(obj, "eigenkosten", 0) or 0),
                    "satzname":      getattr(obj, "satzname", "") or "",
                },
            }
        # Artikel VOLLSTÄNDIG serialisieren (auch EJ-only ohne lokale Nummer) — sonst
        # gehen manuell per EJ-Suche hinzugefügte Artikel beim Speichern verloren.
        return {
            "kind": "article",
            "ref": getattr(obj, "nummer", "") or "",
            "art": {
                "ej_id":             int(getattr(obj, "ej_id", 0) or 0),
                "id_time_factor":    int(getattr(obj, "id_time_factor", 0) or 0),
                "nummer":            getattr(obj, "nummer", "") or "",
                "bezeichnung":       getattr(obj, "bezeichnung", "") or "",
                "warengruppe":       getattr(obj, "warengruppe", "") or "",
                "mutterwarengruppe": getattr(obj, "mutterwarengruppe", "") or "",
                "artikelart":        getattr(obj, "artikelart", "") or "",
                "hersteller":        getattr(obj, "hersteller", "") or "",
                "detail":            getattr(obj, "detail", "") or "",
                "kommentar":         getattr(obj, "kommentar", "") or "",
                "mietpreis":         float(getattr(obj, "mietpreis", 0) or 0),
                "einheit":           getattr(obj, "einheit", "") or "",
                "mietinventar":      int(getattr(obj, "mietinventar", 0) or 0),
            },
        }

    matches: dict = {}
    for iid, mr in (ss.matches or {}).items():
        if not mr or not getattr(mr, "matched", None):
            continue
        r = _ref(mr.matched)
        if r:
            matches[iid] = {**r, "score": mr.score, "method": mr.method,
                            "confident": mr.confident}
    bundles: dict = {}
    for iid, blist in (ss.bundles or {}).items():
        out = []
        for b in blist:
            r = _ref(b.get("article") or b.get("resource"))
            if r:
                out.append({**r, "qty": float(b.get("qty", 1))})
        if out:
            bundles[iid] = out
    return {
        "v": 1,
        "matches": matches,
        "bundles": bundles,
        "booking_qtys": ss.d83_booking_qtys or {},
        "alt_active": ss.d83_alt_active or {},
        "group_jobs": ss.d83_group_jobs or {},
        "local_jobs": ss.d83_local_jobs or [],
        "next_lid": ss.d83_next_lid,
        "standard_job_name": ss.d83_standard_job_name or "",
        "import_mode": ss.d83_import_mode or "positions",
        "einsatztage": ss.einsatztage,
        # Herkunft + Excel-Layout: ohne das Layout ließe sich eine Excel-Quelle beim
        # Laden des Entwurfs nicht erneut in Positionen zerlegen.
        "source_kind": ss.source_kind or "gaeb",
        "excel_layout": ss.excel_layout or {},
    }


def apply_import_state(ss, state: dict) -> list[str]:
    """Stellt matches/bundles/Konfiguration aus einem Entwurf wieder her. Voraussetzung:
    ss.d83_project + ss.matcher sind bereits aufgebaut. Es wird NICHT neu gematcht —
    leere Positionen bleiben leer. Gibt Warnungen zurück (Referenz nicht mehr im Stamm)."""
    from matcher import Resource as _Resource
    warnings: list[str] = []
    m = ss.matcher

    from matcher import Article as _Article

    def _resolve(entry: dict):
        kind, ref = entry.get("kind"), entry.get("ref")
        if kind == "resource":
            if m and ref is not None:
                idx = m._id_to_resource_idx.get(int(ref))
                if idx is not None:
                    return m._pool[idx]
            # nicht (mehr) im Pool → aus gespeicherten Daten rekonstruieren
            res = entry.get("res")
            if res:
                return _Resource(
                    id=int(res.get("id") or 0),
                    funktion=res.get("funktion") or "",
                    ressourcenart=res.get("ressourcenart") or "",
                    tagessatz=float(res.get("tagessatz") or 0),
                    eigenkosten=float(res.get("eigenkosten") or 0),
                    satzname=res.get("satzname") or "",
                    gaeb_synonyms=[],
                )
            return None
        # Artikel: erst lokaler Pool (volle Stammdaten), sonst aus gespeicherten Daten
        # rekonstruieren (EJ-only-Artikel, die nicht im Stamm liegen).
        if m and ref:
            idx = m._num_to_idx.get(str(ref))
            if idx is None and "." not in str(ref):
                idx = m._num_to_idx.get(f"{ref}.00")
            if idx is not None:
                return m._pool[idx]
        art = entry.get("art")
        if art:
            return _Article(
                ej_id=int(art.get("ej_id") or 0),
                id_time_factor=int(art.get("id_time_factor") or 0),
                nummer=art.get("nummer") or "",
                bezeichnung=art.get("bezeichnung") or "",
                warengruppe=art.get("warengruppe") or "",
                mutterwarengruppe=art.get("mutterwarengruppe") or "",
                artikelart=art.get("artikelart") or "",
                hersteller=art.get("hersteller") or "",
                detail=art.get("detail") or "",
                kommentar=art.get("kommentar") or "",
                mietpreis=float(art.get("mietpreis") or 0),
                einheit=art.get("einheit") or "",
                mietinventar=int(art.get("mietinventar") or 0),
                gaeb_synonyms=[],
            )
        return None

    ss.matches = {}
    for iid, mm in (state.get("matches") or {}).items():
        obj = _resolve(mm)
        if obj is None:
            warnings.append(f"Zuordnung {mm.get('ref')} (Pos. {iid}) nicht mehr im Stamm")
            continue
        ss.matches[iid] = MatchResult(matched=obj, score=mm.get("score", 99.0),
                                      method=mm.get("method", "draft"),
                                      confident=mm.get("confident", True))
    ss.bundles = {}
    for iid, blist in (state.get("bundles") or {}).items():
        out = []
        for b in blist:
            obj = _resolve(b)
            if obj is None:
                warnings.append(f"Bundle {b.get('ref')} (Pos. {iid}) nicht mehr im Stamm")
                continue
            key = "resource" if isinstance(obj, _Resource) else "article"
            out.append({key: obj, "qty": float(b.get("qty", 1))})
        if out:
            ss.bundles[iid] = out
    ss.d83_booking_qtys      = state.get("booking_qtys") or {}
    # Buchungsmenge jeder zugeordneten Position auf mind. 1 anheben — offene Positionen
    # (QtyTBD, „Menge selbst bestimmen") aus älteren Entwürfen hatten evtl. 0 bzw. gar
    # keine Menge gespeichert; EJ verbietet Menge 0, Vorgabe ist „Standard 1".
    for _iid in ss.matches:
        _bq = ss.d83_booking_qtys.get(_iid)
        if _bq is None:
            ss.d83_booking_qtys[_iid] = {"qty": 1.0, "lfm_converted": False, "piece_len": None}
        else:
            _bq["qty"] = _bqty(_bq.get("qty"))
    ss.d83_alt_active        = state.get("alt_active") or {}
    ss.d83_group_jobs        = state.get("group_jobs") or {}
    ss.d83_local_jobs        = state.get("local_jobs") or []
    ss.d83_next_lid          = int(state.get("next_lid") or 2)
    ss.d83_standard_job_name = state.get("standard_job_name") or ""
    ss.d83_import_mode       = state.get("import_mode") or "positions"
    ss.einsatztage           = float(state.get("einsatztage") or 2.0)
    ss.source_kind           = state.get("source_kind") or "gaeb"
    ss.excel_layout          = state.get("excel_layout") or {}
    return warnings


# ─── Import-Entwürfe: Speichern / Laden / Freigeben / Löschen ─────────────────

@router.post("/api/import/draft/save", response_class=HTMLResponse)
async def import_draft_save(
    request:               Request,
    draft_name:            str = Form(""),
    target_mode:           str = Form("new"),
    proj_name:             str = Form(""),
    ref_number:            str = Form(""),
    start_date:            str = Form(""),
    end_date:              str = Form(""),
    id_address:            int = Form(0),
    address_name:          str = Form(""),
    id_contact:            int = Form(0),
    contact_name:          str = Form(""),
    id_delivery:           int = Form(0),
    delivery_name:         str = Form(""),
    id_project_type:       int = Form(0),
    id_event_calendar:     int = Form(0),
    existing_project_id:   int = Form(0),
    existing_project_name: str = Form(""),
):
    import json as _json
    ss = get_session(request.session)
    if not ss.d83_project:
        return HTMLResponse('<span class="error-msg">Kein Import geladen — nichts zu speichern.</span>')
    state = serialize_import_state(ss)
    state["setup"] = {
        "target_mode": target_mode, "proj_name": proj_name, "ref_number": ref_number,
        "start_date": start_date, "end_date": end_date,
        "id_address": id_address, "address_name": address_name,
        "id_contact": id_contact, "contact_name": contact_name,
        "id_delivery": id_delivery, "delivery_name": delivery_name,
        "id_project_type": id_project_type, "id_event_calendar": id_event_calendar,
        "existing_project_id": existing_project_id, "existing_project_name": existing_project_name,
    }
    ss.d83_draft_setup = state["setup"]
    name = (draft_name.strip() or proj_name.strip() or existing_project_name.strip()
            or ss.import_filename or "Entwurf")
    gb = ss.x83_bytes if not ss.draft_id else None   # GAEB nur beim ersten Mal ablegen
    saved_id = _db.save_draft(
        name=name, gaeb_name=ss.import_filename or ss.d83_name,
        gaeb_bytes=gb, state_json=_json.dumps(state, ensure_ascii=False),
        user_name=ss.ej_user or "?",
        item_count=len(ss.d83_project.items), draft_id=ss.draft_id,
        source_kind=ss.source_kind or "gaeb",
    )
    if not saved_id:
        # Entwurf existiert nicht mehr als Entwurf (z.B. zwischenzeitlich hochgeladen)
        return HTMLResponse('<span class="error-msg">Entwurf nicht mehr vorhanden (evtl. bereits hochgeladen).</span>')
    ss.draft_id = saved_id
    return HTMLResponse(f'<span class="save-ok">💾 Entwurf „{name}" gespeichert ✓</span>')


@router.post("/api/import/draft/{draft_id}/load")
async def import_draft_load(draft_id: int, request: Request):
    import json as _json
    ss = get_session(request.session)
    d = _db.get_draft(draft_id)
    if not d or d.get("status") != "draft":
        return HTMLResponse('<span class="error-msg">Entwurf nicht gefunden.</span>', status_code=404)
    _usr = ss.ej_user or ""
    ok, _ = _db.acquire_draft_lock(draft_id, _usr)
    if not ok:
        return HTMLResponse('<span class="error-msg">🔒 Dieser Entwurf wird gerade von jemand anderem bearbeitet.</span>')
    gb = d.get("gaeb_bytes")
    if not gb:
        _db.release_draft_lock(draft_id, _usr)
        return HTMLResponse('<span class="error-msg">Entwurf ohne Quelldatei.</span>', status_code=400)

    loop = asyncio.get_event_loop()

    # Herkunft steht in der Projektzeile; das Excel-Layout im Entwurf-State.
    _state_pre  = _json.loads(d.get("state_json") or "{}")
    _kind       = (d.get("source_kind") or _state_pre.get("source_kind") or "gaeb")
    _xl_layout  = _state_pre.get("excel_layout") or {}

    def _setup():
        if _kind == "excel":
            project = _xl.parse_excel(gb, _xl.layout_from_dict(_xl_layout),
                                      name=d.get("gaeb_name") or "Excel-LV")
        else:
            project = parse_gaeb(gb)   # akzeptiert Bytes direkt — keine Temp-Datei nötig
        matcher = UnifiedMatcher(load_articles_db(), load_resources_db())
        return project, matcher

    try:
        project, matcher = await loop.run_in_executor(None, _setup)
        ss.d83_project     = project
        ss.d83_name        = d.get("gaeb_name") or "X83"
        ss.import_filename = d.get("gaeb_name") or "X83"
        ss.x83_bytes       = gb
        ss.source_kind     = _kind
        if _kind == "excel":
            ss.excel_bytes = gb
            ss.excel_name  = d.get("gaeb_name") or ""
        ss.matcher         = matcher
        ss.matcher.apply_mapping_filter(ss.use_train_mappings, ss.use_gui_mappings)

        state = _json.loads(d.get("state_json") or "{}")
        apply_import_state(ss, state)     # KEIN Auto-Match — Stand 1:1 wiederherstellen
        ss.d83_draft_setup = state.get("setup") or {}
        level = 1 if ss.d83_import_mode == "groups" else 0
        ss.d83_groups = _import_gaeb_groups(project, level, ss.d83_alt_active)
        ss.draft_id = draft_id
    except Exception as e:
        # Sperre nicht hängen lassen, wenn Parsen/Wiederherstellen scheitert.
        _db.release_draft_lock(draft_id, _usr)
        logging.error("Entwurf laden fehlgeschlagen (id=%s): %s", draft_id, e)
        return HTMLResponse('<span class="error-msg">Entwurf konnte nicht geladen werden.</span>',
                            status_code=500)

    resp = HTMLResponse("")
    resp.headers["HX-Redirect"] = "/import"
    return resp


@router.post("/api/import/draft/{draft_id}/release")
async def import_draft_release(draft_id: int, request: Request):
    ss = get_session(request.session)
    _db.release_draft_lock(draft_id, ss.ej_user or "", force=ss.is_admin)
    if ss.draft_id == draft_id:
        ss.draft_id = None
    resp = HTMLResponse("")
    resp.headers["HX-Redirect"] = "/projects"
    return resp


@router.post("/api/import/draft/{draft_id}/heartbeat")
async def import_draft_heartbeat(draft_id: int, request: Request):
    """Hält die Bearbeitungs-Sperre am Leben, solange die Import-Seite offen ist."""
    ss = get_session(request.session)
    _db.heartbeat_draft_lock(draft_id, ss.ej_user or "")
    return HTMLResponse("")


@router.post("/api/import/draft/{draft_id}/delete")
async def import_draft_delete(draft_id: int, request: Request):
    ss = get_session(request.session)
    if not ss.is_admin:
        return HTMLResponse('<span class="error-msg">Nur für Admins.</span>')
    d = _db.get_draft(draft_id)
    if not d or d.get("status") != "draft":
        return HTMLResponse('<span class="error-msg">Kein Entwurf.</span>', status_code=400)
    _db.delete_project(draft_id)
    if ss.draft_id == draft_id:
        ss.draft_id = None
    resp = HTMLResponse("")
    resp.headers["HX-Redirect"] = "/projects"
    return resp


# ─── Auto-Matching ───────────────────────────────────────────────────────────

def run_auto_match(ss, project: GaebProject) -> tuple[dict, dict, dict]:
    """Matcht alle Positionen eines Projekts gegen den Artikelstamm.

    Läuft blockierend (Aufrufer schiebt es in einen Executor) und schreibt den
    Fortschritt nach ss.progress. Gibt (matches, bundles, booking_qtys) zurück.
    Von GAEB- und Excel-Upload gemeinsam genutzt.
    """
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
                # match_path trägt beim Excel-Import die volle Gruppenkette; bei GAEB
                # ist es leer und category_path IST schon die volle Kette.
                category_path=item.match_path or item.category_path,
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
                    booking_qtys[item.item_id] = {"qty": _bqty(item.qty), "lfm_converted": False, "piece_len": None}

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


async def rebuild_matcher(ss) -> None:
    """Matcher neu aufbauen, damit die Mapping-Quellen-Toggles (Training-Mappings /
    GUI-Korrekturen) auf das Matching wirken."""
    loop      = asyncio.get_event_loop()
    articles  = await loop.run_in_executor(None, load_articles_db)
    resources = await loop.run_in_executor(None, load_resources_db)
    ss.matcher = UnifiedMatcher(articles, resources)
    ss.matcher.apply_mapping_filter(ss.use_train_mappings, ss.use_gui_mappings)


def reset_import_state(ss) -> None:
    """Job-/Auswahl-State für einen frischen Import zurücksetzen."""
    ss.d83_local_jobs        = []
    ss.d83_group_jobs        = {}
    ss.d83_next_lid          = 2
    ss.d83_standard_job_name = ""
    ss.d83_alt_active        = {}
    ss.d83_booking_qtys      = {}


def start_match_bg(ss, project: GaebProject, notice: str = "") -> HTMLResponse:
    """Matching im Hintergrund starten und die Fortschritts-Antwort liefern.
    Identisch für GAEB und Excel.

    notice: HTML, das über dem Fortschritt stehen bleibt. Beim Polling tauscht htmx
    nur #imp-progress (outerHTML) — die Meldung überlebt das und ist noch da, wenn
    die Gruppenansicht erscheint.
    """
    n_items = len(project.items)
    ss.progress.running = True
    ss.progress.done    = 0
    ss.progress.total   = n_items
    loop = asyncio.get_event_loop()

    async def _run_bg():
        try:
            new_matches, new_bundles, new_bqtys = await loop.run_in_executor(
                None, run_auto_match, ss, project)
            ss.matches          = new_matches
            ss.bundles          = new_bundles
            ss.d83_booking_qtys = new_bqtys
            level = 1 if ss.d83_import_mode == "groups" else 0
            ss.d83_groups = _import_gaeb_groups(project, level, ss.d83_alt_active)
            logging.info("import: %d items, %d matches", n_items, len(ss.matches))
        except Exception:
            traceback.print_exc()
        finally:
            ss.progress.running = False

    asyncio.ensure_future(_run_bg())
    return HTMLResponse(
        notice +
        '<div id="imp-progress"'
        ' hx-get="/api/import/match-progress"'
        ' hx-trigger="every 600ms"'
        ' hx-swap="outerHTML">'
        f'<p style="font-size:.85rem;color:#555;margin-bottom:6px">Analysiere {n_items} Positionen…</p>'
        '<div class="imp-spinner" style="margin:0 auto"></div>'
        '</div>'
    )


# ─── Upload + Auto-Matching ───────────────────────────────────────────────────

@router.post("/api/import/upload", response_class=HTMLResponse)
async def import_upload(request: Request, file: UploadFile = File(...)):
    ss = get_session(request.session)
    # Frischer Upload → evtl. offenen Entwurf freigeben (neuer Import ≠ Entwurf).
    if getattr(ss, "draft_id", None):
        try:
            _db.release_draft_lock(ss.draft_id, ss.ej_user or "")
        except Exception:
            pass
        ss.draft_id = None
    try:
        data  = await file.read()
        fname = file.filename or "upload"
        suf   = pathlib.Path(fname).suffix.lower()

        # ── Excel: erst Layout erkennen, Mapping-Dialog zeigen, Matching folgt beim Apply
        if suf in (".xlsx", ".xlsm"):
            return await _excel_probe_response(request, ss, data, fname)
        if suf == ".xls":
            return HTMLResponse(
                '<div class="error-msg">Das alte Excel-Format (.xls) kann nicht gelesen '
                'werden — bitte die Datei in Excel als <b>.xlsx</b> speichern.</div>')

        # ── GAEB (X83/X84/XML)
        with tempfile.NamedTemporaryFile(suffix=suf or ".xml", delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        try:
            project = parse_gaeb(tmp)
        finally:
            pathlib.Path(tmp).unlink(missing_ok=True)

        ss.d83_project     = project
        ss.d83_name        = fname
        ss.import_filename = fname
        ss.x83_bytes       = data
        ss.source_kind     = "gaeb"
        ss.excel_bytes     = None
        ss.excel_name      = ""
        ss.excel_probe     = None
        ss.excel_layout    = {}
        reset_import_state(ss)
        await rebuild_matcher(ss)
        return start_match_bg(ss, project)
    except Exception as e:
        traceback.print_exc()
        return HTMLResponse(f'<div class="error-msg">Fehler beim Einlesen: {e}</div>')


# ─── Excel-Import: Mapping-Dialog ────────────────────────────────────────────

def _excel_ctx(ss, probe, profile: dict | None, show_all: set | None = None) -> dict:
    """Template-Kontext für den Mapping-Dialog."""
    return {
        "S":        ss,
        "show_all": sorted(show_all or ()),
        # Ab wie vielen Zeilen die Vorschau kürzt — das Template soll die Zahl nicht
        # noch einmal kennen.
        "preview_rows": _xl._PREVIEW_ROWS,
        "probe":    probe,
        "fname":    ss.excel_name,
        "profile":  profile,
        "layout":   _xl.layout_to_dict(probe.layout),
        "col_letter": _xl.get_column_letter,
        # Vorlagenname = Dateiname ohne Endung; der aus dem Blatt gelesene Projektname
        # ist oft die Veranstaltung, nicht die Vorlage.
        "default_label": pathlib.Path(ss.excel_name or "").stem,
    }


async def _excel_probe_response(request: Request, ss, data: bytes, fname: str) -> HTMLResponse:
    """Excel einlesen, Layout erkennen (ggf. gespeichertes Profil anwenden) und den
    Mapping-Dialog rendern. Gematcht wird erst beim Apply.

    openpyxl ist blockierend (0,2–1 s je Mappe) und läuft deshalb im Executor — im
    Event-Loop würde es alle anderen Sessions anhalten, auch deren Fortschritts-Polls.
    """
    loop = asyncio.get_event_loop()

    def _probe():
        # Formelzellen einmal je Upload erfassen (eigener read_only-Ladevorgang) —
        # daraus warnt der Dialog, wenn die Preisspalte selbst berechnet wird.
        fmls    = _xl.formula_rows(data)
        pb      = _xl.probe_workbook(data, fmls)
        prof    = _db.get_excel_layout(pb.fingerprint)
        if prof:
            pb = _xl.preview_workbook(data, _xl.merge_profile(pb, prof["layout"]), fmls)
        return fmls, pb, prof

    fmls, probe, profile = await loop.run_in_executor(None, _probe)

    ss.excel_bytes     = data
    ss.excel_name      = fname
    ss.excel_probe     = probe
    ss.excel_formulas  = fmls
    ss.excel_layout    = _xl.layout_to_dict(probe.layout)
    ss.source_kind     = "excel"
    ss.import_filename = ""          # erst nach dem Apply gilt die Datei als geladen
    # Vorherigen Import verwerfen: sonst zeigt ein Seiten-Reload während des Mappings
    # noch die Positionen der alten Datei, obwohl die Datei-Anzeige schon leer ist.
    ss.d83_project = None
    ss.d83_groups  = []
    ss.matches     = {}
    ss.bundles     = {}
    ss.x83_bytes   = None
    reset_import_state(ss)
    logging.info("import/excel: %s — %d Sheets, fp=%s, Profil=%s",
                 fname, len(probe.sheets), probe.fingerprint, bool(profile))
    return templates.TemplateResponse(request, "partials/excel_mapping.html",
                                      _excel_ctx(ss, probe, profile))


@router.post("/api/import/excel/repreview", response_class=HTMLResponse)
async def import_excel_repreview(request: Request, layout_json: str = Form(...),
                                 show_all: str = Form(""), opened: str = Form("")):
    """Kopfzeile/Spaltenrollen geändert → Klassifikation und Vorschau neu berechnen."""
    ss = get_session(request.session)
    if not ss.excel_bytes:
        return HTMLResponse('<div class="error-msg">Keine Excel-Datei geladen — '
                            'bitte erneut hochladen.</div>')
    try:
        layout = _xl.layout_from_dict(_json.loads(layout_json))
        # „alle Zeilen" ist reine Anzeigesache und gehört nicht ins Layout-Profil —
        # deshalb ein eigenes Feld statt eines Eintrags im Layout.
        wide   = {n for n in show_all.split("|") if n}
        # Nur die aufgeklappten Blätter brauchen Zellen — der Browser weiß, welche
        # das sind, und schickt sie mit. Beim ersten Aufbau (Upload) ist es leer,
        # dann baut der Server alle auf.
        offen  = {n for n in opened.split("|") if n} or None
        loop   = asyncio.get_event_loop()
        probe  = await loop.run_in_executor(
            None, _xl.preview_workbook, ss.excel_bytes, layout,
            getattr(ss, "excel_formulas", None), wide, offen)
        ss.excel_probe  = probe
        ss.excel_layout = _xl.layout_to_dict(probe.layout)
        return templates.TemplateResponse(request, "partials/excel_mapping.html",
                                          _excel_ctx(ss, probe, None, wide))
    except Exception as e:
        traceback.print_exc()
        return HTMLResponse(f'<div class="error-msg">Vorschau fehlgeschlagen: {e}</div>')


def _assign_jobs(ss, project: GaebProject) -> None:
    """Easyjob-Jobs aus der Excel-Struktur vorbelegen.

    Welche Jobs es gibt, entscheidet der Parser (Szenario · Blatt · als „Job" gemalte
    Zeilen) und legt es in ``project.job_by_item`` ab. Hier wird daraus der
    Session-State: der erste Job ist der Standard-Job, alle weiteren kommen als
    lokale Jobs dazu. ``d83_group_jobs`` schlüsselt auf das Gruppenlabel — genau wie
    die manuelle Zuordnung in /api/import/local-add-job. Weil alle Gruppenlabels ihre
    Herkunftskoordinate tragen, gehört jedes Label zu genau einem Job.
    """
    jobs: list[str] = []
    for item in project.items:
        name = (project.job_by_item.get(item.item_id) or "").strip()
        if name and name not in jobs:
            jobs.append(name)
    if len(jobs) < 2:
        # Ein Job (oder keiner) → alles in den Standard-Job, nichts zu verteilen.
        if jobs:
            ss.d83_standard_job_name = jobs[0]
        return

    ss.d83_standard_job_name = jobs[0]
    lid_by_job = {jobs[0]: 1}
    for name in jobs[1:]:
        lid = ss.d83_next_lid
        ss.d83_local_jobs.append({"lid": lid, "name": name})
        ss.d83_next_lid += 1
        lid_by_job[name] = lid

    for item in project.items:
        lid = lid_by_job.get((project.job_by_item.get(item.item_id) or "").strip(), 1)
        if lid == 1:
            continue
        for label in item.category_path:
            ss.d83_group_jobs[label] = lid
    logging.info("import/excel: %d Jobs -> %s", len(jobs), lid_by_job)


@router.post("/api/import/excel/apply", response_class=HTMLResponse)
async def import_excel_apply(request: Request,
                             layout_json: str = Form(...),
                             label: str = Form("")):
    """Bestätigtes Mapping anwenden: Positionen bauen, Layout-Profil merken,
    Auto-Matching starten."""
    ss = get_session(request.session)
    if not ss.excel_bytes:
        return HTMLResponse('<div class="error-msg">Keine Excel-Datei geladen — '
                            'bitte erneut hochladen.</div>')
    try:
        layout  = _xl.layout_from_dict(_json.loads(layout_json))
        loop    = asyncio.get_event_loop()
        project = await loop.run_in_executor(
            None, _xl.parse_excel, ss.excel_bytes, layout,
            label or pathlib.Path(ss.excel_name).stem)
        if not project.items:
            return HTMLResponse(
                '<div class="error-msg">Keine Positionen erkannt. Bitte prüfen, ob '
                'Kopfzeile, Beschreibung und Menge/Einheit richtig eingefärbt sind.</div>')

        ss.d83_project     = project
        ss.d83_name        = ss.excel_name
        ss.import_filename = ss.excel_name
        ss.x83_bytes       = ss.excel_bytes      # Quelldatei für die Preis-Rückschreibung
        ss.excel_layout    = _xl.layout_to_dict(layout)
        ss.source_kind     = "excel"
        ss.excel_probe     = None                # Vorschau nicht länger im RAM halten
        ss.excel_formulas  = None

        reset_import_state(ss)
        _assign_jobs(ss, project)
        _xl.release(ss.excel_bytes)   # Mappen-Cache freigeben, Matching braucht den RAM

        # Layout-Profil merken → beim nächsten Upload derselben Vorlage schon richtig
        try:
            _db.save_excel_layout(layout.fingerprint or "",
                                  label or pathlib.Path(ss.excel_name).stem,
                                  ss.excel_layout)
        except Exception:
            traceback.print_exc()      # Profil ist Komfort, kein Grund den Import zu stoppen

        # Angehaktes Blatt, aus dem keine einzige Position kam: benennen statt
        # stillschweigend weniger zu importieren.
        got    = {i.src_ref.rsplit("!", 1)[0] for i in project.items if i.src_ref}
        empty  = [sl.name for sl in layout.sheets if sl.enabled and sl.name not in got]
        notice = ""
        if empty:
            names = ", ".join(empty)
            notice = (
                '<div class="error-msg" style="margin-bottom:10px">'
                f'Aus {len(empty)} angehakten Blättern kam keine Position: {names}. '
                'Dort fehlt die Zuordnung von <b>Beschreibung</b> und '
                '<b>Menge</b> bzw. <b>Einheit</b> — evtl. auch die Kopfzeile. '
                'Alles andere wurde eingelesen.</div>')
            logging.warning("import/excel: Blätter ohne Positionen: %s", empty)

        await rebuild_matcher(ss)
        return start_match_bg(ss, project, notice)
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

def _promote_bundle_to_primary(ss, item_id: str) -> None:
    """Ohne Haupt-Match, aber mit Zusatz-Artikeln/-Ressourcen: der erste Eintrag
    rückt als neues Haupt-Match auf. Hält die Invariante „Position mit Artikeln
    hat immer ein Haupt-Match" — sonst zeigt die Position „Nicht zugeordnet",
    obwohl noch Artikel gebucht sind."""
    cur = ss.matches.get(item_id)
    if cur and cur.matched and cur.score > 0:
        return
    bundle = ss.bundles.get(item_id, [])
    if not bundle:
        return
    first = bundle.pop(0)
    obj = first.get("article") or first.get("resource")
    if obj is None:
        return
    ss.matches[item_id] = MatchResult(matched=obj, score=99.0, method="manual", confident=True)
    ss.d83_booking_qtys[item_id] = {
        "qty": float(first.get("qty", 1) or 1), "lfm_converted": False, "piece_len": None,
    }
    if not bundle:
        ss.bundles.pop(item_id, None)


@router.get("/api/import/clear-match/{item_id}", response_class=HTMLResponse)
async def import_clear_match(request: Request, item_id: str):
    ss = get_session(request.session)
    ss.matches.pop(item_id, None)
    # Falls noch Zusatz-Artikel/-Ressourcen an der Position hängen, rückt der erste
    # als neues Haupt-Match auf — sonst zeigte die Position „Nicht zugeordnet".
    _promote_bundle_to_primary(ss, item_id)
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
    extra_nums: str = Form(default=""),
    extra_qtys: str = Form(default=""),
):
    import json as _json
    ss = get_session(request.session)
    if not ej_num or not raw_json:
        return '<p class="error-msg">Bitte zuerst einen Artikel auswählen.</p>'
    try:
        raw = _json.loads(raw_json)
    except ValueError:
        return '<p class="error-msg">Ungültige Artikeldaten.</p>'
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
                ss.d83_booking_qtys[item_id] = {"qty": _bqty(item.qty), "lfm_converted": False, "piece_len": None}
    _add_optional_ref_bundles(ss, item_id, extra_nums, extra_qtys)
    _learn_article_match(ss, item_id)
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
    extra_nums: str = Form(default=""),
    extra_qtys: str = Form(default=""),
):
    import json as _json
    ss = get_session(request.session)
    if not ej_num or not raw_json:
        return '<p class="error-msg">Bitte zuerst einen Artikel auswählen.</p>'
    try:
        raw = _json.loads(raw_json)
    except ValueError:
        return '<p class="error-msg">Ungültige Artikeldaten.</p>'
    art = make_article_from_ej(raw, None, ss.matcher)
    cur = ss.matches.get(item_id)
    if not (cur and cur.matched and cur.score > 0):
        # Noch keine Haupt-Zuordnung → hinzugefügter Artikel wird Haupt-Match,
        # damit die Position als „zugeordnet" gilt (statt als loses Bundle ohne
        # Haupt-Match, das fälschlich „Nicht zugeordnet" anzeigt).
        ss.matches[item_id] = MatchResult(matched=art, score=99.0, method="manual", confident=True)
        ss.d83_booking_qtys[item_id] = {"qty": _bqty(qty), "lfm_converted": False, "piece_len": None}
    else:
        ss.bundles.setdefault(item_id, []).append({"article": art, "qty": qty})
    _add_optional_ref_bundles(ss, item_id, extra_nums, extra_qtys)
    _learn_article_match(ss, item_id)
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
                    item.description, limit=5,
                    qty=float(item.qty), unit=item.unit,
                    long_text=item.long_text,
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
        "default_qty":       _bqty(item.qty),
        "results":           [],
        "suggestions":       suggestions,
        "suggestions_error": suggestions_error,
        "form_action":       f"/api/import/set-match/{item_id}",
        "form_target":       "#import-groups",
        "add_action":        f"/api/import/add-match/{item_id}",
        "search_url":        f"/api/import/ej/search/{item_id}",
    })


def _add_optional_ref_bundles(ss, item_id: str, extra_nums: str, extra_qtys: str) -> None:
    """Fügt vom Nutzer in der Referenzkarte gewählte OPTIONALE Referenzartikel
    (IsOptional=1, z.B. ETC-Tubus) als Bundle-Artikel zur Position hinzu — sie werden
    beim Anlegen mitgebucht (nicht-optionale Referenzen kommen ohnehin automatisch)."""
    if not extra_nums or not ss.matcher:
        return
    nums = [n.strip() for n in extra_nums.split(",") if n.strip()]
    qtys = [q.strip() for q in extra_qtys.split(",") if q.strip()]
    bundle = ss.bundles.setdefault(item_id, [])
    for i, num in enumerate(nums):
        idx = ss.matcher._num_to_idx.get(num)
        if idx is None and "." not in num:          # EJ liefert evtl. "123", Stamm hat "123.00"
            idx = ss.matcher._num_to_idx.get(f"{num}.00")
        if idx is None:
            continue
        try:
            q = float(qtys[i]) if i < len(qtys) else 1.0
        except ValueError:
            q = 1.0
        if q > 0:
            bundle.append({"article": ss.matcher._pool[idx], "qty": q})


def _learn_article_match(ss, item_id: str) -> None:
    """Speichert die aktuelle Artikel-Zuordnung (Primär-Match + Artikel-Bundles) als
    gelerntes GUI-Mapping — so bleibt eine manuelle Zuordnung beim nächsten Import /
    Neu-Matchen erhalten (analog zum Ressourcen-Lernen in import_set_resource)."""
    if not ss.d83_project:
        return
    item = next((it for it in ss.d83_project.items if it.item_id == item_id), None)
    if not item or not item.description.strip():
        return
    nums: list[str] = []
    mr = ss.matches.get(item_id)
    if mr and mr.article and (mr.article.nummer or "").strip():
        nums.append(mr.article.nummer)
    for b in ss.bundles.get(item_id, []):
        art = b.get("article")
        if art and (getattr(art, "nummer", "") or "").strip() and art.nummer not in nums:
            nums.append(art.nummer)
    if not nums:
        return
    from db import save_gui_bundle
    save_gui_bundle(item.description, nums)
    if ss.matcher:
        ss.matcher.add_learned_bundle(item.description, nums)


@router.get("/api/import/references/{ej_id}", response_class=HTMLResponse)
async def import_references(ej_id: int, request: Request, name: str = ""):
    """Referenzkarte für einen gewählten Artikel: optionale Referenzen (IsOptional=1,
    z.B. ETC-Tubus) zum Mitbuchen + automatisch mitkommende Referenzen als Info."""
    ss = get_session(request.session)
    if not ss.ej_client or ej_id <= 0:
        return HTMLResponse("")
    loop = asyncio.get_event_loop()
    try:
        refs = await loop.run_in_executor(None, ss.ej_client.get_references, ej_id)
    except Exception:
        return HTMLResponse("")
    optional = [r for r in refs if r.get("IsOptional")]
    auto     = [r for r in refs if not r.get("IsOptional")]
    if not optional and not auto:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "partials/ej_related.html", {
        "optional": optional, "auto": auto, "article_name": name,
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
            lambda: ss.matcher.match(item.description, limit=30, long_text=item.long_text),
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
        "default_qty":    _bqty(item.qty),
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
    from db import save_gui_resource_mapping
    ss = get_session(request.session)
    if not ss.matcher:
        return HTMLResponse('<p class="error-msg">Kein Matcher geladen — bitte neu einlesen.</p>')
    res = next(
        (obj for obj in ss.matcher._pool if isinstance(obj, _Resource) and obj.id == resource_id),
        None,
    )
    if res is None:
        return HTMLResponse('<p class="error-msg">Ressource nicht gefunden.</p>')
    cur = ss.matches.get(item_id)
    if not (cur and cur.matched and cur.score > 0):
        # Noch keine Haupt-Zuordnung → Ressource wird Haupt-Match (Position gilt als
        # zugeordnet) und wird — wie bei „Ersetzen" — als Mapping gelernt.
        ss.matches[item_id] = MatchResult(matched=res, score=99.0, method="manual", confident=True)
        ss.d83_booking_qtys[item_id] = {"qty": _bqty(qty), "lfm_converted": False, "piece_len": None}
        if ss.d83_project:
            item = next((it for it in ss.d83_project.items if it.item_id == item_id), None)
            if item and item.description.strip():
                save_gui_resource_mapping(item.description, resource_id)
                ss.matcher.add_learned_resource(item.description, resource_id)
    else:
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
    """Sucht bestehende EJ-Projekte (Modus „Jobs zu bestehendem Projekt").

    Direkt über die EJ-DB, damit NUR echte Vermiet-Projekte erscheinen:
    IdProjectState IN (1 = Bestätigt/Auftrag, 2 = Angebot). Damit fallen Werkstatt-/
    Service-Aufträge (State 4), Lagerumbuchungen (5), Inventuren (6),
    Fertigungsplanung (11) und abgesagte (3) raus — die die Liste vorher zumüllten
    (z.B. lieferte „26-" 5555 Service- vs. ~840 echte Treffer). Bereits „durch"-
    gelaufene Projekte (Enddatum in der Vergangenheit) werden ausgeblendet; nur
    laufende/zukünftige (oder noch ohne Enddatum) erscheinen. Nächste zuerst.
    """
    ss = get_session(request.session)
    q = (q or "").strip()
    if not ss.ej_db_conn or len(q) < 2:
        return JSONResponse([])

    def _search():
        import pyodbc
        like = f"%{q}%"
        cn = pyodbc.connect(ss.ej_db_conn, timeout=8)
        try:
            rows = cn.cursor().execute(
                "SELECT TOP (?) IdProject, Number, Caption, StartDate, EndDate "
                "FROM Project "
                "WHERE IdProjectState IN (1, 2) "
                "  AND (EndDate IS NULL OR EndDate >= CAST(GETDATE() AS DATE)) "
                "  AND (Number LIKE ? OR Caption LIKE ? OR RefNumber LIKE ?) "
                "ORDER BY StartDate ASC",
                int(limit), like, like, like,
            ).fetchall()
            return [{
                "id":    int(r.IdProject),
                "num":   (r.Number or "").strip(),
                "name":  (r.Caption or "").strip(),
                "start": str(r.StartDate)[:10] if r.StartDate else "",
                "end":   str(r.EndDate)[:10] if r.EndDate else "",
            } for r in rows]
        finally:
            cn.close()

    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, _search)
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
                caption = _gcap(caption)
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
                caption = _gcap(caption)
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

            # Alternativ-/Eventualpositionen: ihre EJ-Gruppe als "Alternative" markieren
            # → stehen im Job, zählen aber nicht in die Angebotssumme. Nur tatsächlich
            # gebuchte (= aktive) Positionen werden markiert (Umschalten bleibt gültig).
            alt_gids: set[int] = set()
            for _it in ss.d83_project.items:
                if _it.item_id not in active_ids:
                    continue
                if not (_it.is_alt or getattr(_it, "is_eventual", False)):
                    continue
                _hg  = oz_to_hg.get(_it.oz or "")
                _lid = ss.d83_group_jobs.get(_hg, 1) if _hg else 1
                _job = lid_map.get(_lid, first_job_id)
                if import_mode == "positions":
                    _cap = f'[{_it.oz}] {_it.description}' if _it.oz else _it.description
                else:
                    _cap = oz_to_sub_cap.get(_it.oz or "")
                _gid = cap_to_gid.get((_job, _gcap(_cap)), 0) if _cap else 0
                if _gid:
                    alt_gids.add(_gid)
            if alt_gids:
                _gph = ",".join("?" for _ in alt_gids)
                cur.execute(
                    f"UPDATE StockType2JobGroup SET Alternative=1 "
                    f"WHERE IdStockType2JobGroup IN ({_gph})",
                    *alt_gids,
                )
                cn.commit()
                log.append({"ok": True, "text": f"{len(alt_gids)} Gruppe(n) als Alternative markiert ✓", "indent": False})

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
                grp_i = cap_to_gid.get((job_i, _gcap(g_cap)), 0) if g_cap else 0

                bq_i  = ss.d83_booking_qtys.get(item.item_id, {})
                qty_i = _bqty(bq_i.get("qty", item.qty))   # nie 0 → EJ verbietet Menge 0
                book_tasks.append((item, mr.article.nummer, id_st, job_i, grp_i, qty_i))

            # Artikel-Bundles (Motor-Hängepunkt-Pauschale, Auto-Extras, manuell
            # ergänzte Artikel) ebenfalls buchen — sie stehen in der bestätigten
            # Kostenvorschau, wurden bislang aber weder gebucht noch gespeichert.
            for item in ss.d83_project.items:
                if item.item_id not in active_ids:
                    continue
                item_bundles = ss.bundles.get(item.item_id, [])
                if not item_bundles:
                    continue
                hg_name = oz_to_hg.get(item.oz or "")
                lid_i   = ss.d83_group_jobs.get(hg_name, 1) if hg_name else 1
                job_i   = lid_map.get(lid_i, first_job_id)
                if import_mode == "positions":
                    g_cap = f'[{item.oz}] {item.description}' if item.oz else item.description
                else:
                    g_cap = oz_to_sub_cap.get(item.oz or "")
                grp_i = cap_to_gid.get((job_i, _gcap(g_cap)), 0) if g_cap else 0
                for b in item_bundles:
                    bart = b.get("article")
                    if not bart:
                        continue  # Ressourcen-Bundles werden in Abschnitt 4b gebucht
                    id_st_b = getattr(bart, "ej_id", 0)
                    if not id_st_b:
                        log.append({
                            "ok": False,
                            "text": f"Bundle-Artikel {getattr(bart, 'nummer', '?')} hat keine EJ-ID — bitte Sync ausführen",
                            "indent": True,
                        })
                        continue
                    book_tasks.append((item, bart.nummer, id_st_b, job_i, grp_i,
                                       _bqty(b.get("qty", 1))))

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
            try:
                dt_start = datetime.fromisoformat(start_date)
            except (ValueError, TypeError):
                dt_start = datetime.now()   # unerwartetes Format → heute, statt Abbruch
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
                grp_id_r  = cap_to_gid.get((job_id_r, _gcap(g_cap_r)), 0) if g_cap_r else 0
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
            # RFA-Inserts wurden nicht committet (Rollback beim close) → die lokal
            # gesammelten Ressourcen-Zeilen dürfen NICHT als Snapshot gespeichert
            # werden, sonst zeigt die Übersicht Ressourcen, die in EJ nicht existieren.
            res_bookings.clear()
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
            # Aus einem Entwurf hochgeladen? Dann dieselbe Zeile umwandeln
            # (Entwurf → Projekt), sonst ein neues Projekt anlegen.
            _draft_id = getattr(ss, "draft_id", None)
            _bcount   = len(bookings) + len(res_bookings)
            if _draft_id:
                await loop.run_in_executor(
                    None,
                    lambda: _db.promote_draft_to_project(
                        draft_id=_draft_id, name=proj_name, ej_project_id=id_project,
                        ej_job_ids=job_ids_csv, item_count=len(ss.d83_project.items),
                        booking_count=_bcount, ej_project_number=ej_project_number,
                        user=ss.ej_user or "",
                        # Zuordnung am Projekt festschreiben — das globale Profil
                        # überschreibt der nächste Import derselben Vorlage.
                        source_layout_json=_json.dumps(ss.excel_layout or {},
                                                       ensure_ascii=False)
                        if ss.source_kind == "excel" else "",
                    ),
                )
                project_db_id = _draft_id
                ss.draft_id = None
            else:
                project_db_id = await loop.run_in_executor(
                    None,
                    lambda: _db.save_project(
                        name=proj_name,
                        ej_project_id=id_project,
                        gaeb_name=ss.d83_name,
                        item_count=len(ss.d83_project.items),
                        booking_count=_bcount,
                        gaeb_bytes=ss.x83_bytes,
                        ej_job_ids=job_ids_csv,
                        ej_project_number=ej_project_number,
                        source_kind=ss.source_kind or "gaeb",
                        source_layout_json=_json.dumps(ss.excel_layout or {},
                                                       ensure_ascii=False)
                        if ss.source_kind == "excel" else "",
                    ),
                )
            from matcher import resolve_time_factor as _resolve_tf
            _curves = _db.load_time_factor_curves_db()
            # Einzelpreis je Buchung aus dem TATSÄCHLICH gebuchten Artikel (Haupt- ODER
            # Bundle-Artikel) — nicht pauschal vom Haupt-Match, sonst bekämen Bundle-Zeilen
            # im lokalen Snapshot den Preis des Hauptartikels.
            _art_obj_by_key: dict = {}
            for _iid, _mr in ss.matches.items():
                _a = getattr(_mr, "article", None)
                if _a and getattr(_a, "ej_id", 0):
                    _art_obj_by_key[(_iid, int(_a.ej_id))] = _a
            for _iid, _blist in ss.bundles.items():
                for _b in _blist:
                    _a = _b.get("article")
                    if _a and getattr(_a, "ej_id", 0):
                        _art_obj_by_key[(_iid, int(_a.ej_id))] = _a

            def _ep_for(item_id, ej_id):
                a = _art_obj_by_key.get((item_id, int(ej_id or 0)))
                if not a:
                    return 0.0
                return float(getattr(a, "mietpreis", 0) or 0) * _resolve_tf(
                    _curves, getattr(a, "id_time_factor", 0), einsatztage)

            for bk in bookings:
                ep_bk = _ep_for(bk["item_id"], bk.get("ej_stock_type_id"))
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
