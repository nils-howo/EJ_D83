"""Projektliste + D84-Export + Projektübersicht aus gespeicherten Projekten."""
import asyncio
import json
import logging
import os
import pathlib
import re
import threading
import xml.etree.ElementTree as ET

import pyodbc
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

import db as _db
import excel_export as _xlout
import excel_parser as _xl
from state import get_session, templates
from routes.import_ import _clean_gaeb_for_export, _import_gaeb_groups

router = APIRouter()


def _fmt_qty(q: float) -> str:
    """Menge für GAEB-<Qty> formatieren: bis zu 3 Nachkommastellen, ohne
    überflüssige Nullen (2.0 → „2", 2.5 → „2.5")."""
    s = f"{q:.3f}".rstrip("0").rstrip(".")
    return s or "0"


# Geparste GAEB-Gruppenstruktur je Projekt cachen — sie ist unveränderlich, das
# erneute Parsen kostet sonst je Übersichtsaufruf ~130 ms. Die Struktur enthält
# eingebettete base64-Bilder, daher die Anzahl begrenzen (FIFO), sonst wächst der
# Speicher unbegrenzt.
_OVERVIEW_GROUPS_CACHE: "dict[int, dict]" = {}
_OVERVIEW_CACHE_MAX = 12
_OVERVIEW_CACHE_LOCK = threading.Lock()


def _excel_scenarios(project_id: int) -> list[str]:
    """Auswählbare Szenarien eines Excel-Projekts.

    Fragt bewusst ``excel_parser.scenario_names`` — dieselbe Aufzählung, die
    ``parse_excel`` für ``scenario_by_item`` benutzt. Eine eigene Regel hier führte
    dazu, dass Blätter mit nur einer Mengenspalte nie auswählbar waren und beim
    Export komplett ohne Preise blieben.

    Wirft weiter, wenn das Layout fehlt — der Aufrufer muss das sehen, denn eine
    leere Liste bedeutet „nur ein Szenario, keine Auswahl nötig".
    """
    layout = _project_excel_layout(project_id)
    return _xl.scenario_names(layout) if layout else []


def _source_kind(project_id: int) -> str:
    """"gaeb" oder "excel" — bestimmt, wie die gespeicherte Quelldatei gelesen und
    wie das Angebot zurückgeschrieben wird."""
    proj = _db.get_project(project_id)
    return (proj or {}).get("source_kind") or "gaeb"


def _project_excel_layout(project_id: int):
    """Die Spalten-Zuordnung, mit der DIESES Projekt eingelesen wurde — oder None.

    Bewusst kein Rückgriff auf das globale ``excel_layouts``-Profil: das ist nach
    Fingerprint geschlüsselt und wird vom nächsten Import derselben Vorlage
    überschrieben. Ein Export würde dann mit der Zuordnung eines fremden Imports
    rechnen, Preise in die falsche Spalte schreiben oder — weil die item_id von der
    Mengenspalte abhängt — überhaupt keine Buchung mehr finden.

    Reihenfolge: Projektspalte (hochgeladene Projekte), dann state_json (Entwürfe).
    """
    with _db.get_conn() as conn:
        row = conn.execute(
            "SELECT source_layout_json, state_json FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
    if not row:
        return None
    for raw, key in ((row["source_layout_json"], None), (row["state_json"], "excel_layout")):
        if not raw:
            continue
        try:
            data = json.loads(raw)
            lay  = data.get(key) if key else data
            if lay:
                return _xl.layout_from_dict(lay)
        except (ValueError, TypeError, AttributeError):
            continue
    return None


def _overview_data(project_id: int) -> dict:
    """Geparste GAEB-Daten eines Projekts (gecacht, größenbegrenzt):
    {"groups": [...], "preliminaries": [...]} — Vorbemerkungen = projektweite
    Beschreibung, Gruppen = Positionsstruktur."""
    cached = _OVERVIEW_GROUPS_CACHE.get(project_id)
    if cached is not None:
        return cached
    gaeb_bytes, _ = _db.get_project_gaeb(project_id)
    groups: list[dict] = []
    preliminaries: list[dict] = []
    if gaeb_bytes:
        try:
            from gaeb_parser import parse_gaeb
            if _source_kind(project_id) == "excel":
                _lay = _project_excel_layout(project_id)
                if _lay is None:
                    raise ValueError("keine Spalten-Zuordnung am Projekt gespeichert")
                gaeb_proj = _xl.parse_excel(gaeb_bytes, _lay)
            else:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".x83", delete=False) as tf:
                    tf.write(gaeb_bytes)
                    tmp = tf.name
                try:
                    gaeb_proj = parse_gaeb(tmp)
                finally:
                    pathlib.Path(tmp).unlink(missing_ok=True)
            groups = _import_gaeb_groups(gaeb_proj, level=0, alt_active={})
            preliminaries = [
                {"title": r.title, "long_text": r.long_text, "images": r.images}
                for r in (gaeb_proj.preliminaries or [])
            ]
        except Exception as _e:
            logging.error("project_overview: Parsen der Quelldatei fehlgeschlagen: %s", _e)
    data = {"groups": groups, "preliminaries": preliminaries}
    # FIFO-Begrenzung: ältesten Eintrag verwerfen, wenn voll. Der Lock schützt die
    # Iteration (next(iter(...))) gegen gleichzeitige Mutationen aus anderen
    # Executor-Threads / project_delete — sonst "dict changed size during iteration".
    with _OVERVIEW_CACHE_LOCK:
        if len(_OVERVIEW_GROUPS_CACHE) >= _OVERVIEW_CACHE_MAX:
            _OVERVIEW_GROUPS_CACHE.pop(next(iter(_OVERVIEW_GROUPS_CACHE)), None)
        _OVERVIEW_GROUPS_CACHE[project_id] = data
    return data


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
        return [], []      # keine XML-Quelle (z.B. Excel) → keine Bieter-Textfelder
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
    with _OVERVIEW_CACHE_LOCK:
        _OVERVIEW_GROUPS_CACHE.pop(project_id, None)
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
        return HTMLResponse('<div class="error-msg">Keine Quelldatei für dieses Projekt.</div>')
    excel = _source_kind(project_id) == "excel"
    # Bieter-Antwortfelder sind ein GAEB-Konstrukt — bei Excel bleibt die Liste leer,
    # der Dialog zeigt dann nur den Export-Knopf.
    ht_labels, single_fields = ([], []) if excel else _extract_bidder_fields(gaeb_bytes)
    # Mehrere Szenarien schreiben in dieselbe Preisspalte — pro Export eines auswählen.
    scenarios = _excel_scenarios(project_id) if excel else []
    return templates.TemplateResponse(request, "partials/export_dialog.html", {
        "project_id":    project_id,
        "ht_labels":     ht_labels,
        "single_fields": single_fields,
        "scenarios":     scenarios,
        "post_url":      f"/api/projects/{project_id}/"
                         + ("export-excel" if excel else "export-d84"),
        "title":         "Excel-Export — Preise zurückschreiben" if excel
                         else "D84-Export — Bieter-Angaben",
        "empty_note":    ("Die Einzelpreise werden in die hochgeladene Excel-Datei "
                          "geschrieben; Formatierung und Formeln bleiben erhalten.") if excel
                         else ("Diese Ausschreibung hat keine Bieter-Antwortfelder. "
                               "Der Export enthält nur die Preise."),
    })


def _learn_from_ej(cur, bookings: list[dict],
                   num_by_ej_id: dict[int, str],
                   res_name_by_id: dict[int, str],
                   name_by_num: dict[str, str]) -> list[dict]:
    """Vergleicht die aktuelle EJ-Buchung je Position mit dem Import-Snapshot und
    lernt in EJ getauschte Artikel/Ressourcen als GUI-Mapping (Kurztext → Artikel
    bzw. Ressource).

    - Nur Positionen, die eindeutig EINER EJ-Gruppe entsprechen (Positions-Modus);
      teilen sich mehrere Positionen eine Gruppe (Gruppen-Modus), wird übersprungen.
    - Bei Artikeln zählen nur EIGENSTÄNDIGE Artikel: Top-Level-Zeilen der Gruppe
      minus alles, was laut Stückliste (StockTypeReference) Referenz eines anderen
      Artikels derselben Gruppe ist (Bolzen/Federstecker etc. fallen raus).

    Gibt die gelernten Änderungen für die Anzeige zurück.
    """
    from collections import Counter

    snap_art: dict[str, set] = {}   # item_id → {Artikelnummer}
    snap_res: dict[str, set] = {}   # item_id → {resource_id}
    kurz:  dict[str, str] = {}
    oz_of: dict[str, str] = {}
    grp_of: dict[str, int] = {}
    for b in bookings:
        iid = b["item_id"]
        kurz.setdefault(iid, (b.get("description") or "").strip())
        oz_of.setdefault(iid, b.get("oz") or "")
        if b.get("ej_group_id"):
            grp_of[iid] = int(b["ej_group_id"])
        if (b.get("kind") or "article") == "resource":
            rid = int(b.get("ej_stock_type_id") or 0)
            if rid:
                snap_res.setdefault(iid, set()).add(rid)
        elif b.get("art_num"):
            snap_art.setdefault(iid, set()).add(str(b["art_num"]))

    grp_count = Counter(grp_of.values())
    positions = [(iid, g) for iid, g in grp_of.items()
                 if grp_count[g] == 1 and kurz.get(iid)]   # nur eindeutige Gruppen
    if not positions:
        return []
    groups = list({g for _, g in positions})
    gph = ",".join("?" for _ in groups)

    # aktuelle Top-Level-Artikel je Gruppe (gebundene Kind-Zeilen ausgeschlossen)
    grp_arts: dict[int, list] = {}
    cur.execute(
        f"""SELECT IdStockType2JobGroup, IdStockType FROM StockType2Job
            WHERE IdStockType2JobGroup IN ({gph})
              AND (IdStockType2Job_Parent IS NULL OR IdStockType2Job_Parent = 0)""",
        *groups,
    )
    for r in cur.fetchall():
        grp_arts.setdefault(int(r[0]), []).append(int(r[1] or 0))

    # Stückliste: NUR NICHT-optionale Kind-Artikel je Elternartikel (für den Filter).
    # Maßgeblich ist StockTypeReference.IsOptional — NICHT der ReferenceType:
    #   IsOptional = 0/NULL → Zubehör, das beim Buchen des Elternartikels automatisch
    #     mitkommt (Bolzen, Federstecker, Y-Case, Netzkabel, Batterie …) → NIE lernen,
    #     sonst wird beim nächsten Import doppelt gebucht.
    #   IsOptional = 1 → echte Wahlmöglichkeit (z.B. ETC-Tubus), kommt NICHT automatisch
    #     → SOLL gelernt werden, wenn gebucht → hier NICHT herausfiltern.
    # (Der ReferenceType 1/3 taugt nicht: Bolzen sind z.B. Typ 3, aber IsOptional=0.)
    all_arts = {a for arts in grp_arts.values() for a in arts if a}
    parent_children: dict[int, set] = {}
    if all_arts:
        aph = ",".join("?" for _ in all_arts)
        cur.execute(
            f"""SELECT IdStockType_Parent, IdStockType FROM StockTypeReference
                WHERE IdStockType_Parent IN ({aph})
                  AND (IsOptional = 0 OR IsOptional IS NULL)""",
            *all_arts,
        )
        for r in cur.fetchall():
            parent_children.setdefault(int(r[0]), set()).add(int(r[1]))

    # aktuelle Ressourcen je Gruppe
    grp_res: dict[int, set] = {}
    cur.execute(
        f"""SELECT IdStockType2JobGroup, IdResourceFunction FROM ResourceFunctionAllocation
            WHERE IdStockType2JobGroup IN ({gph})""",
        *groups,
    )
    for r in cur.fetchall():
        grp_res.setdefault(int(r[0]), set()).add(int(r[1] or 0))

    def _art_names(nums):
        return [name_by_num.get(n, n) for n in sorted(nums)]

    def _res_names(ids):
        return [res_name_by_id.get(r, str(r)) for r in sorted(ids)]

    learned: list[dict] = []
    for iid, g in positions:
        kt   = kurz[iid]
        arts = grp_arts.get(g, [])
        in_grp = set(arts)
        orig_nums = snap_art.get(iid, set())
        # Nicht-optionale Referenz = Kind eines anderen Artikels DERSELBEN Gruppe (steht
        # in parent_children). Nur diese fallen raus (Zubehör, wird automatisch mitgebucht).
        # Optionale Referenzen (IsOptional=1, z.B. ETC-Tubus) stehen NICHT in
        # parent_children und bleiben als eigenständige, lernbare Artikel erhalten.
        referenced = {c for p in in_grp for c in parent_children.get(p, set()) if c in in_grp}
        standalone = [a for a in arts if a and a not in referenced]
        cur_nums  = {num_by_ej_id[a] for a in standalone if a in num_by_ej_id}
        # Artikel geändert / ergänzt / komplett entfernt → lernen + anzeigen.
        # Leere Liste an save_gui_bundle löscht das Mapping (z.B. Material raus,
        # dafür Personal rein) — sonst würde beim nächsten Import wieder das alte
        # Material gebucht.
        if cur_nums != orig_nums and (cur_nums or orig_nums):
            _db.save_gui_bundle(kt, sorted(cur_nums))
            learned.append({"oz": oz_of.get(iid, ""), "kurztext": kt, "kind": "Artikel",
                            "old": _art_names(orig_nums), "new": _art_names(cur_nums)})

        cur_res  = grp_res.get(g, set())
        orig_res = snap_res.get(iid, set())
        if cur_res != orig_res and (cur_res or orig_res):
            # Ressourcen-Mapping ist 1:1 (der Matcher wendet je Position genau eine
            # Ressource an) — nur die primäre (kleinste ID) wird gelernt. Der Bericht
            # zeigt genau das Gelernte, statt mehr zu versprechen als gespeichert wird.
            primary = sorted(cur_res)[0] if cur_res else 0
            _db.save_gui_resource_mapping(kt, primary)
            learned.append({"oz": oz_of.get(iid, ""), "kurztext": kt, "kind": "Ressource",
                            "old": _res_names(orig_res),
                            "new": _res_names({primary} if primary else set())})
    return learned


def _export_costs(ss, project_id: int, proj: dict) -> dict:
    """Buchungen, Gruppenkosten und EJ-Learning — gemeinsame Basis für den
    GAEB-X84- und den Excel-Export. Verbatim aus project_export_d84 gehoben."""
    # Kosten werden ausschließlich über die beim Import angelegten Jobs aggregiert,
    # nicht über das ganze Projekt — sonst würden im Bestehend-Projekt-Modus fremde
    # Jobs mitgerechnet.
    ej_project_id = int(proj.get("ej_project_id") or 0)
    job_ids = [int(x) for x in (proj.get("ej_job_ids") or "").split(",") if x.strip().isdigit()]
    if not job_ids:
        raise HTTPException(400, "Projekt hat keine gespeicherten Job-IDs — bitte neu importieren.")
    bookings = _db.get_project_bookings(project_id)

    # item_id → ej_group_id (IdStockType2JobGroup)
    group_by_item: dict[str, int] = {
        b["item_id"]: int(b["ej_group_id"])
        for b in bookings
        if b.get("ej_group_id")
    }

    # item_id → bestimmte (gebuchte) Menge — erste Buchung je Position (= Haupt-Match).
    # Für offene Ausschreibungspositionen (Menge 0, z.B. Spesen/Hotel/Personaltage)
    # wird diese selbst festgelegte Menge in die D84 (<Qty>) übernommen, statt der 0.
    qty_by_item: dict[str, float] = {}
    for b in bookings:
        iid = b.get("item_id")
        if iid and iid not in qty_by_item:
            try:
                qty_by_item[iid] = float(b.get("qty") or 0)
            except (TypeError, ValueError):
                pass

    # EJ-DB: Gruppenkosten lesen
    # Artikel-Kosten pro Gruppe: SUM(Anzahl × Preis) aller Artikel in der Gruppe
    # Personal-Kosten pro Gruppe: SUM(TotalPrice) aller Ressourcen in der Gruppe
    group_art_cost:  dict[int, float] = {}
    group_pers_cost: dict[int, float] = {}

    cn = pyodbc.connect(ss.ej_db_conn)
    try:
        cur = cn.cursor()

        # Aggregiert wird über alle Jobs des EJ-Projekts, nicht über die beim Import
        # angelegte Job-Liste. Grund: Gruppen dürfen in Easyjob nachträglich in einen
        # anderen Job verschoben werden — mit einem Job-Filter wären ihre Kosten dann
        # verloren und der Einheitspreis 0. Fremde Gruppen desselben Projekts landen
        # dabei mit in den Summen; das ist harmlos, solange nur unter den
        # IdStockType2JobGroup aus project_bookings nachgesehen wird (Gruppen-IDs sind
        # EJ-weit eindeutig). ACHTUNG: der Caption-Fallback weiter unten sucht NICHT
        # über IDs, sondern über die Bezeichnung — der braucht eine eigene Eingrenzung.
        cur.execute(
            """
            SELECT s2j.IdStockType2JobGroup,
                   SUM(s2j.Factor * s2j.TimeFactor * COALESCE(s2j.RentalPrice, s2j.BasePrice, 0)) AS GruppenKosten
            FROM StockType2Job s2j
            JOIN Job j ON j.IdJob = s2j.IdJob
            WHERE j.IdProject = ?
              AND s2j.IdStockType2JobGroup IS NOT NULL
              AND s2j.IdStockType2JobGroup > 0
            GROUP BY s2j.IdStockType2JobGroup
            """,
            ej_project_id,
        )
        for r in cur.fetchall():
            group_art_cost[int(r[0])] = float(r[1] or 0)

        cur.execute(
            """
            SELECT rfa.IdStockType2JobGroup,
                   SUM(rfa.TotalPrice) AS PersKosten
            FROM ResourceFunctionAllocation rfa
            JOIN Job j ON j.IdJob = rfa.IdJob
            WHERE j.IdProject = ?
              AND rfa.IdStockType2JobGroup IS NOT NULL
              AND rfa.IdStockType2JobGroup > 0
            GROUP BY rfa.IdStockType2JobGroup
            """,
            ej_project_id,
        )
        for r in cur.fetchall():
            group_pers_cost[int(r[0])] = float(r[1] or 0)

        # Gruppenbezeichnungen: "[01.01.01] Beschreibung" → OZ → IdStockType2JobGroup
        # Notnagel für Positionen ohne Buchung in project_bookings. Bricht, sobald
        # jemand die Gruppe in Easyjob umbenennt — der Hauptweg über die gespeicherte
        # Gruppen-ID ist davon unabhängig.
        #
        # Eingegrenzt auf die Jobs DIESES Imports plus die Jobs, in denen unsere
        # eigenen Gruppen inzwischen liegen (falls jemand sie verschoben hat). Ohne
        # das gewinnt bei gleicher Bezeichnung — etwa nach einem zweiten Import in
        # dasselbe EJ-Projekt — irgendeine fremde Gruppe, und zwar in unbestimmter
        # Reihenfolge: der Einheitspreis wäre plausibel, aber falsch, und zwischen
        # zwei Exporten desselben Projekts unterschiedlich.
        eigene_gids = {int(b["ej_group_id"]) for b in bookings if b.get("ej_group_id")}
        erlaubte_jobs = set(job_ids)
        if eigene_gids:
            gid_list = sorted(eigene_gids)
            for i in range(0, len(gid_list), 900):     # SQL Server: max. 2100 Parameter
                teil = gid_list[i:i + 900]
                ph   = ",".join("?" for _ in teil)
                cur.execute(
                    f"SELECT DISTINCT IdJob FROM StockType2JobGroup "
                    f"WHERE IdStockType2JobGroup IN ({ph})", *teil)
                erlaubte_jobs |= {int(r[0]) for r in cur.fetchall() if r[0]}

        cur.execute(
            """
            SELECT g.Caption, g.IdStockType2JobGroup, g.IdJob
            FROM StockType2JobGroup g
            JOIN Job j ON j.IdJob = g.IdJob
            WHERE j.IdProject = ?
            """,
            ej_project_id,
        )
        oz_to_group: dict[str, int] = {}
        for r in cur.fetchall():
            if erlaubte_jobs and int(r[2] or 0) not in erlaubte_jobs:
                continue                              # fremder Job desselben Projekts
            cap = r[0] or ""
            if cap.startswith('[') and ']' in cap:
                oz = cap[1:cap.index(']')].strip()
                if oz:
                    oz_to_group[oz] = int(r[1])

        # ── Learning: in EJ getauschte Artikel/Ressourcen als Mapping übernehmen ──
        _articles = _db.load_articles_db()
        num_by_ej_id = {int(a["ej_id"]): a["nummer"] for a in _articles if a.get("ej_id")}
        name_by_num  = {a["nummer"]: (a.get("bezeichnung") or a["nummer"])
                        for a in _articles if a.get("nummer")}
        res_name_by_id = {
            int(p["id"]): (p.get("funktion") or p.get("satzname") or str(p["id"]))
            for p in _db.load_personal_db() if p.get("id")
        }
        try:
            learned = _learn_from_ej(cur, bookings, num_by_ej_id, res_name_by_id, name_by_num)
        except Exception as _le:
            logging.error("Export-Learning fehlgeschlagen: %s", _le)
            learned = []
    except pyodbc.Error as _dbe:
        logging.error("Export: EJ-Kostenabfrage fehlgeschlagen: %s", _dbe)
        raise HTTPException(
            status_code=502,
            detail="EJ-Datenbank nicht erreichbar — Kostenabfrage fehlgeschlagen, Export abgebrochen.",
        )
    finally:
        cn.close()

    # Im Modus "positions" ist jede EJ-Gruppe genau eine LV-Position (1:1) — dann ist
    # der EP schlicht Gruppenkosten ÷ Menge. Im Modus "groups" teilen sich mehrere
    # Positionen eine Gruppe; dann muss die Gruppensumme auf sie verteilt werden,
    # sonst bekäme jede Position die ganze Gruppe und die Angebotssumme wäre um den
    # Faktor "Positionen je Gruppe" zu hoch.
    items_per_group: dict[int, list[str]] = {}
    item_cost:       dict[str, float]     = {}
    for b in bookings:
        gid = int(b.get("ej_group_id") or 0)
        iid = b.get("item_id") or ""
        if not iid:
            continue
        if gid:
            sib = items_per_group.setdefault(gid, [])
            if iid not in sib:
                sib.append(iid)
        try:
            item_cost[iid] = item_cost.get(iid, 0.0) +                 float(b.get("qty") or 0) * float(b.get("unit_price") or 0)
        except (TypeError, ValueError):
            pass
    shared = sum(1 for v in items_per_group.values() if len(v) > 1)
    if shared:
        logging.info("export: %d EJ-Gruppen enthalten mehrere Positionen — "
                     "Gruppenkosten werden anteilig verteilt", shared)

    return {
        "job_ids": job_ids, "bookings": bookings,
        "group_by_item": group_by_item, "qty_by_item": qty_by_item,
        "group_art_cost": group_art_cost, "group_pers_cost": group_pers_cost,
        "oz_to_group": oz_to_group, "learned": learned,
        "items_per_group": items_per_group, "item_cost": item_cost,
        "shared_groups": shared,
        # Der Artikelstamm ist hier schon geladen (11.507 Zeilen, ~95 ms und ~12 MB).
        # Weiterreichen, damit der D84-Export ihn nicht ein zweites Mal holt.
        "articles": _articles,
    }


def _shared_group_note(costs: dict) -> list[str]:
    """Hinweis, wenn sich Positionen eine EJ-Gruppe teilen (Import-Modus „Gruppen")."""
    n = costs.get("shared_groups") or 0
    if not n:
        return []
    return [f"{n} Easyjob-Gruppe(n) enthalten mehrere LV-Positionen — die Gruppenkosten "
            f"wurden anteilig auf sie verteilt, gewichtet nach den gebuchten Kosten. "
            f"Positionsgenaue Preise gibt es nur, wenn im Modus 'Positionen' "
            f"importiert wurde (dann ist jede Gruppe genau eine Position)."]


def _export_ep(costs: dict, item_id: str, oz: str, qty: float) -> float:
    """Einheitspreis einer Position: Kosten ihrer EJ-Gruppe (Artikel + Personal)
    ÷ LV-Menge. Ohne Gruppe 0 — dann steht kein Preis im Angebot.

    Enthält die Gruppe mehrere Positionen (Import-Modus „Gruppen"), wird die
    Gruppensumme anteilig verteilt — gewichtet nach den beim Import gebuchten Kosten
    der jeweiligen Position. So bleibt die Angebotssumme gleich der Gruppensumme.
    Bei 1:1 (Modus „Positionen") ist der Anteil 1 und die Rechnung unverändert.
    """
    grp_id = costs["group_by_item"].get(item_id, 0) or costs["oz_to_group"].get(oz, 0)
    if not grp_id:
        return 0.0
    total = (costs["group_art_cost"].get(grp_id, 0.0)
             + costs["group_pers_cost"].get(grp_id, 0.0))

    siblings = costs.get("items_per_group", {}).get(grp_id) or []
    if len(siblings) > 1:
        weights = {i: costs.get("item_cost", {}).get(i, 0.0) for i in siblings}
        wsum    = sum(weights.values())
        # Ohne belastbare Gewichte (alle Preise 0) gleichmäßig aufteilen
        share   = (weights.get(item_id, 0.0) / wsum) if wsum > 0 else 1.0 / len(siblings)
        total  *= share

    return round(total / qty, 3) if qty else 0.0

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

    if (proj.get("source_kind") or "gaeb") != "gaeb":
        # Sonst landen die xlsx-ZIP-Bytes in ET.fromstring und die Route stirbt mit
        # einem ParseError-500 — nachdem _export_costs schon Mappings gelernt hat.
        raise HTTPException(400, "Dieses Projekt stammt aus einer Excel-Datei — "
                                 "bitte den Excel-Export benutzen.")

    gaeb_bytes, gaeb_name = _db.get_project_gaeb(project_id)
    if not gaeb_bytes:
        raise HTTPException(404, "Keine GAEB-Datei für dieses Projekt gespeichert")

    ej_project_id = proj.get("ej_project_id") or 0
    if not ej_project_id:
        raise HTTPException(400, "Projekt hat keine EasyJob-Projekt-ID")

    # Gemeinsame Kostenbasis (Buchungen, EJ-Gruppenkosten, Learning)
    costs           = _export_costs(ss, project_id, proj)
    job_ids         = costs["job_ids"]
    bookings        = costs["bookings"]
    group_by_item   = costs["group_by_item"]
    qty_by_item     = costs["qty_by_item"]
    group_art_cost  = costs["group_art_cost"]
    group_pers_cost = costs["group_pers_cost"]
    oz_to_group     = costs["oz_to_group"]
    learned         = costs["learned"]

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

        qty_el    = item_el.find(tag("Qty"))
        qtytbd_el = item_el.find(tag("QtyTBD"))
        qty_txt   = qty_el.text.strip() if (qty_el is not None and qty_el.text) else ""
        qty_val   = float(qty_txt.replace(",", ".")) if qty_txt else 0.0

        # Offene Position: entweder <Qty>0 oder GAEB-<QtyTBD> ("Menge vom Bieter
        # anzugeben"). Die bestimmte (gebuchte) Menge übernehmen — sonst blieben EP/GP
        # 0 bzw. der EP falsch. Die Menge wird in die D84 geschrieben; aus <QtyTBD>
        # wird eine feste <Qty>, da die Menge nun festgelegt ist (überlebt das Cleanup).
        if qty_val <= 0:
            booked_q = qty_by_item.get(item_id, 0.0)
            qty_val  = booked_q if booked_q > 0 else 1.0
            if booked_q > 0:
                if qty_el is not None:
                    qty_el.text = _fmt_qty(booked_q)
                elif qtytbd_el is not None:
                    idx = list(item_el).index(qtytbd_el)
                    item_el.remove(qtytbd_el)
                    qty_el = ET.Element(tag("Qty"))
                    qty_el.text = _fmt_qty(booked_q)
                    item_el.insert(idx, qty_el)

        # Gesamtkosten der EJ-Gruppe (Artikel + Personal) ÷ GAEB-Menge = EP.
        # Fallback ohne Buchung: OZ aus der GAEB-Hierarchie → EJ-Gruppenbezeichnung.
        ep = _export_ep(costs, item_id, _item_oz(item_el), qty_val)
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
    art_by_num  = {a["nummer"]: a for a in costs["articles"]}

    def _art_price(art: dict) -> float:
        try:
            return float(art.get("mietpreis") or 0)
        except (TypeError, ValueError):
            return 0.0

    # Je Position den Artikel mit dem größten Kostenblock bestimmen (Menge × Mietpreis) —
    # so zeigt das Bieter-Feld Hersteller/Detail vom kostenmäßig dominierenden Gerät der
    # Gruppe, nicht von einem beliebigen (Zubehör-)Artikel. Zusatz-/Bundle-Artikel zählen mit.
    priciest_art_by_item: dict[str, dict] = {}
    _best_cost_by_item:   dict[str, float] = {}
    for b in bookings:
        if b.get("kind") == "resource":
            continue
        art = art_by_num.get(b.get("art_num"))
        if not art:
            continue
        try:
            qty = float(b.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            qty = 1.0
        cost = _art_price(art) * qty
        iid  = b["item_id"]
        if iid not in _best_cost_by_item or cost > _best_cost_by_item[iid]:
            _best_cost_by_item[iid]   = cost
            priciest_art_by_item[iid] = art

    def _auto_text(item_id: str) -> str:
        art = priciest_art_by_item.get(item_id)
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
    # Datei für den Download zwischenspeichern; zuerst den Learning-Bericht zeigen.
    ss.pending_export = {"project_id": project_id, "name": filename, "bytes": out_bytes}
    return templates.TemplateResponse(request, "partials/export_result.html", {
        "project_id": project_id,
        "filename":   filename,
        "learned":    learned,
        "title":      "D84-Export bereit",
        "notes":      _shared_group_note(costs),
    })


@router.post("/api/projects/{project_id}/export-excel")
async def project_export_excel(project_id: int, request: Request,
                               scenario: str = Form("")):
    """Schreibt die kalkulierten Einheitspreise in die hochgeladene Excel-Datei zurück.

    Nutzt dieselbe Kostenbasis wie der D84-Export (_export_costs/_export_ep); nur das
    Ziel unterscheidet sich: Zellen statt GAEB-Elemente.
    """
    ss = get_session(request.session)
    if not ss.ej_db_conn:
        raise HTTPException(400, "Keine EasyJob-DB-Verbindung — bitte erst einloggen.")

    proj = _db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "Projekt nicht gefunden")
    if (proj.get("source_kind") or "gaeb") != "excel":
        raise HTTPException(400, "Dieses Projekt stammt nicht aus einer Excel-Datei.")
    if not proj.get("ej_project_id"):
        raise HTTPException(400, "Projekt hat keine EasyJob-Projekt-ID")

    src_bytes, src_name = _db.get_project_gaeb(project_id)
    if not src_bytes:
        raise HTTPException(404, "Keine Excel-Quelldatei für dieses Projekt gespeichert")

    layout = _project_excel_layout(project_id)
    if layout is None:
        raise HTTPException(400, "Für dieses Projekt ist keine Spalten-Zuordnung "
                                 "gespeichert — bitte neu importieren.")
    # Mehrere Szenarien treffen dieselben Zellen. Ohne Auswahl würden sie sich
    # gegenseitig überschreiben, also hier abbrechen statt zu raten.
    moeglich = _xl.scenario_names(layout)
    if moeglich and scenario not in moeglich:
        raise HTTPException(
            400, "Dieses Projekt hat mehrere Szenarien — bitte eines auswählen: "
                 + ", ".join(moeglich))

    costs   = _export_costs(ss, project_id, proj)
    project = _xl.parse_excel(src_bytes, layout, name=proj.get("name") or "")

    # EP je Position — offene Positionen (Menge 0) mit der gebuchten Menge rechnen,
    # sonst bliebe der Einzelpreis 0. Gleiche Regel wie im D84-Export.
    prices: dict[str, float] = {}
    for item in project.items:
        # Nur das gewählte Szenario schreiben — sonst treffen zwei Preise dieselbe Zelle.
        if scenario and (project.scenario_by_item.get(item.item_id) or "") != scenario:
            continue
        qty = float(item.qty or 0)
        if qty <= 0:
            qty = float(costs["qty_by_item"].get(item.item_id, 0) or 0) or 1.0
        ep = _export_ep(costs, item.item_id, item.oz, qty)
        if ep:
            prices[item.item_id] = ep

    res = _xlout.write_prices(src_bytes, layout, project, prices)

    # Ohne einen einzigen geschriebenen Preis darf kein Download angeboten werden —
    # sonst verschickt man ein Angebot mit leeren Preisspalten und merkt es nicht.
    if not res.written:
        grund = " ".join(res.notes) or (
            "Für die gebuchten Positionen ließ sich kein Einheitspreis ermitteln.")
        return HTMLResponse(
            '<div class="error-msg">Es wurde <b>kein einziger Preis</b> geschrieben — '
            f'Export abgebrochen.<br>{grund}</div>', status_code=422)

    stem = pathlib.Path(src_name or proj.get("name") or "Angebot").stem
    # Szenario in den Dateinamen, damit mehrere Exporte derselben Datei
    # unterscheidbar bleiben. Nur dateinamentaugliche Zeichen behalten.
    suffix = "_" + re.sub(r"[^\w.-]+", "-", scenario).strip("-") if scenario else ""
    filename = f"{stem}_Angebot{suffix}{res.suffix}"
    notes = list(res.notes) + _shared_group_note(costs)
    if scenario:
        notes.insert(0, "Szenario " + scenario + " — nur dessen Preise wurden geschrieben")
    notes.insert(0, f"{res.written} Positionen mit Preis befüllt"
                    + (f", {res.skipped} ohne Preis" if res.skipped else "")
                    + (f", {res.formulas} Gesamtpreis-Formeln unverändert gelassen"
                       if res.formulas else ""))
    ss.pending_export = {"project_id": project_id, "name": filename, "bytes": res.data}
    return templates.TemplateResponse(request, "partials/export_result.html", {
        "project_id": project_id,
        "filename":   filename,
        "learned":    costs["learned"],
        "title":      "Excel-Export bereit",
        "notes":      notes,
    })


@router.get("/api/projects/{project_id}/export-d84/download")
async def project_export_d84_download(project_id: int, request: Request):
    """Liefert die zuvor (beim Export) erzeugte D84-Datei aus dem Session-Puffer."""
    ss = get_session(request.session)
    pe = getattr(ss, "pending_export", None)
    if not pe or pe.get("project_id") != project_id:
        raise HTTPException(404, "Kein vorbereiteter Export — bitte erneut exportieren.")
    return Response(
        content=pe["bytes"],
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{pe["name"]}"'},
    )


# ─── Projektübersicht ─────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/overview", response_class=HTMLResponse)
async def project_overview(project_id: int, request: Request):
    """Zeigt eine read-only Übersicht eines gespeicherten Projekts:
    D83-Gruppenstruktur + gebuchte Artikel/Ressourcen aus EJ (live via DB)
    oder lokalem Snapshot als Fallback.
    """
    ss = get_session(request.session)

    # ── Projekt-Metadaten ────────────────────────────────────────────────────
    proj = _db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "Projekt nicht gefunden")

    # Nummer kommt aus der lokalen DB (get_project) — KEIN API-Call hier, damit
    # die Shell sofort erscheint. Fehlt sie, füllt sie der Content-Endpoint nach.
    proj.setdefault("ej_project_number", "")

    # Die Seite erscheint sofort mit dem Kopf; die (langsamen) Buchungsdaten
    # werden per HTMX über /overview/content nachgeladen.
    return templates.TemplateResponse(request, "projects_overview.html", {
        "project":  proj,
        "is_admin": ss.is_admin,
        # Die Personalplanung liegt in eigenen Tabellen, nicht im Abbild der
        # Buchungen — nur wenn es sie gibt, hat der Export etwas zu tun.
        "hat_crew": _db.load_crew_plan(project_id) is not None,
    })


@router.get("/projects/{project_id}/overview/content", response_class=HTMLResponse)
async def project_overview_content(project_id: int, request: Request):
    """Schwerer Teil der Projektübersicht (GAEB-Parse + Live-Buchungen aus EJ),
    per HTMX nachgeladen — so ist die Seite selbst sofort da."""
    ss = get_session(request.session)
    proj = _db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "Projekt nicht gefunden")
    proj.setdefault("ej_project_number", "")

    loop = asyncio.get_event_loop()
    # Projektnummer einmalig nachtragen, falls lokal noch nicht gespeichert —
    # das blockiert nur den (ohnehin nachgeladenen) Content, nicht die Shell.
    if not proj["ej_project_number"] and proj.get("ej_project_id") and ss.ej_client:
        try:
            _num = await loop.run_in_executor(
                None, lambda: ss.ej_client.get_project_number(int(proj["ej_project_id"]))
            )
            if _num:
                await loop.run_in_executor(None, lambda: _db.set_project_number(project_id, _num))
        except Exception:
            pass

    # ── D83-Gruppenstruktur + Vorbemerkungen (gecacht, Parse im Thread) ──────
    _data = await loop.run_in_executor(None, _overview_data, project_id)
    groups = _data["groups"]
    preliminaries = _data["preliminaries"]

    # ── Personal-Stammdaten (klein) ──────────────────────────────────────────
    personal_raw = await loop.run_in_executor(None, _db.load_personal_db)
    personal_by_id: dict[int, dict] = {
        int(p["id"]): p for p in personal_raw if p.get("id")
    }
    # Artikel werden erst NACH den Buchungen geladen — dann gezielt nur die
    # tatsächlich gebuchten (statt aller ~11.500).
    articles_by_ej_id: dict[int, dict] = {}

    # ── Buchungen: Live aus EJ-DB oder lokaler Snapshot ──────────────────────
    fallback_mode = False
    bookings_by_item: dict[str, list[dict]] = {}
    cost_mat  = 0.0
    cost_pers = 0.0
    cost_trans = 0.0
    cost_sonst = 0.0
    # In welchen Topf eine Ressource gehört, steht im lokalen Stamm — die Buchungszeile
    # in Easyjob sagt es nicht. Ein LKW oder eine Storno-Pauschale in den
    # Personalkosten ist beim Gegenrechnen nicht zu finden; die Einordnung ist
    # dieselbe wie beim Import (`_kostentopf`).
    from routes.import_ import _kostentopf as _topf_von

    class _RohRes:
        def __init__(self, row):
            self.id = int(row["id"])
            self.funktion = row.get("funktion") or ""
            self.ressourcenart = row.get("ressourcenart") or ""

    _topf_by_id = {int(r["id"]): _topf_von(_RohRes(r))
                   for r in _db.load_personal_db()}

    job_ids = [
        int(x) for x in (proj.get("ej_job_ids") or "").split(",")
        if x.strip().isdigit()
    ]

    if ss.ej_db_conn and job_ids:
        # ── Live-Modus: EJ-DB direkt abfragen ────────────────────────────────
        cn = None
        try:
            cn  = pyodbc.connect(ss.ej_db_conn)
            cur = cn.cursor()
            job_ph = ",".join("?" for _ in job_ids)

            # Gruppen-Caption → item_id (OZ-Mapping aus gespeicherten Buchungen)
            local_bookings = _db.get_project_bookings(project_id)
            # oz → item_id aus der GAEB-Struktur (deckt ALLE Positionen ab, auch
            # reine Ressourcen-Positionen, die nicht in project_bookings stehen).
            oz_to_item: dict[str, str] = {}
            for _hg in groups:
                for _pos in _iter_blocks(_hg):
                    if _pos.get("oz"):
                        oz_to_item.setdefault(_pos["oz"], _pos["item_id"])
            for b in local_bookings:
                if b.get("oz"):
                    oz_to_item.setdefault(b["oz"], b["item_id"])
            # Gebuchte Artikel (StockType2Job) je Gruppe
            # Hinweis: In EJ heißt die Mengenspalte "Factor" (Stückzahl),
            # "TimeFactor" ist der Preisfaktor (Berechnungsgrundlage).
            cur.execute(
                f"""
                SELECT s2j.IdStockType, s2j.Factor, s2j.IdStockType2JobGroup,
                       COALESCE(s2j.RentalPrice, s2j.BasePrice, 0) AS UnitPrice,
                       s2j.TimeFactor,
                       g.Caption AS GrpCaption
                FROM StockType2Job s2j
                LEFT JOIN StockType2JobGroup g
                    ON g.IdStockType2JobGroup = s2j.IdStockType2JobGroup
                WHERE s2j.IdJob IN ({job_ph})
                  -- Fest gebundene Referenzartikel (BOM-Komponenten, die EJ beim
                  -- Buchen des Elternartikels automatisch anlegt) nicht auflisten:
                  -- sie haben IdStockType2Job_Parent gesetzt und Preis 0.
                  AND (s2j.IdStockType2Job_Parent IS NULL OR s2j.IdStockType2Job_Parent = 0)
                ORDER BY s2j.IdStockType2JobGroup, s2j.IdStockType2Job
                """,
                *job_ids,
            )
            for r in cur.fetchall():
                id_st   = int(r[0] or 0)
                qty     = float(r[1] or 1)   # Factor = Stückzahl
                grp_id  = int(r[2] or 0)
                up      = float(r[3] or 0)
                tf      = float(r[4] or 1)   # TimeFactor = Preisfaktor
                grp_cap = r[5] or ""

                # OZ aus Gruppen-Caption "[OZ] Beschreibung" extrahieren
                oz = ""
                if grp_cap.startswith("[") and "]" in grp_cap:
                    oz = grp_cap[1:grp_cap.index("]")].strip()

                item_id = oz_to_item.get(oz, "")
                if not item_id:
                    # Fallback: item_id über ej_group_id aus lokalen Buchungen
                    for b in local_bookings:
                        if b.get("ej_group_id") and int(b["ej_group_id"]) == grp_id:
                            item_id = b["item_id"]
                            break

                if not item_id:
                    continue

                eff_up = up * tf
                cost_mat += qty * eff_up
                bookings_by_item.setdefault(item_id, []).append({
                    "type":           "article",
                    "ej_stock_type_id": id_st,
                    "qty":            qty,
                    "unit_price":     eff_up,
                    "caption":        "",
                })

            # Gebuchte Ressourcen (ResourceFunctionAllocation) je Gruppe
            cur.execute(
                f"""
                SELECT rfa.IdResourceFunction, rfa.DaysInAction,
                       rfa.TotalPrice, rfa.IdStockType2JobGroup,
                       g.Caption AS GrpCaption
                FROM ResourceFunctionAllocation rfa
                LEFT JOIN StockType2JobGroup g
                    ON g.IdStockType2JobGroup = rfa.IdStockType2JobGroup
                WHERE rfa.IdJob IN ({job_ph})
                ORDER BY rfa.IdStockType2JobGroup, rfa.IdResourceFunctionAllocation
                """,
                *job_ids,
            )
            for r in cur.fetchall():
                res_id  = int(r[0] or 0)
                days    = float(r[1] or 0)
                total_p = float(r[2] or 0)
                grp_id  = int(r[3] or 0) if r[3] else 0
                grp_cap = r[4] or ""

                # item_id primär über OZ aus der Gruppen-Caption "[OZ] …" — deckt
                # reine Ressourcen-Gruppen ab, die in project_bookings fehlen;
                # sonst über ej_group_id aus den lokalen (Artikel-)Buchungen.
                oz = ""
                if grp_cap.startswith("[") and "]" in grp_cap:
                    oz = grp_cap[1:grp_cap.index("]")].strip()
                item_id = oz_to_item.get(oz, "")
                if not item_id:
                    for b in local_bookings:
                        if b.get("ej_group_id") and int(b["ej_group_id"]) == grp_id:
                            item_id = b["item_id"]
                            break

                if not item_id:
                    continue

                _t = _topf_by_id.get(res_id, "personal")
                if _t == "transport":
                    cost_trans += total_p
                elif _t == "sonstiges":
                    cost_sonst += total_p
                else:
                    cost_pers += total_p
                bookings_by_item.setdefault(item_id, []).append({
                    "type":        "resource",
                    "resource_id": res_id,
                    "qty":         days,
                    "total_price": total_p,
                    "caption":     "",
                })

        except Exception as _e:
            logging.error("project_overview: EJ-DB-Abfrage fehlgeschlagen: %s", _e)
            fallback_mode = True
        finally:
            if cn is not None:
                try:
                    cn.close()
                except Exception:
                    pass

    if fallback_mode or (not ss.ej_db_conn and job_ids):
        # ── Fallback: lokaler Snapshot aus project_bookings ───────────────────
        fallback_mode = True
        local_bookings = _db.get_project_bookings(project_id)
        for b in local_bookings:
            item_id = b["item_id"]
            up      = float(b.get("unit_price") or 0)
            qty     = float(b.get("qty") or 1)
            if (b.get("kind") or "article") == "resource":
                total_p = qty * up   # Tage × Tagessatz
                _t = _topf_by_id.get(int(b.get("ej_stock_type_id") or 0), "personal")
                if _t == "transport":
                    cost_trans += total_p
                elif _t == "sonstiges":
                    cost_sonst += total_p
                else:
                    cost_pers += total_p
                bookings_by_item.setdefault(item_id, []).append({
                    "type":        "resource",
                    "resource_id": int(b.get("ej_stock_type_id") or 0),
                    "qty":         qty,
                    "total_price": total_p,
                    "caption":     b.get("description") or "",
                })
            else:
                cost_mat += qty * up
                bookings_by_item.setdefault(item_id, []).append({
                    "type":             "article",
                    "ej_stock_type_id": int(b.get("ej_stock_type_id") or 0),
                    "qty":              qty,
                    "unit_price":       up,
                    "caption":          b.get("description") or "",
                })

    # ── Nur die tatsächlich gebuchten Artikel laden (statt aller ~11.500) ────
    _st_ids = {
        b["ej_stock_type_id"]
        for bks in bookings_by_item.values() for b in bks
        if b.get("type") == "article" and b.get("ej_stock_type_id")
    }
    if _st_ids:
        articles_by_ej_id = {
            int(a["ej_id"]): a
            for a in await loop.run_in_executor(None, _db.load_articles_by_ej_ids, _st_ids)
            if a.get("ej_id")
        }

    # ── Kennzahlen ───────────────────────────────────────────────────────────
    total_pos   = sum(
        len(list(_iter_blocks(hg))) for hg in groups
    )
    booked_pos  = sum(
        1 for hg in groups
        for pos in _iter_blocks(hg)
        if bookings_by_item.get(pos["item_id"])
    )
    art_count   = sum(
        1 for bks in bookings_by_item.values()
        for b in bks if b["type"] == "article"
    )
    res_count   = sum(
        1 for bks in bookings_by_item.values()
        for b in bks if b["type"] == "resource"
    )
    metrics = {
        "total_pos":   total_pos,
        "booked_pos":  booked_pos,
        "unbooked_pos": max(0, total_pos - booked_pos),
        "art_count":   art_count,
        "res_count":   res_count,
        "cost_mat":    cost_mat,
        "cost_pers":   cost_pers,
        "cost_trans":  cost_trans,
        "cost_sonst":  cost_sonst,
        "cost_total":  cost_mat + cost_pers + cost_trans + cost_sonst,
    }

    # ── Kosten je Hauptgruppe (für Anzeige im Gruppen-Header) ────────────────
    grp_costs: dict[str, dict] = {}
    for hg in groups:
        mat = pers = 0.0
        for pos in _iter_blocks(hg):
            for bk in bookings_by_item.get(pos["item_id"], []):
                if bk["type"] == "article":
                    mat += float(bk.get("qty", 1)) * float(bk.get("unit_price", 0))
                elif bk["type"] == "resource":
                    pers += float(bk.get("total_price", 0))
        grp_costs[hg["name"]] = {"mat": mat, "pers": pers, "total": mat + pers}

    return templates.TemplateResponse(request, "partials/overview_content.html", {
        "project":          proj,
        "is_admin":         ss.is_admin,
        "groups":           groups,
        "preliminaries":    preliminaries,
        "bookings_by_item": bookings_by_item,
        "articles_by_ej_id": articles_by_ej_id,
        "personal_by_id":   personal_by_id,
        "metrics":          metrics,
        "grp_costs":        grp_costs,
        "fallback_mode":    fallback_mode,
    })


def _iter_blocks(hg: dict):
    """Iteriert alle primären Positionen einer Hauptgruppe (inkl. Untergruppen)."""
    for block in hg.get("blocks", []):
        yield block["primary"]
    for sub in hg.get("sub", []):
        for block in sub.get("blocks", []):
            yield block["primary"]
