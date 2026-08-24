"""Die Gruppenreihenfolge muss der Datei folgen.

Sortiert wurde nach der Ordnungszahl als **Text** — damit stand "4.202" vor "4.13"
und "1.10" vor "1.2". Bei Excel-LVs kommt hinzu, dass die Ordnungszahlen pro Blatt
wieder bei 1 anfangen, ein Vergleich über Blätter hinweg also gar nichts bedeutet.

Regel jetzt: oberste Ebene (Blatt bzw. Job) in Dokumentreihenfolge, darin die
Ordnungszahl zahlenweise, bei gleicher Ordnungszahl wieder das Dokument.

    .venv/Scripts/python.exe tests/test_group_order.py
"""
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import excel_parser as _xl
from gaeb_parser import parse_gaeb
from routes.import_ import _import_gaeb_groups, _oz_key

_fails: list[str] = []
_oks = 0


def check(cond, msg: str) -> None:
    global _oks
    if cond:
        _oks += 1
    else:
        _fails.append(msg)
        print(f"  FAIL  {msg}")


def dokument_reihenfolge(project, level: int) -> list[str]:
    """Gruppennamen in der Reihenfolge, in der sie in der Datei zuerst auftauchen."""
    out, seen = [], set()
    for it in project.items:
        p = it.category_path
        if not p:
            name = "(ohne Gruppe)"
        elif level == 0:
            name = p[-1]
        else:
            name = p[-2] if len(p) >= 2 else p[-1]
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def test_oz_key():
    print("\n=== Ordnungszahlen werden zahlenweise sortiert")
    folge = ["4.13", "4.202", "4.2", "1.1", "1.2", "1.10", "10", "9",
             "01.02", "01.02.01", "A.01", "B.1"]
    ist = sorted(folge, key=_oz_key)
    print("  ", ist)
    # zahlenweise, nicht alphabetisch
    for kleiner, groesser in [("4.2", "4.13"), ("4.13", "4.202"), ("1.2", "1.10"),
                              ("9", "10"), ("01.02", "01.02.01")]:
        check(ist.index(kleiner) < ist.index(groesser),
              f"{kleiner} muss vor {groesser} stehen, ist {ist}")
    # Buchstaben hinten, aber stabil
    check(ist.index("10") < ist.index("A.01"), f"Zahlen vor Buchstaben, ist {ist}")


EXCEL = ["PAG_NOC2026_LV Veranstaltungstechnik_V1.xlsx",
         "20260731 Vector NHX 2026 offen.xlsx",
         "LOS2_LK_Technik_JPK_CMD.xlsx",
         "LOS1_GOE Ausschreibung_Technik 2026_Preisblatt_V2.1.xlsx"]


def test_excel_reihenfolge():
    print("\n=== Excel: Gruppen stehen wie in der Datei")
    for fname in EXCEL:
        data = open(os.path.join("infos", fname), "rb").read()
        layout = _xl.probe_workbook(data).layout
        # Ein Szenario, sonst mischen sich die Positionssätze mehrerer Mengenspalten
        for sl in layout.sheets:
            if len(sl.roles.qty) > 1:
                for i, q in enumerate(sl.roles.qty):
                    q.active = (i == 0)
        project = _xl.parse_excel(data, layout)
        for level in (0, 1):
            soll = dokument_reihenfolge(project, level)
            ist = [g["name"] for g in _import_gaeb_groups(project, level=level, alt_active={})]
            ok = soll == ist
            print(f"  {fname[:32]:34s} level={level} {len(ist):3d} Gruppen · {ok}")
            if not ok:
                i0 = next(i for i, (a, b) in enumerate(zip(soll, ist)) if a != b)
                check(False, f"{fname[:24]} level={level} ab Position {i0}: "
                             f"{soll[i0][:36]!r} statt {ist[i0][:36]!r}")
            else:
                check(True, "")


def test_gaeb_unveraendert():
    """Bei GAEB darf sich nichts ändern — dort sind die Ordnungszahlen auf feste
    Breite aufgefüllt und sortierten schon vorher richtig. Die drei bekannten
    Abweichungen sind Dateien, in denen eine Elterngruppe erst NACH ihren
    Untergruppen auftaucht; dort ist die Ordnungszahl richtiger als das Dokument."""
    print("\n=== GAEB: Reihenfolge unverändert")
    dateien = sorted(set(glob.glob("infos/*.x8*") + glob.glob("infos/*.X8*")
                         + glob.glob("infos/*.D83")))
    geprueft, eltern_nach_kind = 0, []
    for f in dateien:
        try:
            project = parse_gaeb(f)
        except Exception:
            continue                     # kaputte Datei ist hier nicht das Thema
        if not project.items:
            continue
        geprueft += 1
        for level in (0, 1):
            soll = dokument_reihenfolge(project, level)
            ist = [g["name"] for g in _import_gaeb_groups(project, level=level, alt_active={})]
            if soll != ist:
                eltern_nach_kind.append((os.path.basename(f)[:30], level))
    print(f"  {geprueft} Dateien · {len(eltern_nach_kind)} Abweichungen: {eltern_nach_kind}")
    check(geprueft >= 8, f"zu wenige GAEB-Dateien geprüft: {geprueft}")
    check(len(eltern_nach_kind) == 3,
          f"3 bekannte Abweichungen erwartet (Elterngruppe nach Kind), "
          f"sind {len(eltern_nach_kind)}: {eltern_nach_kind}")


def test_positionen_in_gruppe():
    """Innerhalb einer Gruppe müssen die Positionen in Dateireihenfolge stehen."""
    print("\n=== Positionen innerhalb einer Gruppe")
    data = open(os.path.join("infos", EXCEL[0]), "rb").read()
    project = _xl.parse_excel(data, _xl.probe_workbook(data).layout)
    groups = _import_gaeb_groups(project, level=0, alt_active={})
    geprueft = 0
    for g in groups:
        zeilen = [int(b["primary"]["item_id"].split("-")[1]) for b in g["blocks"]
                  if not b.get("has_alt")]
        if len(zeilen) < 2:
            continue
        geprueft += 1
        check(zeilen == sorted(zeilen),
              f"Gruppe {g['name'][:30]!r}: Zeilen nicht aufsteigend: {zeilen[:8]}")
    print(f"  {geprueft} Gruppen mit mehreren Positionen geprüft")
    check(geprueft >= 5, f"zu wenige Gruppen geprüft: {geprueft}")


def test_zz_alle_pruefungen_ok():
    assert not _fails, f"{len(_fails)} Prüfung(en) fehlgeschlagen: " + "; ".join(_fails)


if __name__ == "__main__":
    test_oz_key()
    test_excel_reihenfolge()
    test_gaeb_unveraendert()
    test_positionen_in_gruppe()
    print("\n" + "=" * 60)
    print(f"{_oks} Prüfungen ok, {len(_fails)} fehlgeschlagen")
    if _fails:
        for f in _fails:
            print(f"  - {f}")
        sys.exit(1)
