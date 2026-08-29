"""Personalplanung: /api/crew/* — Crew-Matrix zum laufenden Import.

Die Arbeitskopie liegt in der Session (``ss.crew``), wie matches/bundles auch.
Gespeichert wird sie beim Entwurf-Speichern in die crew_*-Tabellen an derselben
Projektzeile (siehe ``db.save_crew_plan``).
"""
import logging
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               Response)

import db as _db
from crew_plan import (MAX_DAYS, MAX_PERSONS, CrewPlan, Phase,
                       default_phases, format_number, lv_row_candidates,
                       new_plan, parse_day, parse_number, schedule_from_project)
from state import get_session, templates

router = APIRouter()

# Titel eines gerade angelegten Menüpunkts. Solange er so heißt, gilt er als
# unbenannt und die Oberfläche setzt den Eingabefokus hinein.
NEUER_MENUEPUNKT = "Neuer Menüpunkt"

_DOW = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_MONTH = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember")

# Phasenfarben nach Name — unbekannte Phasen bekommen Grau.
_PHASE_COLORS = {
    "aufbau":        "#1565c0",
    "proben":        "#00838f",
    "veranstaltung": "#e05500",
    "abbau":         "#6d4c41",
}


def _phase_color(name: str) -> str:
    return _PHASE_COLORS.get((name or "").strip().lower(), "#78909c")


# ─── Kontext für das Panel ───────────────────────────────────────────────────

def crew_ctx(ss) -> dict:
    """Alles, was ``partials/crew_matrix.html`` braucht. Auch ohne Planung
    aufrufbar — dann steht ``crew`` auf None und das Panel zeigt den Startknopf."""
    plan: CrewPlan | None = ss.crew
    # Vorgabe für den frisch angelegten Abschnitt: nur ``_panel`` setzt ihn, die
    # Vorlage wird aber auch von der Importseite und als OOB-Fragment gerendert.
    if plan is None:
        return {"crew": None, "crew_days": [], "crew_months": [], "crew_phases": [],
                "crew_groups": [], "crew_menu_titles": {}, "crew_menu_pos": {},
                "crew_menu_nk": {},
                "crew_phase_list": [],
                "crew_totals": {}, "crew_day_totals": {},
                "crew_row_stats": {}, "crew_lv_count": len(_candidates(ss)),
                "crew_default_range": _default_range(ss),
                "crew_positions": [], "crew_pos_colors": {}, "crew_bands": {},
                "crew_unassigned": [],
                "crew_lv_schedule": _schedule(ss)}

    # Positionen und Zeilen folgen dem Matching — bei jedem Aufbau der Ansicht.
    # Sonst müsste man einen Knopf drücken, damit die Liste stimmt, und hätte nach
    # jeder Match-Korrektur einen veralteten Stand vor sich.
    kandidaten = _candidates(ss)
    plan.sync_positions([c["item_id"] for c in kandidaten])
    plan.sync_rows(kandidaten)

    days = plan.days()
    day_totals = plan.day_totals()
    peak = max(day_totals.values(), default=0)

    day_ctx = []
    for d in days:
        key = d.isoformat()
        ph = plan.phase_of(key)
        day_ctx.append({
            "key":   key,
            "num":   d.day,
            "dow":   _DOW[d.weekday()],
            "we":    d.weekday() >= 5,
            "total": day_totals.get(key, 0),
            "peak":  bool(peak) and day_totals.get(key, 0) == peak,
            "phase": ph.name if ph else "",
        })

    # Monats- und Phasenbänder als (Beschriftung, Spaltenzahl)
    months: list[dict] = []
    for d in days:
        label = f"{_MONTH[d.month - 1]} {d.year}"
        if months and months[-1]["name"] == label:
            months[-1]["span"] += 1
        else:
            months.append({"name": label, "span": 1})

    phases: list[dict] = []
    for d in day_ctx:
        name = d["phase"]
        if phases and phases[-1]["name"] == name:
            phases[-1]["span"] += 1
        else:
            phases.append({"name": name, "span": 1, "color": _phase_color(name)})

    farben_ = _pos_colors(plan)
    items_ = _lv_items(ss)
    row_stats = {}
    for r in plan.rows:
        std = plan.default_pos_for(r)
        row_stats[r.id] = {
            "mt": plan.manntage(r),
            "total": plan.row_total(r),
            "spesen": plan.row_spesen(r),
            "hotel": plan.row_hotel(r),
            "rk": plan.row_rk(r),
            "naechte": plan.naechte(r),
            # Position, auf die Eingaben in dieser Zeile ohne weiteres Zutun laufen.
            # Sichtbar im Zeilenkopf, sonst wüsste niemand, wohin seine Zahlen gehen.
            "std_oz": (getattr(items_.get(std), "oz", "") or "") if std else "",
            "std_color": farben_.get(std, "") if std else "",
            "std_titel": ((getattr(items_.get(std), "oz", "") or "?") + " " +
                          (getattr(items_.get(std), "description", "") or "")).strip()
                         if std else "",
        }

    # Farbe und Titel je Zelle. Ein eigener Bandstreifen unter jeder Zeile hat die
    # Matrix doppelt so hoch gemacht; die Position steht jetzt als Hinterlegung in
    # der Zahlenzelle selbst.
    day_keys = [d["key"] for d in day_ctx]
    colors = _pos_colors(plan)
    items = _lv_items(ss)
    bands: dict[int, list[dict]] = {}
    for row in plan.rows:
        band = []
        for key in day_keys:
            item_id = row.assign.get(key)
            besetzt = bool(row.cells.get(key))
            farbe = colors.get(item_id, "#b0bec5") if item_id else ""
            band.append({
                "item_id": item_id or "",
                "color":   farbe,
                # Nur besetzte Tage werden eingefärbt — eine Zuordnung ohne
                # Besetzung hat nichts zu zeigen.
                # Die Position zeigt sich als Rahmen um die Zelle, nicht als
                # Füllung: das Feld bleibt weiß und die Zahl schwarz, also lesbar,
                # und die Farbe stimmt mit der des Chips überein statt abgeschwächt
                # zu sein.
                "fill":    farbe if (item_id and besetzt) else "",
                "titel":   ((getattr(items.get(item_id), "oz", "") or "?") + " " +
                            (getattr(items.get(item_id), "description", "") or "")).strip()
                           if item_id else "",
                "offen":   besetzt and not item_id,
            })
        bands[row.id] = band

    phase_list = [{
        "index": i, "name": ph.name, "from": ph.day_from, "to": ph.day_to,
        "color": _phase_color(ph.name),
        "days": (parse_day(ph.day_to) - parse_day(ph.day_from)).days + 1
                if ph.day_from and ph.day_to else 0,
    } for i, ph in enumerate(plan.phases)]

    return {
        "crew":            plan,
        "crew_days":       day_ctx,
        "crew_phase_list": phase_list,
        "crew_months":     months,
        "crew_phases":     phases,
        "crew_groups":     plan.groups(),
        "crew_menu_titles": _menu_titles(ss, plan),
        "crew_menu_pos":    _menu_pos_ctx(ss, plan),
        "crew_menu_nk":     _menu_nk_ctx(ss, plan),
        "crew_totals":     plan.totals(),
        "crew_day_totals": day_totals,
        "crew_row_stats":  row_stats,
        "crew_default_range": _default_range(ss),
        "crew_positions":  _positions_ctx(ss, plan),
        "crew_pos_colors": _pos_colors(plan),
        "crew_bands":      bands,
        "crew_unassigned": plan.unassigned(),
        "crew_lv_schedule": _schedule(ss),
    }


# Farbe je Position — dieselbe in der Zelle und im Chip, damit man einen Block ohne
# Lesen der Nummer wiedererkennt.
_POS_COLORS = ("#1565c0", "#e05500", "#6d4c41", "#00838f",
               "#5c35b0", "#2e7d32", "#ad1457", "#455a64")


def _menu_titles(ss, plan) -> dict:
    """Überschrift je Menüpunkt: bei LV-Positionen „OZ Kurztext", bei eigenen der
    eingetippte Titel. Der leere Schlüssel steht für noch nicht einsortierte Zeilen."""
    items = _lv_items(ss)
    out = {"": "Ohne Abschnitt"}
    for key in plan.menu_keys():
        if plan.is_custom(key):
            out[key] = plan.menu_title(key) or "Ohne Titel"
        else:
            item = items.get(key)
            oz = getattr(item, "oz", "") or ""
            desc = (getattr(item, "description", "") or "").strip()
            out[key] = f"{oz} {desc}".strip() or "Position nicht mehr im LV"
    return out


def _marke(ss, plan, pid: str) -> dict | None:
    """Eine Position als Marke: Nummer und Farbe, sonst nichts."""
    if not pid:
        return None
    items = _lv_items(ss)
    return {"item_id": pid,
            "oz": getattr(items.get(pid), "oz", "") or "?",
            "desc": (getattr(items.get(pid), "description", "") or "").strip(),
            "color": _pos_colors(plan).get(pid, "#b0bec5")}


def _menu_pos_ctx(ss, plan) -> dict:
    """Je Abschnitt die Standard-Position — was dort eingetragen wird, läuft ohne
    weiteren Klick darauf. Genau eine: mehrere wären nicht eindeutig."""
    return {key: _marke(ss, plan, plan.menu_pos(key)) for key in plan.menu_keys()}


def _menu_nk_ctx(ss, plan) -> dict:
    """Je Abschnitt die Position, die seine Nebenkosten übernimmt."""
    return {key: _marke(ss, plan, plan.menu_nk_pos.get(key, ""))
            for key in plan.menu_keys()}


def _lv_items(ss) -> dict:
    return {it.item_id: it for it in (ss.d83_project.items if ss.d83_project else [])}


def _ist_tageseinheit(item) -> bool:
    """Ist die Menge der Position in Tagen angegeben? Nur dann lässt sie sich mit
    geplanten Manntagen vergleichen."""
    from crew_plan import _TAGES_EINHEITEN
    einheit = (getattr(item, "unit", "") or "").strip().lower().rstrip(".")
    return einheit in _TAGES_EINHEITEN


def _is_pauschal(item) -> bool:
    """Pauschalposition? Dann ist der EP die Summe, nicht die Summe je Einheit."""
    unit = (getattr(item, "unit", "") or "").strip().lower()
    if unit.startswith("psch") or unit.startswith("pau"):
        return True
    return float(getattr(item, "qty", 0) or 0) <= 1


def _positions_ctx(ss, plan) -> list[dict]:
    """Die abgedeckten LV-Positionen mit Soll/Ist — die Palette unter der Matrix."""
    items = _lv_items(ss)
    stats = plan.position_stats()
    out: list[dict] = []
    # Nur echte LV-Positionen: eigene Menüpunkte sind Überschriften und tauchen
    # deshalb weder als Chip noch im Soll/Ist-Abgleich auf.
    for i, item_id in enumerate(plan.target_keys()):
        item = items.get(item_id)
        eigen = False
        st = stats.get(item_id) or {"mt": 0, "cost": 0.0, "rows": 0}
        mode = plan.pos_mode(item_id)
        soll = float(getattr(item, "qty", 0) or 0) if item else 0.0
        pauschal = True if eigen else (_is_pauschal(item) if item else True)
        ep = st["cost"] if pauschal else (st["cost"] / soll if soll else 0.0)
        parent_id = plan.position_parent(item_id)
        out.append({
            "item_id":  item_id,
            "eigen":    eigen,
            "title":    plan.menu_title(item_id) if eigen else "",
            "oz":       getattr(item, "oz", "") or "",
            "desc":     (getattr(item, "description", "") or "").strip() or "(ohne Kurztext)",
            "soll":     soll,
            "unit":     getattr(item, "unit", "") or "",
            "pauschal": pauschal,
            "mode":     mode,
            "color":    _POS_COLORS[i % len(_POS_COLORS)],
            "mt":       st["mt"],
            "cost":     st["cost"],
            "rows":     st["rows"],
            "ep":       ep,
            # Mengenprüfung nur, wenn die LV-Menge Manntage sind. Eine Pauschale
            # macht keine Mengenaussage, und „50 h" gegen Manntage zu vergleichen
            # wären Äpfel und Birnen — beides gilt darum als in Ordnung.
            "menge_ok": (pauschal or not _ist_tageseinheit(item)
                         or abs(st["mt"] - soll) < 0.01),
            "parent_oz": (getattr(items.get(parent_id), "oz", "") or "") if parent_id else "",
            # Frisch angelegt und noch unbenannt → die Oberfläche setzt den
            # Eingabefokus dorthin, damit man sofort tippen kann.
            "frisch":   eigen and plan.menu_title(item_id) == NEUER_MENUEPUNKT,
            # Eigene Menüpunkte haben keinen LV-Eintrag — das ist kein Fehler.
            "fehlt":    item is None and not eigen,
        })
    return out


def _pos_colors(plan) -> dict[str, str]:
    """Farbe je Zuordnungsziel — eigene Menüpunkte brauchen keine, auf sie wird
    nichts gebucht."""
    return {item_id: _POS_COLORS[i % len(_POS_COLORS)]
            for i, item_id in enumerate(plan.target_keys())}


def _candidates(ss) -> list[dict]:
    """LV-Positionen mit Personal-Match, in LV-Reihenfolge. Grundlage des Abgleichs."""
    if not ss.d83_project:
        return []
    try:
        return lv_row_candidates(ss.d83_project, ss.matches or {})
    except Exception as e:            # Matcher-Pool halb aufgebaut o.ä.
        logging.error("crew: LV-Kandidaten konnten nicht ermittelt werden: %s", e)
        return []


def _schedule(ss) -> dict:
    """Terminplan aus den LV-Vorbemerkungen — einmal je Import ermittelt und in der
    Session gehalten. Die Vorbemerkungen sind bis zu 50 kB Text; das bei jedem
    Rendern des Panels neu zu durchsuchen wäre Verschwendung."""
    if ss.crew_schedule:
        return {} if ss.crew_schedule.get("leer") else ss.crew_schedule
    if not ss.d83_project:
        return {}
    gefunden = schedule_from_project(ss.d83_project) or {"leer": True}
    ss.crew_schedule = gefunden
    return {} if gefunden.get("leer") else gefunden


def _default_range(ss) -> dict:
    """Vorschlag für die Zeitachse.

    Erste Wahl ist der Terminplan aus dem LV: er nennt Aufbau- und Abbauzeit und
    damit die ganze Spanne, die die Matrix braucht. Das Start-/Enddatum in der
    Projektanlage links taugt schlechter — wer dort eine Veranstaltung auswählt,
    bekommt deren Laufzeit eingesetzt, also ohne Auf- und Abbau.
    """
    plan = _schedule(ss)
    if plan:
        return {"from": plan["date_from"], "to": plan["date_to"], "quelle": "lv"}
    setup = ss.d83_draft_setup or {}
    today = date.today().isoformat()
    return {"from": setup.get("start_date") or today,
            "to":   setup.get("end_date") or setup.get("start_date") or today,
            "quelle": "setup" if setup.get("start_date") else ""}


def _panel(request: Request, ss, fresh_menu: str = "") -> HTMLResponse:
    # crew_oob: die Kennzahlen in der Panel-Kopfzeile stehen außerhalb von
    # #crew-panel und müssen als eigenes Fragment mitkommen.
    # fresh_menu: der gerade angelegte Abschnitt — sein Titelfeld bekommt den Fokus.
    resp = templates.TemplateResponse(request, "partials/crew_matrix.html",
                                      {"S": ss, "crew_oob": True,
                                       "crew_fresh_menu": fresh_menu, **crew_ctx(ss)})
    # Dazu die Kennzahlenzeile über der Positionsliste: die Personalkosten kommen aus
    # der Matrix, würden hier aber nicht mitgetauscht und zeigten weiter die Summe von
    # vorhin. Spät importiert, weil routes.import_ seinerseits von hier importiert.
    from routes.import_ import metrics_oob_html
    return HTMLResponse(resp.body + metrics_oob_html(ss).encode("utf-8"))


def crew_oob_html(ss) -> str:
    """Panel UND Kopfzeile als Out-of-band-Fragment, zum Anhängen an fremde Antworten.

    Wird gebraucht, wenn die Planung sich ändert, ohne dass das Panel das Ziel des
    Austauschs ist — etwa nach dem Laden einer neuen Datei: dort tauscht htmx die
    Gruppenansicht, während die Matrix daneben sonst den alten Stand zeigen würde.
    """
    return templates.get_template("partials/crew_matrix.html").render(
        {"S": ss, "crew_oob": True, "crew_panel_oob": True, **crew_ctx(ss)})


def _err(msg: str) -> HTMLResponse:
    return HTMLResponse(f'<p class="error-msg">{msg}</p>')


# ─── Planung anlegen / Zeitachse ─────────────────────────────────────────────

@router.post("/api/crew/range", response_class=HTMLResponse)
async def crew_range(request: Request,
                     date_from: str = Form(...),
                     date_to: str = Form(...)):
    """Legt die Planung an oder verschiebt ihre Zeitachse."""
    ss = get_session(request.session)
    try:
        start, end = parse_day(date_from), parse_day(date_to)
    except (ValueError, TypeError):
        return _err("Bitte Start- und Enddatum angeben.")
    if end < start:
        start, end = end, start
    if (end - start).days + 1 > MAX_DAYS:
        return _err(f"Zeitraum zu lang — höchstens {MAX_DAYS} Tage.")

    if ss.crew is None:
        ss.crew = new_plan(start.isoformat(), end.isoformat())
        _apply_lv_phases(ss)
    else:
        ss.crew.set_range(start.isoformat(), end.isoformat())
    return _panel(request, ss)


def start_plan_for(ss) -> bool:
    """Planung anlegen, ohne dass jemand den Zeitraum eintippt.

    Wird vom Positionsbaum aus gerufen („über Personalplanung"): dort steht der
    Zeitraum nicht zur Verfügung, aber das Tool kennt ihn — aus dem Terminplan der
    LV-Vorbemerkungen, sonst aus der Projektanlage. Gibt False zurück, wenn beides
    fehlt; dann muss der Zeitraum von Hand kommen.
    """
    if ss.crew is not None:
        return True
    bereich = _default_range(ss)
    if not bereich.get("quelle"):
        return False
    ss.crew = new_plan(bereich["from"], bereich["to"])
    _apply_lv_phases(ss)
    return True


def _apply_lv_phases(ss) -> bool:
    """Die im LV gefundenen Phasen übernehmen, auf die Zeitachse beschnitten."""
    plan_lv = _schedule(ss)
    if not plan_lv or ss.crew is None:
        return False
    keys = ss.crew.day_keys()
    if not keys:
        return False
    erster, letzter = keys[0], keys[-1]
    phasen = []
    for ph in plan_lv["phases"]:
        if ph.day_to < erster or ph.day_from > letzter:
            continue
        phasen.append(Phase(ph.name, max(ph.day_from, erster), min(ph.day_to, letzter)))
    if not phasen:
        return False
    ss.crew.phases = phasen
    return True


@router.post("/api/crew/phases/from-lv", response_class=HTMLResponse)
async def crew_phases_from_lv(request: Request):
    """Phasen (neu) aus dem Terminplan des LV übernehmen."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if not _apply_lv_phases(ss):
        return _err("Im Leistungsverzeichnis steht kein verwertbarer Terminplan.")
    return _panel(request, ss)


@router.post("/api/crew/phases/reset", response_class=HTMLResponse)
async def crew_phases_reset(request: Request):
    """Phasen auf den einfachen Vorschlag zurücksetzen (letzte Tage Abbau usw.)."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    ss.crew.phases = default_phases(ss.crew.date_from, ss.crew.date_to)
    return _panel(request, ss)


@router.post("/api/crew/phase", response_class=HTMLResponse)
async def crew_phase(request: Request,
                     index: int = Form(...),
                     name: str = Form(""),
                     day_from: str = Form(""),
                     day_to: str = Form("")):
    """Einen Termin umbenennen oder verschieben."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if not ss.crew.set_phase(index, name or None, day_from, day_to):
        return _err("Termin nicht gefunden.")
    return _panel(request, ss)


@router.post("/api/crew/phase/add", response_class=HTMLResponse)
async def crew_phase_add(request: Request,
                         name: str = Form("Neuer Termin"),
                         day_from: str = Form(...),
                         day_to: str = Form(...)):
    """Eigenen Termin anlegen — Proben, Anlieferung, Umbautag, was auch immer."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if not ss.crew.add_phase(name, day_from, day_to):
        return _err("Termin überlappt einen bestehenden oder ist unvollständig.")
    return _panel(request, ss)


@router.post("/api/crew/phase/remove", response_class=HTMLResponse)
async def crew_phase_remove(request: Request, index: int = Form(...)):
    ss = get_session(request.session)
    if ss.crew is None or not ss.crew.remove_phase(index):
        return _err("Termin nicht gefunden.")
    return _panel(request, ss)


@router.post("/api/crew/reset", response_class=HTMLResponse)
async def crew_reset(request: Request):
    """Verwirft die Planung komplett."""
    ss = get_session(request.session)
    ss.crew = None
    return _panel(request, ss)


# ─── Zeilen ──────────────────────────────────────────────────────────────────

@router.get("/api/crew/resource/search", response_class=HTMLResponse)
async def crew_resource_search(request: Request, q: str = ""):
    """Ressourcensuche für neue Zeilen — Personal zuerst, dann der Rest."""
    needle = (q or "").strip().lower()
    rows = [r for r in _db.load_personal_db()
            if not needle or needle in (r.get("funktion") or "").lower()]
    rows.sort(key=lambda r: (r.get("ressourcenart") != "Personal",
                             (r.get("funktion") or "").lower()))
    return templates.TemplateResponse(request, "partials/crew_resources.html",
                                      {"resources": rows[:40], "q": q})


@router.post("/api/crew/row/add", response_class=HTMLResponse)
async def crew_row_add(request: Request,
                       resource_id: int = Form(...),
                       group_key: str = Form("")):
    """Neue Ressourcenzeile — unter dem gewählten Menüpunkt, wenn einer aktiv ist."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Bitte zuerst den Zeitraum festlegen.")
    res = next((r for r in _db.load_personal_db() if int(r["id"]) == resource_id), None)
    if not res:
        return _err("Ressource nicht gefunden.")
    key = group_key.strip()
    if key not in ss.crew.menu_keys():
        key = ""
    # Wer eine Ressource von Hand hinzufügt, will sie haben — eine frühere Abwahl
    # (gelöschte Zeile) ist damit hinfällig. Ohne das bliebe sie in `dismissed`
    # stehen und der Abgleich mit dem Matching würde sie nach einem Verwerfen der
    # Zeile nicht wieder anlegen.
    if resource_id in ss.crew.dismissed:
        ss.crew.dismissed.remove(resource_id)
    ss.crew.add_row(
        label=res.get("funktion") or f"Ressource {resource_id}",
        resource_id=resource_id,
        group_key=key,
        tagessatz=res.get("tagessatz") or 0,
        eigenkosten=res.get("eigenkosten") or 0,
    )
    return _panel(request, ss)


@router.post("/api/crew/row/{row_id}/move-to", response_class=HTMLResponse)
async def crew_row_move_to(row_id: int, request: Request,
                           group_key: str = Form("")):
    """Zeile einem anderen Menüpunkt zuordnen."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    row = ss.crew.row(row_id)
    if row is None:
        return _err("Zeile nicht gefunden.")
    key = group_key.strip()
    row.group_key = key if key in ss.crew.menu_keys() else ""
    return _panel(request, ss)


@router.post("/api/crew/menu/add", response_class=HTMLResponse)
async def crew_menu_add(request: Request, title: str = Form("")):
    """Eigenen Menüpunkt anlegen — für Personal, das im LV keine eigene Position hat."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Bitte zuerst den Zeitraum festlegen.")
    key = ss.crew.add_custom_menu(title or NEUER_MENUEPUNKT)
    # Den neuen Abschnitt merken: die Vorlage setzt darauf den Fokus, damit man ihn
    # gleich benennen kann, statt ihn erst suchen und doppelklicken zu müssen.
    return _panel(request, ss, fresh_menu=key)


@router.post("/api/crew/menu/positions", response_class=HTMLResponse)
async def crew_menu_positions(request: Request,
                              key: str = Form(...),
                              item_id: str = Form(...),
                              action: str = Form("add")):
    """Standard-Position eines Menüpunkts setzen oder entfernen.

    Damit ist die Position für alle Zeilen unter diesem Menüpunkt vorgemerkt:
    eingetragene Manntage laufen ohne weiteren Klick darauf. Eine Position kann in
    mehreren Menüpunkten stehen — eine Installations-Pauschale ist oft für Licht und
    Ton gleichzeitig zuständig.
    """
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if action == "remove":
        ss.crew.set_menu_pos(key, "")
        return _panel(request, ss)

    # Getrennt prüfen, statt ein sammelndes „nicht gefunden" zu melden: ein zweiter
    # Klick auf dieselbe Position ist keine Fehlbedienung, sondern folgenlos.
    if not item_id:
        return _err("Erst oben eine Position anklicken, dann hier als Standard setzen.")
    if key not in ss.crew.menu_keys():
        return _err("Abschnitt nicht gefunden.")
    if not ss.crew.covers(item_id):
        return _err("Nur Positionen aus dem Leistungsverzeichnis können Standard sein "
                    "— ein eigener Menüpunkt ist nur eine Überschrift.")
    ss.crew.set_menu_pos(key, item_id)
    return _panel(request, ss)


# Die drei Nebenkosten-Arten und wie man ihre Ressourcen im Stamm findet. Zur Auswahl
# gestellt wird in der Leiste nur der Spesensatz — den gibt es je Land. Für Hotel und
# Reisekosten gibt es in der Angebotsphase je genau eine Ressource, die stehen als
# Vorgabe in crew_plan.py. Die Route bleibt trotzdem allgemein: kommt später eine
# zweite dazu, ist das ein Aufklapper in der Vorlage und kein Umbau.
#
# `preis` sagt, ob der Satz der Ressource den Preis der Planung setzt: beim
# Spesensatz ist er der Punkt der Übung. Beim Hotel steht im Stamm 0 €, der Preis
# kommt aus dem Angebot des Hauses — nur wenn dort ein Satz hinterlegt ist, wird er
# übernommen. Reisekosten bleiben ein halber Tagessatz der jeweiligen Ressource, ein
# fester Satz würde das überschreiben.
_KOSTENARTEN = {
    "spesen": {"titel": "Spesensatz",
               "treffer": lambda n: n.startswith("spesensatz"),
               "id": "spesen_id", "name": "spesen_name", "preis": "spesen_satz",
               "leer": "Keine Spesensätze im Stamm gefunden. Sie liegen in Easyjob "
                       "als Arbeitsmittel („Spesensatz Inland“ und so weiter)."},
    "hotel":  {"titel": "Hotelkosten",
               "treffer": lambda n: "hotel" in n,
               "id": "hotel_id", "name": "hotel_name", "preis": "hotel_satz",
               "leer": "Keine Hotel-Ressource im Stamm gefunden — in Easyjob etwa "
                       "„Hotelkosten eigenes Personal“."},
    "reise":  {"titel": "Reisekosten",
               "treffer": lambda n: "reise" in n,
               "id": "rk_id", "name": "rk_name", "preis": "",
               "leer": "Keine Reisekosten-Ressource im Stamm gefunden — in Easyjob "
                       "etwa „Reisekosten Pauschal“."},
}


def _kosten_liste(art: str) -> list[dict]:
    kfg = _KOSTENARTEN[art]
    treffer = [r for r in _db.load_personal_db()
               if kfg["treffer"]((r.get("funktion") or "").lower())]
    treffer.sort(key=lambda r: (r.get("funktion") or "").lower())
    return treffer


# ── Export ──────────────────────────────────────────────────────────────────
# Zwei Varianten aus denselben Daten: die Kundenversion ohne Preise geht als Beilage
# mit der Ausschreibung raus, die Kalkulation bleibt im Haus. Deshalb steckt die
# Variante im Dateinamen — eine falsch verschickte Datei fällt sonst niemandem auf.

def _export_name(projekt: str, variante: str, endung: str) -> str:
    sauber = "".join(ch if ch.isalnum() or ch in " -_" else "_"
                     for ch in (projekt or "Projekt")).strip()
    teil = "Kalkulation" if variante == "kalkulation" else "Besetzung"
    return f"Personalplanung_{sauber[:60]}_{teil}.{endung}"


def _menu_titel(plan, items: dict) -> dict:
    """Abschnitts-Schlüssel → Überschrift, wie sie in der Matrix steht.

    Das Planungsmodell kennt nur die selbst angelegten Titel; wie eine LV-Position
    heißt, steht im LV. ``items`` liefert das — aus der Sitzung während des Imports,
    aus dem gespeicherten Abbild bei einem abgelegten Projekt.
    """
    def feld(quelle, name: str) -> str:
        """Ein Feld holen, egal ob LV-Objekt oder Zeile aus dem Abbild."""
        if quelle is None:
            return ""
        if isinstance(quelle, dict):
            return str(quelle.get(name) or "")
        return str(getattr(quelle, name, "") or "")

    out = {}
    for key in plan.menu_keys():
        if plan.is_custom(key):
            out[key] = plan.menu_title(key)
            continue
        it = items.get(key)
        out[key] = (f"{feld(it, 'oz')} {feld(it, 'description').strip()}".strip()
                    or key)
    return out


def _projekt_planung(projekt_id: int):
    """Planung, Projektname und Positionsbeschriftungen eines abgelegten Projekts.

    Der Export soll auch dann noch gehen, wenn der Import längst vorbei ist — beim
    Nachreichen einer Beilage etwa. Die Beschriftungen kommen aus dem lokalen Abbild
    der Buchungen; das LV selbst liegt dann nicht mehr in der Sitzung.
    """
    roh = _db.load_crew_plan(projekt_id)
    if not roh:
        return None, "", {}
    proj = _db.get_project(projekt_id) or {}
    items = {}
    for b in _db.get_project_bookings(projekt_id):
        iid = b.get("item_id")
        if iid and iid not in items:
            items[iid] = {"oz": b.get("oz") or "",
                          "description": b.get("description") or ""}
    return CrewPlan.from_dict(roh), proj.get("name") or "", items


@router.get("/api/crew/export/{art}")
async def crew_export(request: Request, art: str, variante: str = "kalkulation",
                      projekt_id: int = 0):
    """Die Planung als PDF oder Excel.

    Ohne ``projekt_id`` die der laufenden Sitzung, mit die eines abgelegten Projekts —
    damit sich eine Beilage auch nachreichen lässt, wenn der Import vorbei ist.
    """
    if art not in ("pdf", "xlsx"):
        return PlainTextResponse("Unbekanntes Format.", status_code=400)
    if projekt_id:
        plan, projekt, items = _projekt_planung(projekt_id)
        if plan is None:
            return PlainTextResponse("Für dieses Projekt gibt es keine "
                                     "Personalplanung.", status_code=404)
    else:
        ss = get_session(request.session)
        plan = ss.crew
        if plan is None:
            return PlainTextResponse("Keine Personalplanung angelegt.",
                                     status_code=404)
        projekt = (ss.d83_project.name if ss.d83_project else "") or ss.d83_name or ""
        items = _lv_items(ss)
    kalk = variante != "kunde"
    titel = _menu_titel(plan, items)
    try:
        if art == "pdf":
            from crew_pdf import build_pdf
            daten = build_pdf(plan, projekt, kalkulation=kalk, menu_titel=titel)
            typ = "application/pdf"
        else:
            from crew_xlsx import build_xlsx
            daten = build_xlsx(plan, projekt, kalkulation=kalk, menu_titel=titel)
            typ = ("application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet")
    except ImportError as e:
        # reportlab fehlt in der Umgebung — als Klartext melden statt mit einem
        # Serverfehler, sonst sucht das jemand im falschen Modul.
        return PlainTextResponse(f"Export nicht möglich: {e}", status_code=500)
    name = _export_name(projekt, variante, art)
    return Response(daten, media_type=typ, headers={
        "Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/crew/kosten/list", response_class=HTMLResponse)
async def crew_kosten_list(request: Request, art: str = "spesen"):
    """Die wählbaren Ressourcen einer Nebenkosten-Art aus dem Stamm."""
    if art not in _KOSTENARTEN:
        return _err("Unbekannte Kostenart.")
    ss = get_session(request.session)
    kfg = _KOSTENARTEN[art]
    aktiv = getattr(ss.crew, kfg["id"], 0) if ss.crew else 0
    return templates.TemplateResponse(
        request, "partials/crew_spesen.html",
        {"saetze": _kosten_liste(art), "art": art, "aktiv": aktiv,
         "mit_satz": bool(kfg["preis"]), "leer_text": kfg["leer"]})


@router.post("/api/crew/kosten", response_class=HTMLResponse)
async def crew_kosten(request: Request, art: str = Form("spesen"),
                      resource_id: int = Form(...)):
    """Ressource für Spesen, Hotel oder Reisekosten wählen. Gilt für alle Zeilen:
    das hängt am Projekt, nicht an der Person."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if art not in _KOSTENARTEN:
        return _err("Unbekannte Kostenart.")
    res = next((r for r in _db.load_personal_db() if int(r["id"]) == resource_id), None)
    if not res:
        return _err("Ressource nicht gefunden.")
    kfg = _KOSTENARTEN[art]
    setattr(ss.crew, kfg["id"], int(res["id"]))
    setattr(ss.crew, kfg["name"], res.get("funktion") or "")
    satz = float(res.get("tagessatz") or 0)
    # Nur übernehmen, was auch dasteht: die Hotel-Ressource führt 0 €, und der
    # eingetippte Preis darf davon nicht überschrieben werden.
    if kfg["preis"] and satz > 0:
        setattr(ss.crew, kfg["preis"], satz)
    return _panel(request, ss)


@router.post("/api/crew/hotelsatz", response_class=HTMLResponse)
async def crew_hotelsatz(request: Request, value: str = Form(...)):
    """Preis je Hotelnacht. Gilt für die ganze Planung — es ist derselbe Ort und
    dieselbe Zeit, also derselbe Preis."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    try:
        ss.crew.hotel_satz = max(0.0, parse_number(value))
    except (ValueError, TypeError):
        return _err("Zahl erwartet.")
    return _panel(request, ss)


@router.post("/api/crew/menu/nk", response_class=HTMLResponse)
async def crew_menu_nk(request: Request,
                       key: str = Form(...), item_id: str = Form(""),
                       action: str = Form("add")):
    """Nebenkosten eines Abschnitts auf eine Position lenken — leeres ``item_id``
    stellt auf anteilig nach Manntagen zurück."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if action == "remove":
        item_id = ""
    elif not item_id:
        return _err("Erst oben eine Position anklicken, dann hier zuweisen.")
    if not ss.crew.set_nk_pos(key, item_id):
        return _err("Abschnitt oder Position nicht gefunden.")
    return _panel(request, ss)


@router.post("/api/crew/menu/rename", response_class=HTMLResponse)
async def crew_menu_rename(request: Request,
                           key: str = Form(...), title: str = Form("")):
    ss = get_session(request.session)
    if ss.crew is None or not ss.crew.rename_menu(key, title):
        return _err("Menüpunkt nicht gefunden.")
    return _panel(request, ss)


@router.post("/api/crew/row/{row_id}/delete", response_class=HTMLResponse)
async def crew_row_delete(row_id: int, request: Request):
    ss = get_session(request.session)
    if ss.crew is None or not ss.crew.remove_row(row_id):
        return _err("Zeile nicht gefunden.")
    return _panel(request, ss)


_TEXT_FIELDS: set[str] = set()
_NUM_FIELDS = {"tagessatz": float, "eigenkosten": float,
               "hotel_naechte": int, "hotel_satz": float,
               "rk_anzahl": int, "rk_satz": float}


@router.post("/api/crew/row/{row_id}/field")
async def crew_row_field(row_id: int, request: Request,
                         field: str = Form(...), value: str = Form("")):
    """Ein Feld einer Zeile ändern. Antwort ist JSON mit den neuen Summen —
    die Matrix wird nicht neu gebaut, damit der Eingabefokus nicht springt."""
    ss = get_session(request.session)
    if ss.crew is None:
        return JSONResponse({"ok": False, "error": "Keine Planung"}, status_code=400)
    row = ss.crew.row(row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "Zeile nicht gefunden"}, status_code=404)

    if field in _TEXT_FIELDS:
        setattr(row, field, value.strip()[:120])
    elif field in _NUM_FIELDS:
        cast = _NUM_FIELDS[field]
        try:
            zahl = parse_number(value)
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "Zahl erwartet"}, status_code=400)
        setattr(row, field, max(cast(0), cast(zahl)))
    else:
        return JSONResponse({"ok": False, "error": "Unbekanntes Feld"}, status_code=400)

    # Den übernommenen Wert zurückgeben und im Feld anzeigen: wer „520.50" tippt, soll
    # sehen, dass daraus 520,5 wurde — und nicht rätseln, ob 52.050 gespeichert ist.
    gespeichert = getattr(row, field)
    return JSONResponse({
        "ok": True,
        "field": field,
        "value": (format_number(gespeichert) if field in _NUM_FIELDS
                  else gespeichert),
        **_row_payload(ss.crew, row),
    })


# ─── Zellen ──────────────────────────────────────────────────────────────────

@router.post("/api/crew/cell")
async def crew_cell(request: Request,
                    row_id: int = Form(...),
                    day: str = Form(...),
                    persons: int = Form(0),
                    assign_to: str = Form("")):
    """Eine Zelle setzen. Antwort ist JSON: Zeilensumme, Tagessumme, Gesamtwerte —
    genug, um die Anzeige zu aktualisieren, ohne die Matrix neu zu rendern."""
    ss = get_session(request.session)
    if ss.crew is None:
        return JSONResponse({"ok": False, "error": "Keine Planung"}, status_code=400)
    try:
        parse_day(day)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Datum ungültig"}, status_code=400)
    if not ss.crew.set_cell(row_id, day, persons):
        return JSONResponse({"ok": False, "error": "Zeile nicht gefunden"}, status_code=404)

    # Ist unten eine Position gewählt, gilt sie als Kontext: eingetragene Manntage
    # laufen sofort auf sie. Ohne das müsste man jede Zahl zweimal anfassen — erst
    # tippen, dann zuordnen.
    # Reihenfolge: eine oben gewählte Position schlägt alles. Ohne Auswahl greift die
    # Standard-Position des Menüpunkts, unter dem die Zeile steht — dafür sind die
    # Standards da, sonst müsste man vor jedem Tippen erst einen Chip anklicken.
    row0 = ss.crew.row(row_id)
    ziel = assign_to if (assign_to and ss.crew.covers(assign_to)) \
        else (ss.crew.default_pos_for(row0) if row0 else "")
    zugeordnet = ""
    if ziel and persons and ss.crew.assign_days(row_id, day, day, ziel):
        zugeordnet = ziel

    row = ss.crew.row(row_id)
    day_totals = ss.crew.day_totals()
    return JSONResponse({
        "ok": True,
        "persons": row.cells.get(day, 0),
        **_row_payload(ss.crew, row),
        "day": day,
        "assigned": zugeordnet,
        "day_total": day_totals.get(day, 0),
        "peak": max(day_totals.values(), default=0),
    })


@router.post("/api/crew/rows/field", response_class=HTMLResponse)
async def crew_rows_field(request: Request,
                          row_ids: str = Form(...),
                          field: str = Form(...),
                          value: str = Form("")):
    """Dasselbe Feld für mehrere Zeilen setzen — Hotelnächte, Reisen, Tagessatz.

    „Die ganze Crew übernachtet fünf Nächte" ist ein Handgriff und nicht acht.
    Antwort ist das Panel: mit den Nächten ändern sich Spesen, Summen und die
    Verteilung auf die Positionen in allen betroffenen Zeilen gleichzeitig.
    """
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if field not in _NUM_FIELDS:
        return _err("Dieses Feld lässt sich nicht für mehrere Zeilen setzen.")
    ids = [int(x) for x in row_ids.split(",") if x.strip().lstrip("-").isdigit()]
    if not ids:
        return _err("Keine Zeile ausgewählt.")
    try:
        zahl = parse_number(value)
    except (ValueError, TypeError):
        return _err("Zahl erwartet.")
    cast = _NUM_FIELDS[field]
    wert = max(cast(0), cast(zahl))
    for rid in ids:
        row = ss.crew.row(rid)
        if row is not None:
            setattr(row, field, wert)
    return _panel(request, ss)


@router.post("/api/crew/cells/fill", response_class=HTMLResponse)
async def crew_cells_fill(request: Request,
                          row_ids: str = Form(...),
                          day_from: str = Form(...),
                          day_to: str = Form(...),
                          persons: int = Form(0),
                          assign_to: str = Form("")):
    """Mehrere Zellen auf denselben Wert setzen (Auswahlrechteck in der Matrix).

    Antwort ist das ganze Panel: nach einem Bereichsfüllen ändern sich Band,
    Positionsliste und Summen auf einmal, und der Eingabefokus ist hier — anders als
    beim Tippen einzelner Zahlen — kein Verlust, weil der Handgriff abgeschlossen ist.
    """
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    try:
        parse_day(day_from), parse_day(day_to)
    except (ValueError, TypeError):
        return _err("Datum ungültig.")
    ids = [int(x) for x in row_ids.split(",") if x.strip().lstrip("-").isdigit()]
    if not ids:
        return _err("Keine Zeile ausgewählt.")
    # None heißt: jede Zeile bekommt ihre eigene Standardposition. Nur eine oben
    # ausdrücklich gewählte Position gilt für alle.
    ziel = assign_to if (assign_to and ss.crew.covers(assign_to)) else None
    n = ss.crew.fill_cells(ids, day_from, day_to, persons, ziel)
    logging.info("crew: %d Zellen gefüllt (%s Personen, Ziel %s)", n, persons, ziel or "—")
    return _panel(request, ss)


def _row_payload(plan: CrewPlan, row) -> dict:
    return {
        "row_id":     row.id,
        "row_mt":     plan.manntage(row),
        "row_total":  round(plan.row_total(row), 2),
        "row_spesen": round(plan.row_spesen(row), 2),
        "row_hotel":  round(plan.row_hotel(row), 2),
        "row_rk":     round(plan.row_rk(row), 2),
        "totals":     plan.totals(),
    }


# ─── Panel neu laden ─────────────────────────────────────────────────────────

@router.get("/api/crew/panel", response_class=HTMLResponse)
async def crew_panel(request: Request):
    return _panel(request, get_session(request.session))


# ─── Positions-Zuordnung (Stufe 2) ───────────────────────────────────────────

@router.post("/api/crew/assign", response_class=HTMLResponse)
async def crew_assign(request: Request,
                      row_id: int = Form(...),
                      day_from: str = Form(...),
                      day_to: str = Form(""),
                      item_id: str = Form("")):
    """Tagesbereich einer Zeile einer LV-Position zuordnen. Leeres ``item_id`` löst.

    Antwort ist das ganze Panel: mit der Zuordnung ändern sich Band, Positionsliste
    und die Liste der offenen Tage gleichzeitig — die einzeln nachzuführen wäre mehr
    Zustand im Browser als Nutzen.
    """
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if item_id and not ss.crew.covers(item_id):
        return _err("Diese Position gehört nicht zur Planung.")
    ss.crew.assign_days(row_id, day_from, day_to or day_from, item_id)
    return _panel(request, ss)


@router.post("/api/crew/assign-range", response_class=HTMLResponse)
async def crew_assign_range(request: Request,
                            row_ids: str = Form(...),
                            day_from: str = Form(...),
                            day_to: str = Form(...),
                            item_id: str = Form("")):
    """Ausgewählte Felder einer Position zuordnen (oder mit leerem ``item_id`` lösen).

    Das ist der Weg, eine bestehende Besetzung umzuhängen: Felder markieren, Position
    anklicken. Früher gab es dafür einen Bandstreifen unter jeder Zeile, den man
    überstrichen hat — der hat die Matrix doppelt so hoch gemacht.
    """
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if item_id and not ss.crew.covers(item_id):
        return _err("Diese Position gehört nicht zur Planung.")
    try:
        parse_day(day_from), parse_day(day_to)
    except (ValueError, TypeError):
        return _err("Datum ungültig.")
    ids = [int(x) for x in row_ids.split(",") if x.strip().lstrip("-").isdigit()]
    if not ids:
        return _err("Keine Zeile ausgewählt.")
    for rid in ids:
        ss.crew.assign_days(rid, day_from, day_to, item_id)
    return _panel(request, ss)


@router.post("/api/crew/assign-open", response_class=HTMLResponse)
async def crew_assign_open(request: Request,
                           item_id: str = Form(...),
                           row_id: int = Form(0)):
    """Alle offenen Tage einer Position zuschlagen — der Nachzieh-Knopf.

    Gedacht für die übliche Reihenfolge: erst die Besetzung tippen, die Zuordnung
    später. Ohne das müsste man vor der ersten Zahl wissen, auf welche Position sie
    läuft.
    """
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if not item_id:
        return _err("Erst oben eine Position anklicken, dann die offenen Tage zuordnen.")
    if not ss.crew.covers(item_id):
        return _err("Diese Position gehört nicht zur Planung.")
    n = ss.crew.assign_open(item_id, row_id)
    logging.info("crew: %d offene Tage auf %s gelegt", n, item_id)
    return _panel(request, ss)


@router.post("/api/crew/fill-phase", response_class=HTMLResponse)
async def crew_fill_phase(request: Request,
                          index: int = Form(...),
                          item_id: str = Form(...),
                          row_id: int = Form(0)):
    """Alle besetzten, noch offenen Tage einer Phase auf eine Position legen.
    ``row_id=0`` nimmt alle Zeilen — das ist der Abkürzung-Knopf für „Aufbau geht
    komplett auf 03.03.01"."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if not ss.crew.covers(item_id):
        return _err("Diese Position gehört nicht zur Planung.")
    rows = [r.id for r in ss.crew.rows] if not row_id else [row_id]
    n = sum(ss.crew.fill_phase(rid, index, item_id) for rid in rows)
    logging.info("crew: Phase %s → %s (%d Tage)", index, item_id, n)
    return _panel(request, ss)


@router.post("/api/crew/pos/add", response_class=HTMLResponse)
async def crew_pos_add(request: Request, item_id: str = Form(...)):
    """LV-Position in die Planung aufnehmen — von hier oder aus dem Positionsbaum
    des Imports („Personal über Personalplanung")."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Bitte zuerst den Zeitraum der Personalplanung festlegen.")
    if item_id not in _lv_items(ss):
        return _err("Position nicht gefunden.")
    ss.crew.add_position(item_id, manual=True)
    return _panel(request, ss)


@router.post("/api/crew/pos/remove", response_class=HTMLResponse)
async def crew_pos_remove(request: Request, item_id: str = Form(...)):
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    n = ss.crew.remove_position(item_id)
    if n:
        logging.info("crew: Position %s entfernt, %d Tage gelöst", item_id, n)
    return _panel(request, ss)


@router.post("/api/crew/pos/mode", response_class=HTMLResponse)
async def crew_pos_mode(request: Request,
                        item_id: str = Form(...),
                        mode: str = Form(...)):
    """Menüpunkt oder Sammelposition. Wirkt auf die Gliederung der Positionsliste
    und später auf die Easyjob-Buchung: ein Menüpunkt bekommt seine eigene Gruppe,
    eine Sammelposition läuft in die Gruppe ihres Menüpunkts."""
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    if mode not in ("menu", "batch"):
        return _err("Unbekannter Modus.")
    ss.crew.set_pos_mode(item_id, mode)
    return _panel(request, ss)


@router.post("/api/crew/pos/move", response_class=HTMLResponse)
async def crew_pos_move(request: Request,
                        item_id: str = Form(...),
                        delta: int = Form(...)):
    ss = get_session(request.session)
    if ss.crew is None:
        return _err("Keine Planung.")
    ss.crew.move_position(item_id, max(-1, min(1, delta)))
    return _panel(request, ss)
