"""Regressionstest für den Excel-LV-Parser gegen die echten Ausschreibungen in infos/.

Prüft Erkennung (Kopfzeile, Spaltenrollen, Mengenspalten, Zeilentypen) und den daraus
gebauten GaebProject. Bewusst ohne pytest — wie die übrigen Tests hier direkt aufrufbar:

    .venv/Scripts/python.exe tests/test_excel_parser.py
"""
import sys

# Konsole auf UTF-8: sonst stirbt schon ein "→" im print an cp1252 und der Test
# bricht mitten drin ab, ohne dass eine Prüfung fehlgeschlagen wäre.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from excel_parser import (ROW_GROUP, ROW_JOB, ROW_MAIN, ROW_NOTE, ROW_OUTLINE,
                          ROW_POS,
                          SHEET_AS_JOB, SHEET_AS_KEEP, SHEET_AS_MAIN,
                          layout_from_dict, layout_to_dict, parse_excel,
                          preview_workbook, probe_workbook)

INFOS = os.path.join(os.path.dirname(__file__), "..", "infos")

VECTOR = "20260731 Vector NHX 2026 offen.xlsx"
GOE    = "LOS1_GOE Ausschreibung_Technik 2026_Preisblatt_V2.1.xlsx"
LOS2   = "LOS2_LK_Technik_JPK_CMD.xlsx"
PAG    = "PAG_NOC2026_LV Veranstaltungstechnik_V1.xlsx"

_fails: list[str] = []
_oks = 0


def check(cond, msg: str) -> None:
    global _oks
    if cond:
        _oks += 1
    else:
        _fails.append(msg)
        print(f"  FAIL  {msg}")


def load(name: str) -> bytes:
    with open(os.path.join(INFOS, name), "rb") as fh:
        return fh.read()


def probe_of(name: str):
    return probe_workbook(load(name))


def sheet(probe, title: str):
    return next(sp for sp in probe.sheets if sp.layout.name == title)


def prow(sp, row: int):
    return next((p for p in sp.preview if p.row == row), None)


# ── Vector NHX: keine Positionsnummern, Hierarchie nur über Füllfarben ───────

def test_vector():
    print(f"\n=== {VECTOR}")
    pb = probe_of(VECTOR)
    s1 = sheet(pb, "Los 1 Technik")
    r  = s1.layout.roles
    check(s1.layout.header_row == 6, f"Kopfzeile 6 erwartet, ist {s1.layout.header_row}")
    check(r.desc == 3, f"desc=C(3) erwartet, ist {r.desc}")
    check(r.unit == 6, f"unit=F(6) erwartet, ist {r.unit}")
    check([q.col for q in r.qty] == [7], f"qty=[G(7)] erwartet, ist {[q.col for q in r.qty]}")
    check(4 in r.ref, f"ref soll die header-lose Spalte D(4) enthalten, ist {r.ref}")
    check(s1.layout.enabled, "Los 1 Technik muss aktiv sein")

    # Die ganze Gruppenkette bleibt erhalten — wie beim D83-Import. 'Rigging' ist
    # Ebene 1 (ROW_MAIN), 'Haupt-Rig' darunter eine Gruppe (ROW_GROUP), kein Hinweis.
    check(prow(s1, 13).kind == ROW_MAIN,
          f"Z13 'Rigging' = Hauptgruppe (Ebene 1), ist {prow(s1,13).kind}")
    check(prow(s1, 16).kind == ROW_GROUP,
          f"Z16 'Haupt-Rig' = Gruppe (Ebene 2), ist {prow(s1,16).kind}")
    check(prow(s1, 14).kind == ROW_NOTE, f"Z14 = Hinweis, ist {prow(s1,14).kind}")
    check(prow(s1, 15).kind == ROW_NOTE, f"Z15 = Hinweis, ist {prow(s1,15).kind}")
    check(prow(s1, 18).kind == ROW_POS,  f"Z18 = Position, ist {prow(s1,18).kind}")

    ov = sheet(pb, "Übersicht") if any(sp.layout.name == "Übersicht" for sp in pb.sheets) else None
    if ov:
        check(not ov.layout.enabled, "Sheet 'Übersicht' darf nicht aktiv sein")

    proj = parse_excel(load(VECTOR), pb.layout)
    z18 = next((i for i in proj.items if i.src_ref == "Los 1 Technik!18"), None)
    check(z18 is not None, "Position aus Zeile 18 fehlt")
    if z18:
        check(z18.qty == 660.0, f"Z18 qty=660 erwartet, ist {z18.qty}")
        check(z18.unit == "lfm", f"Z18 unit='lfm' erwartet, ist {z18.unit!r}")
        check("FD34" in z18.ref_text, f"Z18 ref_text soll 'FD34' enthalten, ist {z18.ref_text!r}")
        check("FD34" in z18.long_text, "Referenztext muss im long_text landen (Matcher-Input)")
        # Volle Kette: Blatt/Job → Rigging → Haupt-Rig. Die tiefste Ebene ist die
        # Hauptgruppe, 'Rigging' steht darüber als Elternebene.
        check("Haupt-Rig" in z18.category_path[-1],
              f"Z18 unter 'Haupt-Rig' erwartet, ist {z18.category_path}")
        check(any("Rigging" in p_ for p_ in z18.category_path[:-1]),
              f"'Rigging' muss als Elternebene im Pfad stehen, ist {z18.category_path}")

    l2 = [i for i in proj.items if i.src_ref.startswith("Los 2 Messebau!")]
    check(len(l2) > 0, "Los 2 Messebau liefert keine Positionen")
    check(sum(1 for i in l2 if i.is_alt) == 15,
          f"Los 2: 15 Alternativpositionen (AP) erwartet, sind {sum(1 for i in l2 if i.is_alt)}")
    check(sum(1 for i in l2 if i.is_eventual) == 9,
          f"Los 2: 9 Bedarfspositionen (BP) erwartet, sind {sum(1 for i in l2 if i.is_eventual)}")
    print(f"  {len(proj.items)} Positionen, {len(proj.remarks)} Hinweise")


# ── LOS1 GOE: vier Mengenspalten (Event-Szenarien) ──────────────────────────

def test_goe():
    print(f"\n=== {GOE}")
    pb = probe_of(GOE)
    sp = sheet(pb, "Preisblatt Technik")
    r  = sp.layout.roles
    check(sp.layout.header_row == 11, f"Kopfzeile 11 erwartet, ist {sp.layout.header_row}")
    check(r.oz == 1 and r.desc == 2 and r.unit == 6,
          f"oz=A/desc=B/unit=F erwartet, ist {r.oz}/{r.desc}/{r.unit}")
    cols = [q.col for q in r.qty]
    check(cols == [8, 10, 12, 14], f"vier Mengenspalten H/J/L/N erwartet, sind {cols}")
    check(all(q.label for q in r.qty), f"jede Mengenspalte braucht ein Label, sind {[q.label for q in r.qty]}")
    for q in r.qty:
        print(f"    Spalte {q.col}: {q.label!r}  ({q.values} Werte)")

    # Dieses Blatt nummeriert seine Gruppen ("01 Videotechnik", "01.01 Displays",
    # "01.01.01 LC-Displays") — damit IST die Nummer die Struktur. Die Strichzeilen
    # darunter ("-- UHD Professional") tragen keine und sind Zwischenüberschriften,
    # keine Ebene: sie erfanden sonst 69 von 133 Gruppen, die das LV nicht kennt.
    check(prow(sp, 13).kind == ROW_MAIN,
          f"Z13 '01 Videotechnik' = Hauptgruppe (Ebene 1), ist {prow(sp,13).kind}")
    check(prow(sp, 14).kind == ROW_GROUP,
          f"Z14 '01.01 Displays' = Gruppe (nummeriert), ist {prow(sp,14).kind}")
    check(prow(sp, 15).kind == ROW_GROUP,
          f"Z15 '01.01.01 LC-Displays' = Gruppe (nummeriert), ist {prow(sp,15).kind}")
    check(prow(sp, 16).kind == ROW_NOTE,
          f"Z16 '-- UHD Professional' ohne Nummer = Hinweis, ist {prow(sp,16).kind}")
    check(prow(sp, 29).kind == ROW_NOTE,
          f"Z29 '-- HD ULTRA NARROW' ohne Nummer = Hinweis, ist {prow(sp,29).kind}")
    check(prow(sp, 17).kind == ROW_POS, f"Z17 = Position, ist {prow(sp,17).kind}")
    # Die nummerierten Ebenen bleiben streng geschachtelt
    _lv = {r_: prow(sp, r_).level for r_ in (13, 14, 15)}
    check(_lv[13] < _lv[14] < _lv[15], f"Ebenen müssen tiefer werden, sind {_lv}")

    # Ein Szenario aktiv
    lay = pb.layout
    for i, q in enumerate(lay.sheets[1].roles.qty):
        q.active = (i == 2)
    p1 = parse_excel(load(GOE), lay)
    ids1 = {i.item_id for i in p1.items}
    check(len(ids1) == len(p1.items), "item_ids müssen eindeutig sein (1 Szenario)")

    # Alle vier Szenarien aktiv
    lay2 = pb.layout
    for q in lay2.sheets[1].roles.qty:
        q.active = True
    p4 = parse_excel(load(GOE), lay2)
    ids4 = {i.item_id for i in p4.items}
    check(len(ids4) == len(p4.items), "item_ids müssen über alle Szenarien eindeutig sein")
    check(len(p4.items) == 4 * len(p1.items),
          f"4 Szenarien = 4x Positionen ({len(p1.items)}), sind {len(p4.items)}")
    roots = {i.category_path[0] for i in p4.items if i.category_path}
    check(len(roots) == 4, f"4 Szenario-Wurzeln erwartet, sind {len(roots)}: {sorted(roots)}")
    print(f"  1 Szenario: {len(p1.items)} Positionen · 4 Szenarien: {len(p4.items)}")


# ── LOS2 LK: 10 Sheets, gleiche Gruppennamen, Label in der Pos-Spalte ───────

def test_los2():
    print(f"\n=== {LOS2}")
    pb = probe_of(LOS2)
    active = [sp.layout.name for sp in pb.sheets if sp.layout.enabled]
    print(f"  aktiv: {active}")
    check("JPK_Rigging_Licht" in active and "CMD_Rigging_Licht" in active,
          "JPK/CMD-Sheets müssen aktiv sein")
    for dead in ("Deckblatt", "Tagessätze", "Allgemeine Hinweise"):
        if any(sp.layout.name == dead for sp in pb.sheets):
            check(dead not in active, f"Sheet {dead!r} darf nicht aktiv sein")

    jpk = sheet(pb, "JPK_Rigging_Licht")
    r   = jpk.layout.roles
    check(r.desc == 5 and r.unit == 4 and [q.col for q in r.qty] == [3],
          f"desc=E/unit=D/qty=C erwartet, ist {r.desc}/{r.unit}/{[q.col for q in r.qty]}")
    check(prow(jpk, 11).kind == ROW_MAIN,
          f"Z11 'Licht' (Label in Pos-Spalte) = Hauptgruppe, ist {prow(jpk,11).kind}")
    # Die Kette darf beliebig tief sein (wie D83), aber jede Ebene muss ein Label
    # tragen — leere Zwischenebenen wären Löcher im Pfad.
    _pfade = [i.category_path for i in parse_excel(load(LOS2), pb.layout).items]
    check(all(all(p_ for p_ in cp) for cp in _pfade),
          "keine leere Ebene im category_path")
    check(max(len(cp) for cp in _pfade) >= 3,
          f"volle Gruppenkette erwartet, tiefster Pfad ist {max(len(cp) for cp in _pfade)}")
    check(prow(jpk, 12).kind == ROW_POS, f"Z12 'B.01' = Position, ist {prow(jpk,12).kind}")

    proj = parse_excel(load(LOS2), pb.layout)
    labels = {p for i in proj.items for p in i.category_path}
    licht = sorted(l for l in labels if l.startswith("Licht ("))
    check(len(licht) >= 2, f"'Licht' muss je Sheet eine eigene Gruppe sein, gefunden: {licht}")
    print(f"  Licht-Gruppen: {licht}")
    check(not any(l.lower().startswith("summe") for l in labels),
          f"'Summe'-Zeilen dürfen keine Gruppe werden: {[l for l in labels if 'umme' in l]}")
    print(f"  {len(proj.items)} Positionen aus {len(active)} Sheets")


# ── PAG NOC: Positionsnummern, mehrzeilige Beschreibung, Personal ohne Menge ─

def test_pag():
    print(f"\n=== {PAG}")
    pb = probe_of(PAG)
    mat = sheet(pb, "01_Material")
    r   = mat.layout.roles
    check(mat.layout.header_row == 11, f"Kopfzeile 11 erwartet, ist {mat.layout.header_row}")
    check(r.oz == 1 and r.desc == 2, f"oz=A/desc=B erwartet, ist {r.oz}/{r.desc}")
    check(3 in r.ref, f"Referenz-Spalte C(3) erwartet, ist {r.ref}")
    check([q.col for q in r.qty] == [5], f"qty=E(5) erwartet, ist {[q.col for q in r.qty]}")

    check(prow(mat, 13).kind == ROW_MAIN,
          f"Z13 '1. Rigging' = Hauptgruppe, ist {prow(mat,13).kind}")
    # 01_Material nummeriert seine Gruppen ("1. Rigging"), 'Traversen' hat keine
    # Nummer → Zwischenüberschrift, keine Ebene.
    check(prow(mat, 15).kind == ROW_NOTE,
          f"Z15 'Traversen' ohne Nummer = Hinweis, ist {prow(mat,15).kind}")
    check(prow(mat, 16).kind == ROW_POS, f"Z16 = Position, ist {prow(mat,16).kind}")

    per = sheet(pb, "02_Personal")
    check(prow(per, 13).kind == ROW_MAIN,
          f"02_Personal Z13 '1. Personal' = Hauptgruppe, ist {prow(per,13).kind}")
    check(prow(per, 17).kind == ROW_GROUP,
          f"02_Personal Z17 '1.1 Personal Aufbau' = Gruppe, ist {prow(per,17).kind}")
    check(prow(per, 18).kind == ROW_POS,
          f"02_Personal Z18 '1.1.1 Technische Leitung' = Position trotz leerer Menge, "
          f"ist {prow(per,18).kind}")
    check(prow(per, 15).kind == ROW_NOTE,
          f"02_Personal Z15 = Hinweis, ist {prow(per,15).kind}")

    proj = parse_excel(load(PAG), pb.layout)
    z16 = next((i for i in proj.items if i.src_ref == "01_Material!16"), None)
    check(z16 is not None and z16.oz == "1.1", f"Z16 oz='1.1' erwartet, ist {z16 and z16.oz!r}")
    z32 = next((i for i in proj.items if i.src_ref == "01_Material!32"), None)
    check(z32 is not None, "Position aus Zeile 32 fehlt")
    if z32:
        check("\n" not in z32.description,
              f"description muss einzeilig sein, ist {z32.description!r}")
        check("Bestückung" in z32.long_text,
              f"Folgezeilen gehören in long_text, ist {z32.long_text!r}")
        check("Meyer Sound LINA" in z32.ref_text, f"ref_text: {z32.ref_text!r}")
    print(f"  {len(proj.items)} Positionen, {len(proj.remarks)} Hinweise")


# ── Pinsel: Zeilen-Override, Kopfzeile, Job/Hauptgruppe, Blatt-Modus ───────

def test_brush():
    print("\n=== Pinsel-Pfade (Vector)")
    data = load(VECTOR)
    pb   = probe_of(VECTOR)
    lay  = pb.layout
    sl   = lay.sheet("Los 1 Technik")

    def snap(probe):
        sp = sheet(probe, "Los 1 Technik")
        return sp.counts, {p.row: (p.kind, p.level, p.outline) for p in sp.preview}

    c0, k0 = snap(pb)
    check(k0[13][0] == ROW_MAIN, f"Z13 'Rigging' = Ebene 1, ist {k0[13]}")
    check(k0[16][0] == ROW_GROUP, f"Z16 'Haupt-Rig' = Ebene 2, ist {k0[16]}")

    # Position zur Gruppe machen — darf die Ebenen der übrigen Zeilen NICHT verschieben
    sl.row_overrides["18"] = ROW_MAIN
    c1, k1 = snap(preview_workbook(data, lay))
    check(k1[18][0] == ROW_MAIN, f"Z18 gemalte Hauptgruppe, ist {k1[18]}")
    check(c1["pos"] == c0["pos"] - 1, f"eine Position weniger: {c0['pos']} → {c1['pos']}")
    check(k1[13][0] == ROW_MAIN, f"Z13 'Rigging' behält seine Ebene, ist {k1[13]}")
    # Gezählt wird das Endergebnis: Z18 wird angelegt, dafür verliert 'Haupt-Rig'
    # (Z16) seine direkten Positionen und rutscht in die Gliederung. Die Zahl der
    # angelegten Gruppen bleibt also gleich, eine Gliederungsebene kommt dazu.
    check(c1["main"] == c0["main"],
          f"gleich viele angelegte Gruppen erwartet: {c0['main']} → {c1['main']}")
    check(c1["outline"] == c0["outline"] + 1,
          f"eine Gliederungsebene mehr erwartet: {c0['outline']} → {c1['outline']}")
    check(c1["note"] == c0["note"],
          f"Hinweise unverändert erwartet: {c0['note']} → {c1['note']}")
    check(k1[38][0] == k0[38][0],
          f"Z38 unverändert erwartet, war {k0[38]} ist {k1[38]}")
    # Die Position darunter hängt jetzt unter der neuen Gruppe
    i19 = next(i for i in parse_excel(data, lay).items if i.src_ref == "Los 1 Technik!19")
    check("(2.18)" in i19.category_path[-1],
          f"Z19 unter der gemalten Hauptgruppe erwartet, ist {i19.category_path[-1]!r}")
    del sl.row_overrides["18"]

    # Job-Pinsel: eigener Easyjob-Job ab dieser Zeile
    sl.row_overrides["13"] = ROW_JOB
    proj = parse_excel(data, lay)
    jobs = {j for j in proj.job_by_item.values() if j}
    check(any("Rigging" in j for j in jobs), f"Job aus Zeile 13 erwartet, sind {sorted(jobs)}")
    del sl.row_overrides["13"]

    # Kopfzeile verschieben: Zeilen 7-13 tragen nur Gruppen/Hinweise, keine Positionen
    sl.header_row = 13
    c2, k2 = snap(preview_workbook(data, lay))
    check(min(k2) == 13, f"Vorschau muss bei 13 beginnen, ist {min(k2)}")
    check(c2["pos"] == c0["pos"], f"Positionen unverändert: {c0['pos']} → {c2['pos']}")
    check(c2["main"] < c0["main"] or c2["outline"] < c0["outline"]
          or c2["note"] < c0["note"],
          f"Zeilen 7-13 fallen aus dem Datenbereich: {c0} → {c2}")
    sl.header_row = 6
    print(f"  {c0['pos']} Pos · {c0['main']} Hauptgruppen · {c0['note']} Hinweise")


def test_sheet_mode():
    print("\n=== Blatt-Modi (LOS2, 10 aktive Blätter)")
    data = load(LOS2)
    pb   = probe_of(LOS2)

    def run(mode):
        lay = pb.layout
        lay.sheet_mode = mode
        pr = parse_excel(data, lay)
        jobs = []
        for i in pr.items:
            j = pr.job_by_item.get(i.item_id, "")
            if j not in jobs:
                jobs.append(j)
        item = next(i for i in pr.items if i.src_ref.startswith("JPK_Rigging_Licht!"))
        return pr, jobs, item.category_path

    pr_j, jobs_j, path_j = run(SHEET_AS_JOB)
    pr_k, jobs_k, path_k = run(SHEET_AS_KEEP)
    pr_m, jobs_m, path_m = run(SHEET_AS_MAIN)

    print(f"  Jobs           : {len(jobs_j)} Jobs   · Pfad {path_j}")
    print(f"  Struktur behalten: {len(jobs_k)} Job    · Pfad {path_k}")
    print(f"  Blatt = HG     : {len(jobs_m)} Job    · Pfad {path_m}")

    # 1) Jedes Blatt ein eigener Job, Hauptgruppen bleiben
    check(len(jobs_j) == 10, f"10 Blätter = 10 Jobs erwartet, sind {len(jobs_j)}")
    check("Licht" in path_j[-1], f"HG 'Licht' erwartet, Pfad {path_j}")

    # 2) Ein Job, Struktur beibehalten: Blatt als Kontext, Hauptgruppe bleibt
    check(jobs_k == [""], f"ein Standard-Job erwartet, ist {jobs_k}")
    check(path_k[0] == "JPK_Rigging_Licht", f"Blatt als Kontext erwartet, Pfad {path_k}")
    check("Licht" in path_k[-1], f"HG 'Licht' muss bleiben, Pfad {path_k}")

    # 3) Ein Job, Blatt IST die Hauptgruppe: Gruppen darin werden Hinweise
    check(jobs_m == [""], f"ein Standard-Job erwartet, ist {jobs_m}")
    check(path_m == ["JPK_Rigging_Licht"],
          f"Blatt als einzige Gruppenebene erwartet, Pfad {path_m}")
    lay = pb.layout
    lay.sheet_mode = SHEET_AS_MAIN
    sp = sheet(preview_workbook(data, lay), "JPK_Rigging_Licht")
    check(sp.counts["main"] == 0,
          f"keine Hauptgruppe im Blatt erwartet, sind {sp.counts['main']}")
    check(sp.counts["note"] == 3,
          f"die 3 Gruppen werden Hinweise, sind {sp.counts['note']}")

    # Positionszahl darf sich in keinem Modus ändern
    check(len(pr_j.items) == len(pr_k.items) == len(pr_m.items),
          f"Positionszahl je Modus: {len(pr_j.items)}/{len(pr_k.items)}/{len(pr_m.items)}")
    # Die Tiefe ist frei (volle Kette wie D83), aber im Modus „Blatt IST Hauptgruppe"
    # darf es unter dem Blatt keine Ebene mehr geben — genau das sagt der Modus.
    deep_m = [i.category_path for i in pr_m.items if len(i.category_path) > 1]
    check(not deep_m, f"main: unter dem Blatt darf keine Gruppe stehen: {deep_m[:2]}")
    for name, pr in [("job", pr_j), ("keep", pr_k)]:
        tiefe = max(len(i.category_path) for i in pr.items)
        check(tiefe >= 3, f"{name}: volle Gruppenkette erwartet, tiefster Pfad {tiefe}")
        check(all(all(p_ for p_ in i.category_path) for i in pr.items),
              f"{name}: keine leere Ebene im category_path")


# ── Vererbung: Positionen gehören zum letzten Job / zur letzten Hauptgruppe ──

def test_inheritance():
    print("\n=== Vererbung von Job und Hauptgruppe (PAG 01_Material)")
    data = load(PAG)
    pb   = probe_of(PAG)
    lay  = pb.layout
    for sl in lay.sheets:                     # nur ein Blatt betrachten
        sl.enabled = sl.name == "01_Material"
    sl = lay.sheet("01_Material")

    # Struktur von Hand setzen: Job / HG / Positionen / HG / Positionen / Job
    sl.row_overrides = {
        "13": ROW_JOB,    # "1. Rigging"     -> Job
        "15": ROW_MAIN,   # "Traversen"      -> Hauptgruppe
        "23": ROW_MAIN,   # "Kettenzuege"    -> Hauptgruppe
        "29": ROW_JOB,    # "2. Tontechnik"  -> neuer Job
    }
    proj = parse_excel(data, lay)
    by_row = {int(i.src_ref.split("!")[1]): i for i in proj.items}

    def job_of(row):
        return proj.job_by_item.get(by_row[row].item_id, "")

    def hg_of(row):
        return by_row[row].category_path[-1] if by_row[row].category_path else ""

    # Zeilen 16-22 stehen unter Job "Rigging" und HG "Traversen"
    for row in (16, 18, 22):
        check("Rigging" in job_of(row), f"Z{row} Job Rigging erwartet, ist {job_of(row)!r}")
        check("Traversen" in hg_of(row), f"Z{row} HG Traversen erwartet, ist {hg_of(row)!r}")

    # Ab Zeile 24 gilt die neue Hauptgruppe, der Job bleibt
    for row in (24, 27):
        check("Rigging" in job_of(row), f"Z{row} Job bleibt Rigging, ist {job_of(row)!r}")
        check("Kettenz" in hg_of(row), f"Z{row} HG Kettenzuege erwartet, ist {hg_of(row)!r}")

    # Ab Zeile 29 neuer Job; der Gruppenstapel beginnt von vorn
    later = [r for r in by_row if r > 29]
    check(bool(later), "keine Positionen nach Zeile 29 gefunden")
    if later:
        r0 = min(later)
        check("Tontechnik" in job_of(r0),
              f"Z{r0} Job Tontechnik erwartet, ist {job_of(r0)!r}")
        check("Traversen" not in hg_of(r0) and "Kettenz" not in hg_of(r0),
              f"Z{r0} darf keine HG aus dem alten Job erben, ist {hg_of(r0)!r}")

    jobs = sorted({j for j in proj.job_by_item.values() if j})
    print(f"  Jobs: {jobs}")
    print(f"  Z16 -> Job {job_of(16)!r} / HG {hg_of(16)!r}")
    print(f"  Z24 -> Job {job_of(24)!r} / HG {hg_of(24)!r}")
    check(len(jobs) == 2, f"zwei Jobs erwartet, sind {len(jobs)}: {jobs}")


# ── Übergreifende Invarianten ───────────────────────────────────────────────

def test_invariants():
    print("\n=== Invarianten über alle Dateien")
    for name in (VECTOR, GOE, LOS2, PAG):
        data = load(name)
        pb   = probe_workbook(data)
        proj = parse_excel(data, pb.layout)
        short = name[:34]
        check(all(i.description.strip() for i in proj.items),
              f"{short}: Position ohne Beschreibung")
        ids = [i.item_id for i in proj.items]
        check(len(ids) == len(set(ids)), f"{short}: doppelte item_id")
        check(all(i.src_ref for i in proj.items), f"{short}: Position ohne src_ref")
        check(len(proj.items) > 0, f"{short}: keine Positionen erkannt")

        # Layout muss den Roundtrip durch JSON überleben (Entwürfe + Layout-Profile)
        rt = layout_from_dict(layout_to_dict(pb.layout))
        p2 = parse_excel(data, rt)
        check(len(p2.items) == len(proj.items),
              f"{short}: JSON-Roundtrip ändert Positionszahl "
              f"({len(proj.items)} → {len(p2.items)})")
        check(pb.fingerprint and len(pb.fingerprint) == 16, f"{short}: Fingerprint fehlt")

        # Die Zähler der Mapping-Vorschau müssen das ENDERGEBNIS zeigen, nicht die
        # Pinselebene: "main" sind die Gruppen, die in Easyjob entstehen, "outline"
        # die Ebenen darüber. Vorher zählte "main" die Ebene-1-Zeilen — in LOS1 GOE
        # 9 Stück, von denen keine einzige angelegt wird.
        for sp in pb.sheets:
            if not sp.layout.enabled:
                continue
            for schl in ("pos", "job", "main", "outline", "note"):
                check(schl in sp.counts,
                      f"{short}/{sp.layout.name[:16]}: Zähler {schl!r} fehlt")
            gruppen = [r for r in sp.preview if r.kind in (ROW_MAIN, ROW_GROUP)]
            check(sp.counts["main"] + sp.counts["outline"] >= len(gruppen),
                  f"{short}/{sp.layout.name[:16]}: {len(gruppen)} Gruppenzeilen in der "
                  f"Vorschau, aber nur {sp.counts['main'] + sp.counts['outline']} gezählt")
            # Gezählt wird über das ganze Blatt, die Vorschau ist auf
            # _PREVIEW_ROWS gekürzt — deckungsgleich also nur ohne Kürzung.
            angelegt = sum(1 for r in gruppen if not r.outline)
            gliedert = sum(1 for r in gruppen if r.outline)
            if not sp.counts["truncated"]:
                check(sp.counts["main"] == angelegt,
                      f"{short}/{sp.layout.name[:16]}: 'main' {sp.counts['main']} != "
                      f"{angelegt} Vorschauzeilen ohne Gliederung")
                check(sp.counts["outline"] == gliedert,
                      f"{short}/{sp.layout.name[:16]}: 'outline' "
                      f"{sp.counts['outline']} != {gliedert} Gliederungszeilen")
            else:
                check(sp.counts["main"] >= angelegt and sp.counts["outline"] >= gliedert,
                      f"{short}/{sp.layout.name[:16]}: Zähler kleiner als die "
                      f"gezeigten Zeilen ({angelegt}/{gliedert})")
        print(f"  {short:36s} {len(proj.items):4d} Pos · fp={pb.fingerprint}")


def test_nummerierung_entscheidet_pro_blatt():
    """Nummeriert ein Blatt seine Gruppen, ist eine Gruppenzeile OHNE Nummer ein
    Hinweis — aber nur dann.

    In LOS1 GOE tragen "01 Videotechnik", "01.01 Displays" und "01.01.01 LC-Displays"
    Nummern, die Strichzeilen darunter ("-- UHD Professional") nicht. Als Ebene gezählt
    erfanden sie 69 von 133 Gruppen, die das LV so nicht kennt.

    Vector NHX nummeriert dagegen gar nichts und baut seine Hierarchie allein über
    Füllfarben — global angewandt bliebe dort keine einzige Gruppe übrig. Deshalb pro
    Blatt entschieden.
    """
    print("\n=== Nummerierte Gruppen: Regel gilt pro Blatt")

    # 1) Nummeriertes Blatt: nur die nummerierten Ebenen bleiben Gruppen
    sp = sheet(probe_of(GOE), "Preisblatt Technik")
    fuer = {13: ROW_MAIN, 14: ROW_GROUP, 15: ROW_GROUP, 16: ROW_NOTE, 29: ROW_NOTE}
    for row, soll in fuer.items():
        check(prow(sp, row).kind == soll,
              f"GOE Z{row}: {soll} erwartet, ist {prow(sp,row).kind}")
    print("  GOE Preisblatt Technik: 01/01.01/01.01.01 = Gruppen, Strichzeilen = Hinweise")

    # 2) Unnummeriertes Blatt: Hierarchie über Füllfarben bleibt erhalten
    v1 = sheet(probe_of(VECTOR), "Los 1 Technik")
    hg = [r for r in v1.preview if r.kind in (ROW_MAIN, ROW_GROUP)]
    check(len(hg) > 10,
          f"Vector baut die Hierarchie über Füllfarben, >10 Gruppen erwartet, {len(hg)}")
    check(prow(v1, 13).kind == ROW_MAIN, f"Z13 'Rigging' bleibt HG, ist {prow(v1,13).kind}")
    check(prow(v1, 16).kind == ROW_GROUP,
          f"Z16 'Haupt-Rig' bleibt Gruppe, ist {prow(v1,16).kind}")
    print(f"  Vector Los 1 Technik: {len(hg)} Gruppen ohne jede Nummer — bleiben Gruppen")

    # 3) Der Pinsel sticht die Regel: gemalte Ebenen gelten immer
    lay = probe_of(GOE).layout
    sl  = lay.sheet("Preisblatt Technik")
    sl.row_overrides["16"] = ROW_MAIN            # "-- UHD Professional", ohne Nummer
    sp2 = sheet(preview_workbook(load(GOE), lay), "Preisblatt Technik")
    check(prow(sp2, 16).kind == ROW_MAIN,
          f"gemalte Ebene muss die Nummern-Regel überstimmen, ist {prow(sp2,16).kind}")
    del sl.row_overrides["16"]
    print("  gemalte Ebene ohne Nummer bleibt Gruppe")

    # 4) Wirkung auf die Gruppenliste: die Nummern der Ebenen müssen echt
    #    geschachtelt sein. Die Phantom-Ebenen hatten keine eigene Nummer und
    #    erbten die des Elters — vier Geschwister trugen dieselbe "01.01.01".
    from routes.import_ import _import_gaeb_groups
    for name in (VECTOR, GOE, LOS2, PAG):
        data = load(name)
        proj = parse_excel(data, probe_workbook(data).layout)
        for level in (0, 1):
            for g in _import_gaeb_groups(proj, level=level, alt_active={}):
                nums = [x["num"] for x in g["parents"] if x["num"]]
                for a, b in zip(nums, nums[1:]):
                    check(b.startswith(a + "."),
                          f"{name[:22]} level={level}: {b} ist kein Kind von {a}")
                if nums and g.get("num"):
                    check(g["num"].startswith(nums[-1] + "."),
                          f"{name[:22]} level={level}: HG {g['num']} passt nicht "
                          f"unter {nums[-1]}")
        print(f"  {name[:34]:36} Nummern der Ebenen echt geschachtelt")


def test_gliederung_pinsel():
    """Gliederung ergibt sich normalerweise, lässt sich aber erzwingen.

    Abgeleitet: eine Gruppe ohne eigene Positionen. In LOS1 GOE sind das genau
    "01 Videotechnik" (Z13) und "01.01 Displays" (Z14) — "01.01.01 LC-Displays" (Z15)
    hat die Positionen 01.01.01.01ff direkt darunter und wird angelegt.

    Gemalt: die Ebene wird nie Hauptgruppe. Positionen direkt darunter hängen sich an
    die nächste Ebene darüber, die angelegt werden darf.
    """
    print("\n=== Gliederung: abgeleitet und malbar")
    lay = probe_of(GOE).layout
    sl  = lay.sheet("Preisblatt Technik")

    def rollen():
        sp = sheet(preview_workbook(load(GOE), lay), "Preisblatt Technik")
        return {p.row: (p.kind, p.outline) for p in sp.preview}, sp.counts

    def pfad(zeile):
        pr = parse_excel(load(GOE), lay)
        it = next(i for i in pr.items if i.src_ref == f"Preisblatt Technik!{zeile}")
        return it.category_path

    k0, c0 = rollen()
    check(k0[13][1] is True, f"Z13 '01 Videotechnik' = Gliederung, ist {k0[13]}")
    check(k0[14][1] is True, f"Z14 '01.01 Displays' = Gliederung, ist {k0[14]}")
    check(k0[15][1] is False, f"Z15 '01.01.01 LC-Displays' = Hauptgruppe, ist {k0[15]}")
    p0 = pfad(17)
    check("LC-Displays" in p0[-1],
          f"Z17 muss unter 'LC-Displays' hängen, Pfad {p0}")
    print(f"  abgeleitet: Z13/Z14 Gliederung, Z15 Hauptgruppe · "
          f"{c0['main']} HG / {c0['outline']} Gliederung")

    # Pinsel: Z15 erzwingen → Positionen wandern hoch zu 'Displays'
    sl.row_overrides["15"] = ROW_OUTLINE
    k1, c1 = rollen()
    check(k1[15] == (ROW_OUTLINE, True),
          f"Z15 gemalte Gliederung erwartet, ist {k1[15]}")
    check(k1[14][1] is False,
          f"Z14 'Displays' übernimmt die Positionen und wird angelegt, ist {k1[14]}")
    p1 = pfad(17)
    check("Displays" in p1[-1] and "LC-Displays" not in p1[-1],
          f"Z17 muss jetzt unter 'Displays' hängen, Pfad {p1}")
    check(len(p1) == len(p0) - 1,
          f"eine Ebene weniger im Pfad erwartet: {p0} → {p1}")
    # Eine Ebene wird Gliederung, eine wird Hauptgruppe — die Summe bleibt
    check(c1["main"] + c1["outline"] == c0["main"] + c0["outline"],
          f"Gesamtzahl der Ebenen darf sich nicht ändern: "
          f"{c0['main']}+{c0['outline']} → {c1['main']}+{c1['outline']}")
    print(f"  gemalt: Z15 Gliederung, Z14 Hauptgruppe · Pfad {len(p0)} → {len(p1)} Ebenen")

    # Der Pinsel übersteht die Nummern-Regel (Z15 trägt eine Nummer, Z16 nicht)
    del sl.row_overrides["15"]
    sl.row_overrides["16"] = ROW_OUTLINE          # '-- UHD Professional', ohne Nummer
    k2, _ = rollen()
    check(k2[16] == (ROW_OUTLINE, True),
          f"gemalte Gliederung ohne Nummer muss bestehen bleiben, ist {k2[16]}")
    del sl.row_overrides["16"]
    print("  gemalte Gliederung ohne Positionsnummer bleibt bestehen")


def test_pinsel_kippt_nicht_die_nummern_regel():
    """Ein Pinselstrich darf die Nummern-Regel eines Blatts nicht umdrehen.

    Die Regel "nummeriert dieses Blatt seine Gruppen?" ist eine Frage an die DATEI.
    Positionszeilen tragen eine Nummer — malte man eine davon zur Hauptgruppe, galt
    das Blatt schlagartig als nummeriert und alle erkannten Gruppen (die dort keine
    Nummer haben) wurden zu Hinweisen: aus 4 Gruppen in CMD_Video wurden 1 Gruppe
    und 4 Hinweise.
    """
    print("\n=== Pinsel kippt die Nummern-Regel nicht")
    data = load(LOS2)
    for blatt, n_soll in (("CMD_Video", 4), ("JPK_Rigging_Licht", 3)):
        lay = probe_of(LOS2).layout
        sl  = lay.sheet(blatt)

        def zaehl():
            sp = sheet(preview_workbook(data, lay, opened={blatt}), blatt)
            return sp.counts

        c0 = zaehl()
        check(c0["main"] == n_soll,
              f"{blatt}: {n_soll} Gruppen erwartet, sind {c0['main']}")
        check(c0["note"] == 0, f"{blatt}: keine Hinweise erwartet, sind {c0['note']}")

        for zeile, wert in (("12", ROW_MAIN), ("11", ROW_OUTLINE)):
            sl.row_overrides[zeile] = wert
            c1 = zaehl()
            check(c1["note"] == c0["note"],
                  f"{blatt}: Pinsel ({wert} auf Z{zeile}) hat Gruppen zu Hinweisen "
                  f"gemacht: {c0['note']} → {c1['note']}")
            check(c1["main"] + c1["outline"] >= n_soll,
                  f"{blatt}: Ebenen verloren nach Pinsel {wert} auf Z{zeile}: "
                  f"{c1['main']}+{c1['outline']} < {n_soll}")
            del sl.row_overrides[zeile]
        print(f"  {blatt:20} {n_soll} Gruppen bleiben, auch nach Pinselstrich")

    # Gegenprobe: auf einem nummerierten Blatt muss die Regel weiter greifen
    sp = sheet(probe_of(GOE), "Preisblatt Technik")
    check(prow(sp, 16).kind == ROW_NOTE,
          f"GOE Z16 ohne Nummer muss Hinweis bleiben, ist {prow(sp,16).kind}")
    print("  GOE Preisblatt Technik: Regel greift unverändert")


# Von pytest gesehen: check() sammelt nur, damit ein Lauf ALLE Fehlschläge zeigt.
# Ohne diesen Abschluss meldet pytest jede Testfunktion als PASSED, auch wenn jede
# einzelne Prüfung fehlgeschlagen ist.
def test_zz_alle_pruefungen_ok():
    assert not _fails, f"{len(_fails)} Prüfung(en) fehlgeschlagen: " + "; ".join(_fails)


if __name__ == "__main__":
    test_vector()
    test_goe()
    test_los2()
    test_pag()
    test_brush()
    test_sheet_mode()
    test_inheritance()
    test_invariants()
    test_nummerierung_entscheidet_pro_blatt()
    test_gliederung_pinsel()
    test_pinsel_kippt_nicht_die_nummern_regel()
    print(f"\n{'='*60}\n{_oks} Prüfungen ok, {len(_fails)} fehlgeschlagen")
    if _fails:
        for f in _fails:
            print(f"  - {f}")
        sys.exit(1)
