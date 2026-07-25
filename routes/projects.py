"""Projektliste + D84-Export aus gespeicherten Projekten."""
import asyncio
import logging
import os
import pathlib
import re
import xml.etree.ElementTree as ET

import pyodbc
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

import db as _db
from state import get_session, templates
from routes.import_ import _clean_gaeb_for_export

router = APIRouter()


def _is_herstellertyp(label: str) -> bool:
    low = label.lower()
    return ("manufacturer" in low or "hersteller" in low
            or "type" in low or "typ" in low)


def _extract_bidder_fields(gaeb_bytes: bytes) -> tuple[list[dict], list[dict]]:
    """Ermittelt die Bieter-Antwortfelder (TextComplement Kind=Bidder) einer Ausschreibung.

    Gibt (ht_labels, single_fields) zurück:
      ht_labels    – Hersteller/Typ-Felder, gesammelt je Label: {label, count}
                     (werden automatisch aus dem Artikel befüllt)
      single_fields – sonstige Felder EINZELN je Position mit Kontext:
                     {item_id, label, oz, gruppe, kurztext}
    """
    try:
        root = ET.fromstring(gaeb_bytes.decode("utf-8", errors="replace").encode("utf-8"))
    except Exception:
        return [], []
    m = re.search(r"\{(.+?)\}", root.tag)
    ns = m.group(1) if m else ""
    def tag(n): return f"{{{ns}}}{n}" if ns else n
    parent = {c: p for p in root.iter() for c in p}

    # Positions-Kontext (OZ, Gruppe, Kurztext) je item_id über den Parser
    item_ctx: dict[str, object] = {}
    try:
        from gaeb_parser import parse_gaeb
        proj = parse_gaeb(gaeb_bytes)
        item_ctx = {it.item_id: it for it in proj.items}
    except Exception:
        pass

    ht_counter: dict[str, int] = {}
    single: list[dict] = []
    for tc in root.iter(tag("TextComplement")):
        if tc.get("Kind") != "Bidder":
            continue
        cap_el = tc.find(tag("ComplCaption"))
        label = " ".join("".join(cap_el.itertext()).split()) if cap_el is not None else ""
        if _is_herstellertyp(label):
            ht_counter[label] = ht_counter.get(label, 0) + 1
        else:
            el = tc
            item_id = ""
            while el is not None:
                if el.tag == tag("Item"):
                    item_id = el.get("ID", ""); break
                el = parent.get(el)
            it = item_ctx.get(item_id)
            cpath = list(it.category_path) if (it and it.category_path) else []
            single.append({
                "item_id":   item_id,
                "label":     label,
                "oz":        getattr(it, "oz", "") if it else "",
                "gruppe":    cpath[-1] if cpath else "",
                "breadcrumb": " › ".join(cpath),
                "kurztext":  (getattr(it, "description", "") or "")[:70] if it else "",
            })
    ht_labels = [{"label": l, "count": c} for l, c in ht_counter.items()]
    ht_labels.sort(key=lambda d: -d["count"])
    # nach Gruppe sortieren + stabilen Index vergeben (für die Formularfelder)
    single.sort(key=lambda s: (s["breadcrumb"], s["oz"]))
    for i, s in enumerate(single):
        s["idx"] = i
    return ht_labels, single


async def _attach_project_numbers(ss, projects: list[dict]) -> None:
    """Sorgt dafür, dass jedes Projekt eine 'ej_project_number' hat — reiner API-Weg.

    Neue Projekte tragen die Nummer (z.B. „26-0994") bereits gespeichert, weil sie
    beim Anlegen einmalig per API geholt wird. Altprojekte ohne gespeicherte Nummer
    werden hier EINMALIG per API nachgeladen und lokal gespeichert — danach nie
    wieder. Kein Zugriff mehr auf die EJ-DB; ohne Fehler bleibt das Feld leer.
    """
    for p in projects:
        p.setdefault("ej_project_number", "")
    missing = [
        p for p in projects
        if not (p.get("ej_project_number") or "").strip() and p.get("ej_project_id")
    ]
    if not missing or not ss.ej_client:
        return
    loop = asyncio.get_event_loop()

    async def _fill(p: dict) -> None:
        try:
            num = await loop.run_in_executor(
                None, lambda: ss.ej_client.get_project_number(int(p["ej_project_id"]))
            )
            if num:
                p["ej_project_number"] = num
                await loop.run_in_executor(
                    None, lambda: _db.set_project_number(int(p["id"]), num)
                )
        except Exception as _e:
            logging.error("Projektnummer (API) fehlgeschlagen: %s", _e)

    await asyncio.gather(*[_fill(p) for p in missing])


@router.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    ss = get_session(request.session)
    projects = _db.list_projects()
    await _attach_project_numbers(ss, projects)
    return templates.TemplateResponse(request, "projects.html", {
        "projects": projects,
        "is_admin": ss.is_admin,
    })


@router.post("/api/projects/{project_id}/delete", response_class=HTMLResponse)
async def project_delete(project_id: int, request: Request):
    ss = get_session(request.session)
    if not ss.is_admin:
        return HTMLResponse('<span class="error-msg">Nur für Admins</span>', status_code=403)
    _db.delete_project(project_id)
    projects = _db.list_projects()
    await _attach_project_numbers(ss, projects)
    return templates.TemplateResponse(request, "projects.html", {
        "projects": projects,
        "is_admin": ss.is_admin,
    })


@router.get("/api/projects/{project_id}/export-dialog", response_class=HTMLResponse)
async def export_dialog(project_id: int, request: Request):
    """Zeigt vor dem D84-Export einen Dialog: pro Bieter-Antwortfeld-Typ kann
    gewählt werden, was eingetragen wird (automatisch / fester Text / leer)."""
    gaeb_bytes, _ = _db.get_project_gaeb(project_id)
    if not gaeb_bytes:
        return HTMLResponse('<div class="error-msg">Keine GAEB-Datei für dieses Projekt.</div>')
    ht_labels, single_fields = _extract_bidder_fields(gaeb_bytes)
    return templates.TemplateResponse(request, "partials/export_dialog.html", {
        "project_id":    project_id,
        "ht_labels":     ht_labels,
        "single_fields": single_fields,
    })


@router.post("/api/projects/{project_id}/export-d84")
async def project_export_d84(project_id: int, request: Request):
    ss = get_session(request.session)

    # Bieter-Feld-Konfiguration aus dem Dialog.
    form = await request.form()
    # Hersteller/Typ-Felder gesammelt je Label: label -> (mode, text)
    bidder_config: dict[str, tuple[str, str]] = {}
    for i in range(int(form.get("field_count", 0) or 0)):
        lbl  = str(form.get(f"label_{i}", ""))
        mode = str(form.get(f"mode_{i}", "leer"))
        text = str(form.get(f"text_{i}", "")).strip()
        bidder_config[lbl] = (mode, text)
    # Einzelfelder je Position: item_id -> text
    single_text: dict[str, str] = {}
    for j in range(int(form.get("single_count", 0) or 0)):
        iid = str(form.get(f"single_item_{j}", ""))
        txt = str(form.get(f"single_text_{j}", "")).strip()
        if iid and txt:
            single_text[iid] = txt

    if not ss.ej_db_conn:
        raise HTTPException(400, "Keine EasyJob-DB-Verbindung — bitte erst einloggen.")

    proj = _db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "Projekt nicht gefunden")

    gaeb_bytes, gaeb_name = _db.get_project_gaeb(project_id)
    if not gaeb_bytes:
        raise HTTPException(404, "Keine GAEB-Datei für dieses Projekt gespeichert")

    ej_project_id = proj.get("ej_project_id") or 0
    if not ej_project_id:
        raise HTTPException(400, "Projekt hat keine EasyJob-Projekt-ID")

    # Kosten werden ausschließlich über die beim Import angelegten Jobs aggregiert,
    # nicht über das ganze Projekt — sonst würden im Bestehend-Projekt-Modus fremde
    # Jobs mitgerechnet.
    job_ids = [int(x) for x in (proj.get("ej_job_ids") or "").split(",") if x.strip().isdigit()]
    if not job_ids:
        raise HTTPException(400, "Projekt hat keine gespeicherten Job-IDs — bitte neu importieren.")
    job_ph = ",".join("?" for _ in job_ids)

    bookings = _db.get_project_bookings(project_id)

    # item_id → ej_group_id (IdStockType2JobGroup)
    group_by_item: dict[str, int] = {
        b["item_id"]: int(b["ej_group_id"])
        for b in bookings
        if b.get("ej_group_id")
    }

    # EJ-DB: Gruppenkosten lesen
    # Artikel-Kosten pro Gruppe: SUM(Anzahl × Preis) aller Artikel in der Gruppe
    # Personal-Kosten pro Gruppe: SUM(TotalPrice) aller Ressourcen in der Gruppe
    group_art_cost:  dict[int, float] = {}
    group_pers_cost: dict[int, float] = {}

    cn  = pyodbc.connect(ss.ej_db_conn)
    cur = cn.cursor()

    cur.execute(
        f"""
        SELECT s2j.IdStockType2JobGroup,
               SUM(s2j.Factor * s2j.TimeFactor * COALESCE(s2j.RentalPrice, s2j.BasePrice, 0)) AS GruppenKosten
        FROM StockType2Job s2j
        WHERE s2j.IdJob IN ({job_ph})
          AND s2j.IdStockType2JobGroup IS NOT NULL
          AND s2j.IdStockType2JobGroup > 0
        GROUP BY s2j.IdStockType2JobGroup
        """,
        *job_ids,
    )
    for r in cur.fetchall():
        group_art_cost[int(r[0])] = float(r[1] or 0)

    cur.execute(
        f"""
        SELECT rfa.IdStockType2JobGroup,
               SUM(rfa.TotalPrice) AS PersKosten
        FROM ResourceFunctionAllocation rfa
        WHERE rfa.IdJob IN ({job_ph})
          AND rfa.IdStockType2JobGroup IS NOT NULL
          AND rfa.IdStockType2JobGroup > 0
        GROUP BY rfa.IdStockType2JobGroup
        """,
        *job_ids,
    )
    for r in cur.fetchall():
        group_pers_cost[int(r[0])] = float(r[1] or 0)

    # Gruppenbezeichnungen: "[01.01.01] Beschreibung" → OZ → IdStockType2JobGroup
    # Fallback für Ressourcen-Positionen, die nicht in project_bookings stehen
    cur.execute(
        f"""
        SELECT g.Caption, g.IdStockType2JobGroup
        FROM StockType2JobGroup g
        WHERE g.IdJob IN ({job_ph})
        """,
        *job_ids,
    )
    oz_to_group: dict[str, int] = {}
    for r in cur.fetchall():
        cap = r[0] or ""
        if cap.startswith('[') and ']' in cap:
            oz = cap[1:cap.index(']')].strip()
            if oz:
                oz_to_group[oz] = int(r[1])

    cn.close()

    # GAEB-XML laden, DA83 → DA84
    xml_text = gaeb_bytes.decode("utf-8", errors="replace")
    xml_text = xml_text.replace(
        "http://www.gaeb.de/GAEB_DA_XML/DA83/",
        "http://www.gaeb.de/GAEB_DA_XML/DA84/",
    )

    root = ET.fromstring(xml_text.encode("utf-8"))
    ns_match = re.search(r'\{(.+?)\}', root.tag)
    ns = ns_match.group(1) if ns_match else ""

    def tag(name: str) -> str:
        return f"{{{ns}}}{name}" if ns else name

    # Parent-Map für OZ-Rekonstruktion (Fallback für Ressourcen-Positionen)
    parent_map: dict = {child: parent for parent in root.iter() for child in parent}

    def _item_oz(item_el) -> str:
        parts = []
        el = item_el
        while el is not None:
            local = el.tag.split('}')[-1] if '}' in el.tag else el.tag
            if local in ('BoQCtgy', 'Item'):
                rno = el.get("RNoPart", "")
                if rno:
                    parts.append(rno.zfill(2))
            el = parent_map.get(el)
        parts.reverse()
        return '.'.join(parts)

    dp_el = root.find(f".//{tag('DP')}")
    if dp_el is not None:
        dp_el.text = "84"

    # Ersteller-Kennung (GAEBInfo) auf das eigene Programm setzen (aus .env) —
    # sonst stünde weiterhin das Ausschreibungs-Programm (z.B. AVAPLAN) drin.
    gaeb_info = root.find(f".//{tag('GAEBInfo')}")
    if gaeb_info is not None:
        for env_key, tag_name in [("GAEB_PROG_SYSTEM", "ProgSystem"),
                                  ("GAEB_PROG_NAME", "ProgName")]:
            val = os.environ.get(env_key, "").strip()
            if not val:
                continue
            el = gaeb_info.find(tag(tag_name))
            if el is None:
                el = ET.SubElement(gaeb_info, tag(tag_name))
            el.text = val

    # AwardInfo auf das Nötige (Währung) reduzieren — Ausschreibungs-Termine/
    # Verfahren/Ort (OpenDate, CnstStart, SubmLoc, Cat …) gehören nicht ins Angebot.
    award_info = root.find(f".//{tag('AwardInfo')}")
    if award_info is not None:
        for child in list(award_info):
            lc = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if lc != "Cur":
                award_info.remove(child)

    # Bieter-Block: Auftraggeber (OWN) → Bieter (CTR) mit EIGENEN Firmendaten (.env).
    # Ohne das Überschreiben stünde sonst der Auftraggeber als Bieter in der D84.
    award_el = root.find(f".//{tag('Award')}")
    if award_el is not None:
        own_el = award_el.find(tag("OWN"))
        if own_el is not None:
            own_el.tag = tag("CTR")
            addr = own_el.find(tag("Address"))
            if addr is None:
                addr = ET.SubElement(own_el, tag("Address"))
            for fname, val in [
                ("Name1",  os.environ.get("BIDDER_NAME1", "")),
                ("Name2",  ""),  # Auftraggeber-Zusatz entfernen
                ("Street", os.environ.get("BIDDER_STREET", "")),
                ("PCode",  os.environ.get("BIDDER_PCODE", "")),
                ("City",   os.environ.get("BIDDER_CITY", "")),
            ]:
                el = addr.find(tag(fname))
                if val.strip():
                    if el is None:
                        el = ET.SubElement(addr, tag(fname))
                    el.text = val.strip()
                elif el is not None:
                    addr.remove(el)  # kein eigener Wert → fremdes Feld raus

    for item_el in root.iter(tag("Item")):
        item_id = item_el.get("ID", "")

        qty_el  = item_el.find(tag("Qty"))
        qty_val = float(qty_el.text.replace(",", ".")) if qty_el is not None and qty_el.text else 1.0

        grp_id = group_by_item.get(item_id, 0)
        if not grp_id:
            # Fallback: OZ aus GAEB-Hierarchie → EJ-Gruppenbezeichnung
            grp_id = oz_to_group.get(_item_oz(item_el), 0)
        if grp_id:
            # Gesamtkosten der Gruppe (Artikel + Personal) ÷ GAEB-Menge = EP
            total = group_art_cost.get(grp_id, 0.0) + group_pers_cost.get(grp_id, 0.0)
            ep = round(total / qty_val, 3) if qty_val else 0.0
        else:
            ep = 0.0

        gp = round(qty_val * ep, 2)

        for old in item_el.findall(tag("UP")) + item_el.findall(tag("IT")):
            item_el.remove(old)

        qu_el      = item_el.find(tag("QU"))
        anchor     = qu_el if qu_el is not None else qty_el
        insert_idx = list(item_el).index(anchor) + 1 if anchor is not None else len(list(item_el))

        up_el      = ET.Element(tag("UP"))
        up_el.text = f"{ep:.3f}"
        it_el      = ET.Element(tag("IT"))
        it_el.text = f"{gp:.2f}"
        item_el.insert(insert_idx, it_el)
        item_el.insert(insert_idx, up_el)

    # Bieter-Antwortfelder je Position + zugehöriges Label erfassen (vor Cleanup).
    # Nur Positionen mit vorgesehenem <TextComplement Kind="Bidder"> dürfen befüllt werden.
    bidder_field_by_item: dict[str, str] = {}
    for tc in root.iter(tag("TextComplement")):
        if tc.get("Kind") != "Bidder":
            continue
        cap_el = tc.find(tag("ComplCaption"))
        label = " ".join("".join(cap_el.itertext()).split()) if cap_el is not None else ""
        el = tc
        while el is not None:
            if el.tag == tag("Item"):
                bidder_field_by_item[el.get("ID", "")] = label
                break
            el = parent_map.get(el)

    # Nur Preisdaten behalten, alles andere raus
    _clean_gaeb_for_export(root, ns)

    # Befüllung je Feld-Typ gemäß Dialog-Konfiguration:
    #   auto → Hersteller · Detail aus gebuchtem Artikel · text → fester Text · leer → nichts
    art_by_num  = {a["nummer"]: a for a in _db.load_articles_db()}
    booked_art  = {b["item_id"]: b.get("art_num") for b in bookings}

    def _auto_text(item_id: str) -> str:
        art = art_by_num.get(booked_art.get(item_id))
        if not art:
            return ""
        parts = [str(art.get(f, "") or "").strip() for f in ("hersteller", "detail")]
        return " · ".join(p for p in parts if p)

    text_by_item: dict[str, str] = {}
    for item_id, label in bidder_field_by_item.items():
        if _is_herstellertyp(label):
            mode, fixed = bidder_config.get(label, ("leer", ""))
            if mode == "auto":
                t = _auto_text(item_id)
            elif mode == "text":
                t = fixed
            else:
                t = ""
        else:
            # Einzelfeld: individueller Text je Position (aus dem Dialog)
            t = single_text.get(item_id, "")
        if t:
            text_by_item[item_id] = t

    if text_by_item:
        SPAN_STYLE = "font-family:Arial;font-size:10pt;"
        for item_el in root.iter(tag("Item")):
            txt = text_by_item.get(item_el.get("ID", ""))
            if not txt:
                continue
            # Bieterergänzung wie in der Ausschreibung vorgesehen (Kind="Bidder")
            desc = ET.SubElement(item_el, tag("Description"))
            ct   = ET.SubElement(desc, tag("CompleteText"))
            dt   = ET.SubElement(ct,   tag("DetailTxt"))
            tc   = ET.SubElement(dt,   tag("TextComplement"), {"MarkLbl": "1", "Kind": "Bidder"})
            cb   = ET.SubElement(tc,   tag("ComplBody"))
            sp   = ET.SubElement(cb,   tag("span"), {"style": SPAN_STYLE})
            sp.text = txt

    # Summen (Totals): je Gruppe (BoQCtgy) und gesamt mit MwSt (BoQInfo) — wie im
    # Standard-Angebot. IT der Positionen aufsummieren (nach dem Cleanup, also final).
    vat_rate = float(os.environ.get("GAEB_VAT", "19") or 19)
    pmap = {c: p for p in root.iter() for c in p}
    ctgy_sum: dict = {}
    grand = 0.0
    for item_el in root.iter(tag("Item")):
        it_el = item_el.find(tag("IT"))
        if it_el is None or not (it_el.text or "").strip():
            continue
        try:
            val = float(it_el.text)
        except ValueError:
            continue
        grand += val
        # zu ALLEN übergeordneten Gruppen zählen (Unter- und Hauptgruppen-Summen)
        el = pmap.get(item_el)
        while el is not None:
            if el.tag == tag("BoQCtgy"):
                ctgy_sum[el] = ctgy_sum.get(el, 0.0) + val
            el = pmap.get(el)
    for ctgy_el, s in ctgy_sum.items():
        tot = ET.SubElement(ctgy_el, tag("Totals"))
        ET.SubElement(tot, tag("Total")).text = f"{s:.2f}"
    boq_info = root.find(f".//{tag('BoQInfo')}")
    if boq_info is not None:
        tot = ET.SubElement(boq_info, tag("Totals"))
        ET.SubElement(tot, tag("Total")).text      = f"{grand:.2f}"
        ET.SubElement(tot, tag("VAT")).text        = f"{vat_rate:.2f}"
        ET.SubElement(tot, tag("TotalGross")).text = f"{grand * (1 + vat_rate / 100):.2f}"

    ET.register_namespace("", ns)
    out_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    stem     = pathlib.Path(gaeb_name or "export").stem
    filename = f"{stem}.x84"
    return Response(
        content=out_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
