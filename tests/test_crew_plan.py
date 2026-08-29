"""Regressionstest für die Personalplanung (Crew-Matrix).

Der Prüfstein ist die echte Excel-Personalliste des Schneider-Electric-Projekts
(HMI 2026, `infos/Personal_Kalkulation_Schneider_Electric@HMI_2026.xlsx`): dieselben
18 Zeilen, dieselben Tage. Erwartet werden die Tagessummen der Excel-Zeile „Gesamt"
— aber die *korrekte* Gesamtsumme: in der Excel summieren 12 von 22 Zeilen nur bis
Spalte Z statt AF und verlieren dadurch 9 Manntage (siehe docs/PERSONALPLANUNG.md).

Bewusst ohne pytest — wie die übrigen Tests hier direkt aufrufbar:

    .venv/Scripts/python.exe tests/test_crew_plan.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# DB-Pfad VOR dem Import von db setzen — der Modulpfad wird beim Import ausgewertet.
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="crewtest_"), "test.db")
os.environ["DB_PATH"] = _TMP_DB

import db as _db
from crew_plan import (DEFAULT_HOTEL_ID, DEFAULT_RK_ID, EJ_NK_VORLAUF,
                       EJ_TAG_BEGINN, EJ_TAG_ENDE,
                       MAX_DAYS, CrewPlan, bookings,
                       default_phases, detect_schedule, format_number,
                       lv_row_candidates, new_plan, parse_day, parse_number,
                       schedule_from_project)

_fails: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {label}: {got}" + ("" if ok else f"  ERWARTET {want}"))
    if not ok:
        _fails.append(label)


def check_true(label: str, cond) -> None:
    check(label, bool(cond), True)


# ─── Die Schneider-Liste als Planungsdaten ───────────────────────────────────
# Tagesindex 0 = 31.03.2026, 1..30 = 01.–30.04.2026.
# (Menüpunkt, Label, Tagessatz, Hotelnächte, RK-Fahrten, {Tagesindex: Personen})
# Spesen stehen hier nicht: sie werden aus dem gewählten Satz der Planung gerechnet
# (voller Satz je Nacht, halber je Tag ohne Übernachtung). In der Excel war das die
# Wahl zwischen 16 und 32 je Zeile, von Hand getroffen.
SCHNEIDER = [
    ("Projektleitung", "Projektleiter", 700, 13, 2,
     {0: 1, 1: 1, 5: 1, 6: 1, 7: 1, 8: 1, 15: 1, 16: 1, 17: 1, 18: 1, 19: 1, 29: 1, 30: 1}),
    ("Traverse", "Rigger", 570, 0, 0, {0: 3, 1: 3, 29: 2, 30: 2}),
    ("Traverse", "Veranstaltungstechniker / Grounder", 380, 0, 0,
     {0: 4, 1: 4, 29: 3, 30: 3}),
    ("Kabel", "Veranstaltungstechniker", 500, 0, 0, {4: 1, 5: 1}),
    ("Licht", "Lichttechniker", 500, 0, 0,
     {0: 5, 1: 5, 17: 2, 18: 2, 19: 1, 24: 1, 29: 4, 30: 4}),
    ("Licht", "Lichttechniker System", 500, 0, 0, {0: 1, 1: 1}),
    ("Licht", "Licht-Operator", 600, 5, 1,
     {17: 1, 18: 1, 19: 1, 20: 1, 21: 1, 22: 1, 23: 1, 24: 1}),
    ("Licht", "Veranstaltungstechniker", 500, 0, 0,
     {0: 3, 1: 3, 20: 1, 21: 1, 22: 1, 23: 1, 24: 1, 29: 3, 30: 3}),
    ("Displays", "Medientechniker Montage", 600, 0, 0,
     {16: 4, 17: 4, 24: 6, 25: 2}),
    ("Displays", "Medientechniker", 600, 0, 0, {16: 2, 17: 2, 18: 1, 19: 1}),
    ("Displays", "Hilfskraft", 380, 0, 0, {16: 2, 17: 2, 24: 2, 25: 2}),
    ("Displays", "Netzwerktechniker", 650, 0, 0,
     {4: 1, 5: 1, 16: 1, 17: 1, 18: 1, 19: 1, 20: 1, 21: 1, 22: 1, 23: 1, 24: 1}),
    ("LED-Wände", "Pandoras-Operator", 750, 16, 3,
     {16: 2, 17: 2, 18: 1, 19: 1, 20: 1, 21: 1, 22: 1, 23: 1, 24: 1}),
    ("LED-Wände", "LED-Wand Techniker", 600, 0, 0,
     {5: 3, 6: 3, 7: 3, 8: 3, 24: 3, 25: 3}),
    ("LED-Wände", "Aquilon-Operator", 600, 5, 1,
     {19: 1, 20: 1, 21: 1, 22: 1, 23: 1, 24: 1}),
    ("LED-Wände", "Veranstaltungstechniker", 500, 0, 0,
     {5: 1, 6: 1, 7: 1, 8: 1, 16: 1, 17: 1, 18: 1, 24: 2, 25: 2}),
    ("Tontechnik", "Tontechniker", 500, 8, 0,
     {0: 1, 1: 1, 17: 1, 18: 1, 19: 1, 24: 1, 25: 1, 30: 1}),
    ("Tontechnik", "Ton-Operator", 600, 5, 1,
     {0: 1, 1: 1, 17: 1, 18: 1, 19: 1, 20: 1, 21: 1, 22: 1, 23: 1, 24: 1}),
]

# Zeile „Gesamt" der Excel (Spalten B..AF), 31 Werte.
EXCEL_TAGESSUMMEN = [19, 19, 0, 0, 2, 7, 5, 5, 5, 0, 0, 0, 0, 0, 0, 1, 13, 18, 10,
                     9, 6, 6, 6, 6, 21, 10, 0, 0, 0, 13, 14]


def build_plan() -> CrewPlan:
    plan = new_plan("2026-03-31", "2026-04-30")
    keys = plan.day_keys()
    for menu, label, ts, hotel, rk, cells in SCHNEIDER:
        row = plan.add_row(label=label, resource_id=1, group_key=menu,
                           tagessatz=ts)
        row.hotel_naechte = hotel
        row.rk_anzahl = rk
        for idx, persons in cells.items():
            plan.set_cell(row.id, keys[idx], persons)
    return plan


# ─── 0. Zahlen lesen ─────────────────────────────────────────────────────────
# Der Punkt ist zweideutig: „1.250" sind tausendzweihundertfünfzig, „520.50" sind
# fünfhundertzwanzig fünfzig. Wer das verwechselt, kalkuliert mit dem Hundertfachen —
# genau das passierte in der ersten Fassung mit einem Tagessatz von 520.50.

print("\n[0] Zahlen aus Eingaben lesen")
for eingabe, erwartet in [
    ("520,50", 520.5), ("520.50", 520.5), ("520", 520.0),
    ("1.250,50", 1250.5), ("1,250.50", 1250.5),
    ("1.250", 1250.0), ("1.250.000", 1250000.0),
    ("  1 250,50  ", 1250.5), ("32 €", 32.0),
    ("0,5", 0.5), ("12.5", 12.5), ("", 0.0), ("-99", -99.0),
]:
    check(f"„{eingabe}“", parse_number(eingabe), erwartet)

check("Anzeige 520,5", format_number(520.5), "520,5")
check("Anzeige ohne Nullen", format_number(520.0), "520")
check("Anzeige 0", format_number(0), "0")


# ─── 1. Zeitachse ────────────────────────────────────────────────────────────

print("\n[1] Zeitachse")
plan = build_plan()
check("Tage 31.03.–30.04.", len(plan.day_keys()), 31)
check("erster Tag", plan.day_keys()[0], "2026-03-31")
check("letzter Tag", plan.day_keys()[-1], "2026-04-30")

verdreht = CrewPlan(date_from="2026-04-30", date_to="2026-03-31")
check("verdrehte Datumsangaben werden getauscht", len(verdreht.day_keys()), 31)

lang = CrewPlan(date_from="2026-01-01", date_to="2030-01-01")
check("Zeitachse gedeckelt", len(lang.day_keys()), MAX_DAYS)

kaputt = CrewPlan(date_from="", date_to="")
check("leere Daten ergeben keine Spalten", kaputt.day_keys(), [])


# ─── 2. Rechnung gegen die Excel ─────────────────────────────────────────────

print("\n[2] Rechnung gegen die Schneider-Excel")
tages = [plan.day_total(k) for k in plan.day_keys()]
check("Tagessummen wie Excel-Zeile Gesamt", tages, EXCEL_TAGESSUMMEN)

totals = plan.totals()
check("Zeilen", totals["rows"], 18)
check("Manntage", totals["manntage"], 195)
# Die Excel wies 117.406 € aus. Der Unterschied hat zwei Ursachen: die Formelfehler
# aus Abschnitt 1 (5.146 €) und das neue Nebenkostenmodell — Spesen je nach
# Übernachtung statt eines von Hand gewählten Satzes, Reisekosten als halber
# Tagessatz statt 250 € pauschal.
check("Summe", round(totals["summe"]), 122607)
check("Spesen gesamt", round(totals["spesen"]), 3872)
check("Hotel gesamt", round(totals["hotel"]), 7050)
check("Reisekosten gesamt", round(totals["rk"]), 2725)
check("Spitzenbesetzung", totals["peak"], 21)

# Die Excel weist 117.406 € aus. Die Differenz sind die Zeilen, deren Summenformel
# nur bis Spalte Z lief, plus der Reisekostensatz von 8 € statt 250 €.
pl = plan.rows[0]
check("Projektleiter Manntage", plan.manntage(pl), 13)
# 13 Tage, 13 Nächte → alle Tage voller Satz; RK 2 × 350 statt 2 × 250.
check("Projektleiter Spesen", round(plan.row_spesen(pl)), 13 * 32)
check("Projektleiter Hotel", round(plan.row_hotel(pl)), 13 * 150)
check("Projektleiter Reisekosten", round(plan.row_rk(pl)), 2 * 350)
check("Projektleiter Zeilensumme", round(plan.row_total(pl)), 12166)


# ─── 3. Tage außerhalb der Zeitachse ─────────────────────────────────────────

print("\n[3] Verkürzte Zeitachse")
eng = build_plan()
eng.set_range("2026-03-31", "2026-04-19")       # Show und Abbau abgeschnitten
check("nur noch Aufbautage", len(eng.day_keys()), 20)
check("Manntage sinken", eng.totals()["manntage"], sum(EXCEL_TAGESSUMMEN[:20]))
eng.set_range("2026-03-31", "2026-04-30")
check("zurückgezogen: Zellen sind wieder da", eng.totals()["manntage"], 195)


# ─── 4. Zellen setzen ────────────────────────────────────────────────────────

print("\n[4] Zellen")
p4 = new_plan("2026-04-01", "2026-04-03")
r4 = p4.add_row("Rigger", resource_id=13, tagessatz=570)
check_true("Zelle setzen", p4.set_cell(r4.id, "2026-04-01", 3))
check("Personenzahl", r4.cells["2026-04-01"], 3)
p4.set_cell(r4.id, "2026-04-01", 0)
check("0 löscht die Zelle", "2026-04-01" in r4.cells, False)
p4.set_cell(r4.id, "2026-04-02", 999)
check("Obergrenze greift", r4.cells["2026-04-02"], 99)
check("unbekannte Zeile", p4.set_cell(9999, "2026-04-02", 1), False)


# ─── 5. Serialisierung ───────────────────────────────────────────────────────

print("\n[5] Serialisierung")
kopie = CrewPlan.from_dict(plan.to_dict())
check("Zeilen erhalten", len(kopie.rows), 18)
check("Summe erhalten", round(kopie.totals()["summe"]), 122607)
check("Tagessummen erhalten", [kopie.day_total(k) for k in kopie.day_keys()],
      EXCEL_TAGESSUMMEN)
check("from_dict(None)", CrewPlan.from_dict(None), None)

# Kaputter Entwurf: next_row_id kleiner als vergebene IDs → neue Zeile würde eine
# bestehende überschreiben. Muss repariert werden.
kaputt = plan.to_dict()
kaputt["next_row_id"] = 1
repariert = CrewPlan.from_dict(kaputt)
neu = repariert.add_row("Test", resource_id=1)
check("next_row_id repariert", len([r for r in repariert.rows if r.id == neu.id]), 1)


# ─── 6. Phasenvorschlag ──────────────────────────────────────────────────────

print("\n[6] Phasen")
ph = default_phases("2026-03-31", "2026-04-30")
check("drei Phasen", [p.name for p in ph], ["Aufbau", "Veranstaltung", "Abbau"])
check("lückenlos", [(ph[0].day_to, ph[1].day_from), (ph[1].day_to, ph[2].day_from)],
      [("2026-04-26", "2026-04-27"), ("2026-04-28", "2026-04-29")])
check("kurzer Zeitraum", [p.name for p in default_phases("2026-04-01", "2026-04-02")],
      ["Veranstaltung"])
check("Phase eines Tages", plan.phase_of("2026-03-31").name, "Aufbau")


# ─── 7. Zeilen aus dem LV ────────────────────────────────────────────────────

print("\n[7] Zeilenvorschläge aus dem LV")


class _Item:
    def __init__(self, item_id, oz, desc, path, qty=1.0, unit="d"):
        self.item_id, self.oz, self.description = item_id, oz, desc
        self.category_path, self.qty, self.unit = path, qty, unit


class _Proj:
    def __init__(self, items):
        self.items = items


from matcher import Resource as _Resource


def _mr(obj):
    class M:
        matched = obj
    return M()


_res_person = _Resource(id=84, funktion="Licht-Operator", ressourcenart="Personal",
                        tagessatz=600, eigenkosten=540, satzname="Standard",
                        gaeb_synonyms=[])
_res_fahrzeug = _Resource(id=2, funktion="LKW 7,5t", ressourcenart="Fahrzeug",
                          tagessatz=150, eigenkosten=0, satzname="Standard",
                          gaeb_synonyms=[])

items = [
    _Item("i1", "03.05.02", "Light operator", ["Extra charges", "Show / operating crew"], 5.0),
    _Item("i2", "03.05.01", "Audio operator", ["Extra charges", "Show / operating crew"], 5.0),
    _Item("i3", "03.02.01", "Transportations", ["Extra charges", "Logistics"], 1.0),
    _Item("i4", "01.01.01", "Moving Light", ["Lighting"], 12.0),
]
matches = {
    "i1": _mr(_res_person),
    "i2": _mr(_Resource(id=85, funktion="Ton-Operator", ressourcenart="Personal",
                        tagessatz=600, eigenkosten=540, satzname="Standard",
                        gaeb_synonyms=[])),
    "i3": _mr(_res_fahrzeug),          # Fahrzeug → keine Personalzeile
    "i4": _mr(None),                    # kein Match
}
cands = lv_row_candidates(_Proj(items), matches)
check("nur Personalpositionen", [c["oz"] for c in cands], ["03.05.02", "03.05.01"])
check("Funktion als Zeilenname", cands[0]["funktion"], "Licht-Operator")
check("Tagessatz übernommen", cands[0]["tagessatz"], 600.0)

# Zwei Positionen auf dieselbe Ressource → zwei Positionsvorschläge (jede Position
# ist ein eigenes Zuordnungsziel). Dass daraus nur EINE Zeile wird, entscheidet
# crew_seed — siehe tests/test_crew_routes.py.
items.append(_Item("i5", "03.05.09", "Light operator (Reserve)",
                   ["Extra charges", "Show / operating crew"], 2.0))
matches["i5"] = _mr(_res_person)
cands2 = lv_row_candidates(_Proj(items), matches)
check("ein Vorschlag je Position", [c["oz"] for c in cands2],
      ["03.05.02", "03.05.01", "03.05.09"])
check("Funktion mitgeliefert", cands2[0]["funktion"], "Licht-Operator")


# ─── 8. Speichern und Laden ──────────────────────────────────────────────────

print("\n[8] Speichern und Laden (SQLite)")
_db.init_db()
with _db.get_conn() as conn:
    conn.execute("INSERT INTO projects (id, name, status) VALUES (7, 'Testprojekt', 'draft')")

_db.save_crew_plan(7, plan.to_dict(), user_name="test")
geladen = CrewPlan.from_dict(_db.load_crew_plan(7))
check("Zeilen aus der DB", len(geladen.rows), 18)
check("Summe aus der DB", round(geladen.totals()["summe"]), 122607)
check("Tagessummen aus der DB",
      [geladen.day_total(k) for k in geladen.day_keys()], EXCEL_TAGESSUMMEN)
check("Zugehörigkeit erhalten",
      [r.group_key for r in geladen.rows][:4],
      ["Projektleitung", "Traverse", "Traverse", "Kabel"])
check("Phasen erhalten", [p.name for p in geladen.phases],
      ["Aufbau", "Veranstaltung", "Abbau"])
# Auf welche Ressourcen die Nebenkosten laufen, gehört mit in den Entwurf — sonst
# bucht ein späterer Import woanders hin als die Planung anzeigt.
# Eine LV-Position, die zum Abschnitt gemacht wurde, ist der Fall, den man am
# ehesten wieder aufmacht — und genau der ging beim Laden verloren: gelesen wurde
# nur „batch".
_pm = CrewPlan(date_from="2026-03-09", date_to="2026-03-12")
_pm.positions = ["p1", "p2"]
_pm.set_pos_mode("p1", "menu")
_pm.set_pos_mode("p2", "batch")
_pm2 = CrewPlan.from_dict(_pm.to_dict())
check("Abschnitt bleibt Abschnitt", _pm2.pos_mode("p1"), "menu")
check("Sammelposition bleibt Sammelposition", _pm2.pos_mode("p2"), "batch")
check("und taucht als Abschnitt auf", _pm2.menu_keys(), ["p1"])

# Wird eine LV-Position zum Abschnitt, ist sie auch das nächstliegende Ziel für
# alles darunter — sie ist ja diese Leistung.
check("Abschnitt ist sein eigenes Ziel", _pm.menu_pos("p1"), "p1")
# Ein von Hand gesetztes Ziel wird dabei nicht überschrieben.
_pm.set_menu_pos("p1", "p2")
_pm.set_pos_mode("p1", "batch")
_pm.set_pos_mode("p1", "menu")
check("gesetztes Ziel bleibt", _pm.menu_pos("p1"), "p2")
# Ein eigener Menüpunkt hat kein Gegenstück im LV und bekommt deshalb keins.
_eig = _pm.add_custom_menu("Eigener")
_pm.set_pos_mode(_eig, "menu")
check("eigener Abschnitt ohne Ziel", _pm.menu_pos(_eig), "")

check("Nebenkosten-Ressourcen erhalten",
      (geladen.spesen_id, geladen.hotel_id, geladen.rk_id), (123, 37, 126))
check("und ihre Namen", geladen.hotel_name, "Hotelkosten")

# Zweites Speichern ersetzt, statt zu verdoppeln.
geladen.remove_row(geladen.rows[0].id)
_db.save_crew_plan(7, geladen.to_dict())
check("Ersetzen statt Anhängen", len(_db.load_crew_plan(7)["rows"]), 17)

check("ohne Planung: None", _db.load_crew_plan(999), None)
_db.save_crew_plan(7, None)
check("Planung gelöscht", _db.load_crew_plan(7), None)

# Projekt löschen räumt die Planung mit weg (sonst blieben Waisen zurück).
_db.save_crew_plan(7, plan.to_dict())
_db.delete_project(7)
with _db.get_conn() as conn:
    rest = conn.execute("SELECT COUNT(*) c FROM crew_cells WHERE project_id=7").fetchone()["c"]
check("delete_project räumt auf", rest, 0)


# ─── 9. Terminplan aus dem LV lesen ──────────────────────────────────────────

print("\n[9] Terminplan aus den Vorbemerkungen")


def _phasen(text, jahr=None):
    s = detect_schedule(text, jahr)
    return [(p.name, p.day_from, p.day_to) for p in s.get("phases", [])]


# Das echte LV: der Terminplan steht dort im Klartext, zweisprachig, mit
# Einzelschritten unter der Gesamtzeit.
import gaeb_parser as _g

_LV = os.path.join(os.path.dirname(__file__), "..", "infos",
                   "251127_Schneider Electric_Hannover Messe26_AVL_Tender.x83")
if os.path.exists(_LV):
    _proj = _g.parse_gaeb(open(_LV, "rb").read())
    _s = schedule_from_project(_proj)
    # „Rehearsel Stage 19.04." ist ein eigener Termin (Proben) und schneidet den
    # letzten Aufbautag ab — nicht bloß eine Unterzeile des Terminplans.
    check("Schneider-LV: Phasen",
          [(p.name, p.day_from, p.day_to) for p in _s.get("phases", [])],
          [("Aufbau", "2026-03-31", "2026-04-18"),
           ("Proben", "2026-04-19", "2026-04-19"),
           ("Veranstaltung", "2026-04-20", "2026-04-24"),
           ("Abbau", "2026-04-25", "2026-04-30")])
    check("Schneider-LV: Zeitraum",
          (_s.get("date_from"), _s.get("date_to")), ("2026-03-31", "2026-04-30"))
    # Genau der Kopf der Excel-Personalliste: Aufbau 31.03.-19.04.,
    # Veranstaltung 20.04.-24.04., Abbau 25.04.-30.04.
else:
    print("  (LV-Datei nicht vorhanden — übersprungen)")

check("deutsch, mit „bis“", _phasen(
    "Aufbau 10.05.2026 bis 12.05.2026. Veranstaltung 13.05. - 14.05.2026. "
    "Abbau 15.05.2026 bis 16.05.2026."),
    [("Aufbau", "2026-05-10", "2026-05-12"),
     ("Veranstaltung", "2026-05-13", "2026-05-14"),
     ("Abbau", "2026-05-15", "2026-05-16")])

check("zweistellige Jahre", _phasen(
    "Installation 01.06.26 till 03.06.26 Dismantling 08.06.26 till 09.06.26"),
    [("Aufbau", "2026-06-01", "2026-06-03"),
     ("Veranstaltung", "2026-06-04", "2026-06-07"),      # Lücke = Laufzeit
     ("Abbau", "2026-06-08", "2026-06-09")])

# Abbau beginnt am Abend des letzten Showtags — für eine Tagesmatrix gehört der Tag
# zur Veranstaltung, der Abbau rückt nach hinten.
check("Abbau am letzten Showtag", _phasen(
    "Aufbau 01.07.2026 - 05.07.2026 Veranstaltung 06.07. - 08.07.2026 "
    "Abbau 08.07.2026 ab 18:00 bis 10.07.2026"),
    [("Aufbau", "2026-07-01", "2026-07-05"),
     ("Veranstaltung", "2026-07-06", "2026-07-08"),
     ("Abbau", "2026-07-09", "2026-07-10")])

# Terminplantabellen listen unter der Gesamtzeit die Einzelschritte auf. Für die
# Phase „Aufbau" gewinnt die Gesamtzeit. Die Probe am letzten Aufbautag ist dagegen
# ein eigener Termin und schneidet diesen Tag ab.
check("Einzelschritte im Aufbau, Probe als eigener Termin", _phasen(
    "Installation 01.03.2026 till 20.03.2026 "
    "Build Up Cabeling 02.03.26 04.03.26 Build Up LED 05.03.26 08.03.26 "
    "Rehearsal 20.03.26 20.03.26 "
    "Execution / event 21.03. - 23.03.2026 "
    "Dismantling 24.03.2026 till 26.03.2026"),
    [("Aufbau", "2026-03-01", "2026-03-19"),
     ("Proben", "2026-03-20", "2026-03-20"),
     ("Veranstaltung", "2026-03-21", "2026-03-23"),
     ("Abbau", "2026-03-24", "2026-03-26")])

# Ein fremder Zeitraum MITTEN in einer Phase ist eine Unterzeile der Tabelle. Ihn
# als Phase zu nehmen würde den Aufbau zerreißen und ein Loch hinterlassen.
check("Termin mitten im Block wird verworfen", _phasen(
    "Aufbau 01.03.2026 bis 20.03.2026 "
    "Rehearsal 05.03.2026 - 06.03.2026 "
    "Veranstaltung 21.03. - 23.03.2026"),
    [("Aufbau", "2026-03-01", "2026-03-20"),
     ("Veranstaltung", "2026-03-21", "2026-03-23")])

# „Hall Opening" ist die Hallenöffnungszeit, nicht die Veranstaltung. Sie liegt
# mitten im Aufbau — würde sie als Phase gelesen, käme Unsinn heraus.
check("Hallenöffnungszeit ist keine Veranstaltung", _phasen(
    "Installation 31.03.2026 till 19.04.2026 "
    "Hall Opening from the 10.04. - 19.04. Halls open 24 h "
    "Execution / event 20.04. - 24.04.2026 "
    "Dismantling 25.04.2026 till 30.04.2026"),
    [("Aufbau", "2026-03-31", "2026-04-19"),
     ("Veranstaltung", "2026-04-20", "2026-04-24"),
     ("Abbau", "2026-04-25", "2026-04-30")])

# Unplausibles wird verworfen — kein Vorschlag ist besser als ein falscher.
check("Abbau vor Aufbau wird verworfen", _phasen(
    "Abbau 01.05.2026 bis 02.05.2026 Aufbau 10.05.2026 bis 12.05.2026"), [])
check("Text ohne Daten", _phasen("Aufbau und Abbau nach Absprache."), [])
check("leerer Text", detect_schedule(""), {})
check("Daten ohne Stichwort", _phasen("Angebotsfrist 01.02.2026 - 15.02.2026"), [])

# Nur eine Phase erkannt: das ist erlaubt, aber es wird nichts dazugedichtet.
check("nur Aufbau", _phasen("Aufbau 01.09.2026 bis 05.09.2026"),
      [("Aufbau", "2026-09-01", "2026-09-05")])

check("Unsinnsdaten (32.13.) ignoriert",
      _phasen("Aufbau 32.13.2026 bis 40.99.2026"), [])


# ─── 10. Abgleich mit dem Matching ───────────────────────────────────────────

print("\n[10] Abgleich: Ziele, Abschnitte und Vorbelegung")

p10 = new_plan("2026-04-01", "2026-04-10")
kand = [
    {"item_id": "a", "funktion": "Projektleiter", "resource_id": 14,
     "tagessatz": 700, "eigenkosten": 630, "qty": 1.0, "unit": "psch"},
    {"item_id": "b", "funktion": "Ton-Operator", "resource_id": 85,
     "tagessatz": 600, "eigenkosten": 540, "qty": 5.0, "unit": "d"},
    {"item_id": "c", "funktion": "Licht-Operator", "resource_id": 84,
     "tagessatz": 600, "eigenkosten": 540, "qty": 5.0, "unit": "d"},
    {"item_id": "d", "funktion": "Licht-Operator", "resource_id": 84,
     "tagessatz": 600, "eigenkosten": 540, "qty": 50.0, "unit": "h"},
]
p10.sync_positions([c["item_id"] for c in kand])
p10.sync_rows(kand)

check("alle gematchten Positionen sind Ziele", p10.positions, ["a", "b", "c", "d"])
# Eine LV-Position wird NICHT automatisch zur Überschrift — sonst hätte die Matrix
# so viele Abschnitte wie das LV Personalpositionen.
check("aber keine ist ein Abschnitt", p10.menu_keys(), [])
check("eine Zeile je Ressource", [r.label for r in p10.rows],
      ["Projektleiter", "Ton-Operator", "Licht-Operator"])

# Die geforderte Menge steht am ersten Tag und ist der Position zugeordnet, damit
# der Soll/Ist-Abgleich sofort stimmt. „psch" und „h" sagen nichts über Manntage.
erste = p10.day_keys()[0]
check("Menge am ersten Tag",
      {r.label: r.cells.get(erste, 0) for r in p10.rows},
      {"Projektleiter": 0, "Ton-Operator": 5, "Licht-Operator": 5})
check("und gleich zugeordnet",
      {r.label: r.assign.get(erste, "") for r in p10.rows},
      {"Projektleiter": "", "Ton-Operator": "b", "Licht-Operator": "c"})
check("Manntage je Position",
      {k: v["mt"] for k, v in p10.position_stats().items()}, {"b": 5, "c": 5})

# Rechtsklick macht eine Position zum Abschnitt — und zurück, ohne Spuren.
p10.set_pos_mode("b", "menu")
check("zum Abschnitt", p10.menu_keys(), ["b"])
p10.set_pos_mode("b", "batch")
check("und zurück", (p10.menu_keys(), p10.pos_modes), ([], {}))

# Eigene Menüpunkte sind immer Abschnitte.
key = p10.add_custom_menu("Projektleitung")
check("eigener Menüpunkt ist ein Abschnitt", p10.menu_keys(), [key])
check("Titel", p10.menu_title(key), "Projektleitung")

# Zweiter Abgleich ändert nichts (kein Aufblähen bei jedem Aufbau der Ansicht)
vorher = p10.to_dict()
p10.sync_positions([c["item_id"] for c in kand])
p10.sync_rows(kand)
check("Abgleich ist wiederholbar", p10.to_dict(), vorher)


# ─── 11. Bereich füllen ──────────────────────────────────────────────────────
# „Drei Rigger, die ganze Aufbauwoche" ist der häufigste Handgriff der Planung.

print("\n[11] Bereich füllen")

p11 = new_plan("2026-04-01", "2026-04-10")
a = p11.add_row("Rigger", 13, tagessatz=570)
b = p11.add_row("Lichttechniker", 6, tagessatz=520)
p11.add_position("pos")

n = p11.fill_cells([a.id, b.id], "2026-04-02", "2026-04-06", 3, "pos")
check("zwei Zeilen × fünf Tage", n, 10)
check("Manntage", p11.totals()["manntage"], 30)
check("Zuordnung mitgesetzt", len(a.assign), 5)
check("keine offenen Tage", p11.unassigned(), [])

# Verdrehter Bereich wird getauscht
n = p11.fill_cells([a.id], "2026-04-09", "2026-04-08", 1, "pos")
check("verdrehter Bereich", sorted(k[8:] for k in a.cells if a.cells[k] == 1), ["08", "09"])

# 0 räumt auf — Zelle UND Zuordnung, sonst bliebe ein Band ohne Besetzung stehen
p11.fill_cells([b.id], "2026-04-02", "2026-04-06", 0, "pos")
check("geleert", b.cells, {})
check("Zuordnung mit weg", b.assign, {})

# Tage außerhalb der Zeitachse werden nicht angefasst
n = p11.fill_cells([a.id], "2027-01-01", "2027-01-05", 5, "pos")
check("außerhalb der Zeitachse", n, 0)
check("unbekannte Zeile", p11.fill_cells([9999], "2026-04-02", "2026-04-03", 1), 0)


# ─── 12. Abschnitte, Standard-Positionen und eigene Menüpunkte ───────────────

print("\n[12] Abschnitte und Standard-Positionen")

p12 = new_plan("2026-08-01", "2026-08-10")
p12.add_position("lv1")
p12.add_position("lv2")
licht = p12.add_custom_menu("Licht")

# Ein selbst angelegter Menüpunkt ist eine Überschrift der Übersicht halber. Er hat
# kein Gegenstück im LV, also kann nichts darauf gebucht werden.
check("Abschnitte", p12.menu_keys(), [licht])
check("Zuordnungsziele ohne eigene Menüpunkte", p12.target_keys(), ["lv1", "lv2"])
check("eigener Menüpunkt ist kein Ziel", p12.covers(licht), False)
check("LV-Position ist ein Ziel", p12.covers("lv1"), True)

r = p12.add_row("Rigger", 13, group_key=licht, tagessatz=570)
check("Zeile steht im Abschnitt", p12.home_of(r), licht)

# Standard-Position: was in diesem Abschnitt eingetragen wird, läuft darauf. Genau
# eine je Abschnitt — mehrere wären nicht eindeutig, die erste hätte immer gewonnen.
check("Standard setzen", p12.set_menu_pos(licht, "lv1"), True)
check("Standard der Zeile", p12.default_pos_for(r), "lv1")
check("eigener Menüpunkt kann kein Standard sein",
      p12.set_menu_pos(licht, licht), False)
check("unbekannter Abschnitt", p12.set_menu_pos("eigen:99", "lv1"), False)

# Dieselbe Position darf Standard in mehreren Abschnitten sein — eine
# Installations-Pauschale ist oft für Licht UND Ton zuständig.
ton = p12.add_custom_menu("Ton")
p12.set_menu_pos(ton, "lv1")
check("Position in zwei Abschnitten",
      (p12.menu_pos(licht), p12.menu_pos(ton)), ("lv1", "lv1"))

# Ein zweites Setzen ersetzt, statt anzuhängen.
p12.set_menu_pos(licht, "lv2")
check("ersetzt", p12.menu_pos(licht), "lv2")
check("und greift sofort", p12.default_pos_for(r), "lv2")
p12.set_menu_pos(licht, "")
check("herausgenommen", p12.menu_pos(licht), "")

# Position aus der Planung nehmen räumt die Standards mit auf
p12.set_menu_pos(licht, "lv2")
p12.remove_position("lv2")
check("Standards aufgeräumt", p12.menu_positions.get(licht), None)
check("Zeile ohne Standard", p12.default_pos_for(r), "")

# Zeile ohne Abschnitt hat keinen Standard
r2 = p12.add_row("Lichttechniker", 6, tagessatz=520)
check("Zeile ohne Abschnitt", p12.default_pos_for(r2), "")

# Und durch die DB
plan_dict = p12.to_dict()
check("Standards serialisiert", plan_dict["menu_positions"], {ton: "lv1"})
check("und wieder gelesen",
      CrewPlan.from_dict(plan_dict).menu_pos(ton), "lv1")
# Ältere Entwürfe hielten hier eine Liste — die erste galt ohnehin.
_alt = dict(plan_dict, menu_positions={ton: ["lv1", "lv2"]})
check("alte Liste wird gelesen", CrewPlan.from_dict(_alt).menu_pos(ton), "lv1")


# ─── 13. Zuordnung nachreichen ───────────────────────────────────────────────
# Die natürliche Reihenfolge beim Planen: erst die Besetzung tippen, die Zuordnung
# später. Sonst müsste man vor der ersten Zahl wissen, worauf sie läuft.

print("\n[13] Offene Tage nachträglich zuordnen")

p13 = new_plan("2026-04-01", "2026-04-08")
p13.add_position("x1")
p13.add_position("x2")
ra = p13.add_row("Rigger", 13, tagessatz=570)
rb = p13.add_row("Lichttechniker", 6, tagessatz=520)
for d in ("2026-04-02", "2026-04-03"):
    p13.set_cell(ra.id, d, 2)
for d in ("2026-04-03", "2026-04-06"):
    p13.set_cell(rb.id, d, 2)
check("alles offen", sum(u["mt"] for u in p13.unassigned()), 8)

check("nachgezogen", p13.assign_open("x1"), 4)
check("nichts mehr offen", p13.unassigned(), [])
check("Manntage auf der Position", p13.position_stats()["x1"]["mt"], 8)

# Ein bewusst gesetzter Tag darf beim Nachziehen nicht eingesammelt werden.
p13.set_cell(ra.id, "2026-04-07", 1)
p13.assign_days(ra.id, "2026-04-07", "2026-04-07", "x2")
p13.set_cell(rb.id, "2026-04-07", 1)
check("nur die offenen", p13.assign_open("x1"), 1)
check("Ausnahme bleibt", ra.assign["2026-04-07"], "x2")
check("die andere Zeile bekommt x1", rb.assign["2026-04-07"], "x1")

# Auf eine Zeile begrenzt
p13.set_cell(ra.id, "2026-04-08", 1)
p13.set_cell(rb.id, "2026-04-08", 1)
check("nur diese Zeile", p13.assign_open("x2", row_id=ra.id), 1)
check("Zeile A", ra.assign.get("2026-04-08"), "x2")
check("Zeile B unberührt", rb.assign.get("2026-04-08"), None)

# Leere Tage werden nicht zugeordnet — ohne Besetzung gibt es nichts zu buchen.
leer = new_plan("2026-04-01", "2026-04-03")
leer.add_position("x1")
leer.add_row("Rigger", 13)
check("ohne Besetzung nichts", leer.assign_open("x1"), 0)


# ─── 14. Bereich über mehrere Zeilen ─────────────────────────────────────────
# Jede Zeile bekommt ihre eigene Standardposition. Die der ersten für alle zu nehmen
# wäre falsch: ein Rigger und ein Lichttechniker gehören selten auf dieselbe Position.

print("\n[14] Bereichsfüllen über mehrere Zeilen")

p14 = new_plan("2026-04-01", "2026-04-08")
for pid in ("rig", "lit", "ton"):
    p14.add_position(pid)
zeilen = [p14.add_row("Rigger", 13, group_key="rig", tagessatz=570),
          p14.add_row("Lichttechniker", 6, group_key="lit", tagessatz=520),
          p14.add_row("Tontechniker", 1, group_key="ton", tagessatz=520)]
ids14 = [z.id for z in zeilen]

p14.fill_cells(ids14, "2026-04-02", "2026-04-04", 2)      # item_id=None
check("jede Zeile auf ihre eigene Position",
      [z.assign.get("2026-04-03") for z in zeilen], ["rig", "lit", "ton"])
check("Manntage je Position",
      {k: v["mt"] for k, v in p14.position_stats().items()},
      {"rig": 6, "lit": 6, "ton": 6})

# Eine ausdrücklich gewählte Position gilt dagegen für alle.
p14.fill_cells(ids14, "2026-04-06", "2026-04-06", 1, "rig")
check("Auswahl gilt für alle",
      [z.assign.get("2026-04-06") for z in zeilen], ["rig", "rig", "rig"])

# Leerer String heißt ausdrücklich: keine Zuordnung.
p14.fill_cells(ids14, "2026-04-07", "2026-04-07", 1, "")
check("leeres Ziel ordnet nicht zu",
      [z.assign.get("2026-04-07") for z in zeilen], [None, None, None])

# 0 Personen räumt Zelle und Zuordnung.
p14.fill_cells(ids14, "2026-04-02", "2026-04-04", 0)
check("geleert", [z.cells.get("2026-04-03") for z in zeilen], [None, None, None])
check("und gelöst", [z.assign.get("2026-04-03") for z in zeilen], [None, None, None])


# ─── 15. Buchungen für Easyjob ─────────────────────────────────────
# Was in Easyjob landet, muss auf den Cent dasselbe sein wie das, was die Matrix
# anzeigt. Eine Abweichung fällt sonst erst beim Abgleich der Angebotssumme auf,
# und dann sucht sie niemand mehr in den Rundungen einer Verteilung.
print(chr(10) + "── 15. Buchungen für Easyjob ──")

_bp = CrewPlan(date_from="2026-03-09", date_to="2026-03-14")
_bp.positions = ["a", "b"]
_bp.spesen_satz, _bp.hotel_satz = 32.0, 150.0
_br = _bp.add_row("Ton-Operator", 501)
for _d, _n in [("2026-03-09", 2), ("2026-03-10", 2), ("2026-03-11", 1),
               ("2026-03-13", 1), ("2026-03-14", 1)]:
    _bp.set_cell(_br.id, _d, _n)
_bp.assign_days(_br.id, "2026-03-09", "2026-03-11", "a")
_bp.assign_days(_br.id, "2026-03-13", "2026-03-14", "b")
_br.tagessatz, _br.eigenkosten, _br.hotel_naechte, _br.rk_anzahl = 600.0, 400.0, 4, 2

_bs = bookings(_bp)
_tage = [b for b in _bs if b.kind == "tage"]

# Ein Block endet, wo die Position oder die Besetzung wechselt. Die Kopfzahl steht
# in `count` und landet in Easyjob in Quantity — zwei Leute an zwei Tagen sind EINE
# Zeile mit Anzahl 2, nicht zwei Zeilen.
check("Tagesblöcke",
      [(b.item_id, b.day_from, b.day_to, b.count, b.days) for b in _tage],
      [("a", "2026-03-09", "2026-03-10", 2, 2.0),
       ("a", "2026-03-11", "2026-03-11", 1, 1.0),
       ("b", "2026-03-13", "2026-03-14", 1, 2.0)])
# Easyjob rechnet TotalPrice = Quantity × Tage × Satz.
check("Preis mal Kopfzahl", _tage[0].total, 2 * 2 * 600.0)
# Uhrzeiten wie im Testsystem abgelesen: 08:00 bis 18:00, nicht Mitternacht.
check("Einsatzzeiten", (_tage[0].start_dt().hour, _tage[0].end_dt().hour),
      (EJ_TAG_BEGINN, EJ_TAG_ENDE))
check("Ende am letzten Tag", _tage[0].end_dt().strftime("%Y-%m-%d"), "2026-03-10")
# Die Lücke am 12. steht in keinem Block: gebucht wird, was besetzt ist.
check("Lücke bleibt Lücke",
      any(b.day_from <= "2026-03-12" <= b.day_to for b in _tage), False)
check("Manntage vollständig",
      sum(b.count * b.days for b in _tage), float(_bp.manntage(_br)))
check("eine Zeile je Block", len(_tage), 3)
check("Eigenkosten mitgegeben", {b.fixed_cost for b in _tage}, {400.0})

def _summe(kind):
    return round(sum(b.total for b in _bs if b.kind == kind), 2)

check("Tageskosten", _summe("tage"), round(_bp.manntage(_br) * _br.tagessatz, 2))
check("Spesen", _summe("spesen"), round(_bp.row_spesen(_br), 2))
check("Hotel", _summe("hotel"), round(_bp.row_hotel(_br), 2))
check("Reisekosten", _summe("reise"), round(_bp.row_rk(_br), 2))
check("Summe wie in der Zeile", round(sum(b.total for b in _bs), 2),
      round(_bp.row_total(_br), 2))

# Nebenkosten gehören auf eigene Ressourcen — nicht als Zuschlag auf den Tagessatz
# der Techniker, sonst sind sie später weder auswertbar noch abrechenbar.
# Preis je Nacht: der der Planung, solange die Zeile keinen eigenen trägt. Meist
# übernachtet die Crew im selben Haus — abweichen soll aber gehen, ohne dass die
# übrigen Zeilen davon wissen müssen.
check("Preis aus der Planung", _bp.hotel_satz_of(_br), 150.0)
_br.hotel_satz = 210.0
check("eigener Preis der Zeile", _bp.hotel_satz_of(_br), 210.0)
check("Hotel neu gerechnet", _bp.row_hotel(_br), 4 * 210.0)
check("und in der Buchung",
      {b.day_pay for b in bookings(_bp) if b.kind == "hotel"}, {210.0})
_br.hotel_satz = 0.0
check("zurück auf den Preis der Planung", _bp.row_hotel(_br), 4 * 150.0)

# Reisen genauso: vorbelegt mit einem halben Tagessatz der Zeile — ein Rigger reist
# günstiger als ein Lichtdesigner —, überschreibbar für den Flug statt der Bahn.
check("Vorgabe halber Tagessatz", _bp.rk_satz_of(_br), 300.0)
_br.rk_satz = 480.0
check("eigener Preis je Reise", _bp.row_rk(_br), 2 * 480.0)
check("und in der Buchung",
      {b.day_pay for b in bookings(_bp) if b.kind == "reise"}, {480.0})
_br.rk_satz = 0.0
check("zurück auf den halben Tagessatz", _bp.row_rk(_br), 2 * 300.0)

check("eigene Ressource für Hotel",
      {b.resource_id for b in _bs if b.kind == "hotel"}, {DEFAULT_HOTEL_ID})
check("eigene Ressource für Reisen",
      {b.resource_id for b in _bs if b.kind == "reise"}, {DEFAULT_RK_ID})
check("eigene Ressource für Spesen",
      {b.resource_id for b in _bs if b.kind == "spesen"}, {_bp.spesen_id})
check("Tageskosten auf der Person",
      {b.resource_id for b in _tage}, {501})
# Zwei Spesenzeilen: halber Satz für Tage ohne Übernachtung, voller mit. Als eine
# Zeile mit einem Mischsatz wäre in Easyjob nicht mehr erkennbar, wie sie zustande
# kommt.
check("Spesen nach Satz getrennt",
      sorted({b.day_pay for b in _bs if b.kind == "spesen"}), [16.0, 32.0])

# Ohne Nebenkosten-Position werden sie anteilig nach Manntagen verteilt — 5 zu 2.
# Verteilt wird die Menge, nicht der Betrag: 7 Manntage, 4 Nächte, also 2,86 zu 1,14.
# Der Rundungsrest liegt auf dem größten Anteil, damit die Summe exakt bleibt —
# fehlende Cents sucht später niemand mehr in einer Verteilung.
_hn = {b.item_id: b.days for b in _bs if b.kind == "hotel"}
check("Nächte anteilig verteilt", _hn, {"a": 2.86, "b": 1.14})
check("Nächte vollständig", round(sum(_hn.values()), 2), 4.0)

# Mit gesetzter Nebenkosten-Position genau eine Zeile je Posten — keine
# Bruchteilsnächte, die in Easyjob niemand lesen will.
_bp.menu_nk_pos[_bp.home_of(_br)] = "b"
_bs2 = bookings(_bp)
check("Nebenkosten gebündelt",
      [b.item_id for b in _bs2 if b.kind in ("hotel", "reise")], ["b", "b"])
check("Betrag unverändert", round(sum(b.total for b in _bs2), 2),
      round(_bp.row_total(_br), 2))
_bp.menu_nk_pos.clear()

# Nebenkosten liegen nicht über dem Einsatzzeitraum, sondern auf einem eigenen Tag
# davor: über die ganze Spanne gezogen legen sie sich in der Personaldisposition quer
# über alles und verdecken, wer wann wirklich arbeitet.
_nk = [b for b in _bs if b.kind != "tage"]
_erster = min(b.day_from for b in _tage)
check("Nebenkosten alle am selben Tag", len({b.day_from for b in _nk}), 1)
check("und der liegt davor", _nk[0].day_from < _erster, True)
check("genau zwei Tage",
      (parse_day(_erster) - parse_day(_nk[0].day_from)).days, EJ_NK_VORLAUF)
check("eintägig", [b.day_from == b.day_to for b in _nk], [True] * len(_nk))
# Die Menge steckt in `days`, nicht in `count`: Quantity ist ganzzahlig, und die
# anteilige Verteilung ergibt Bruchteile.
check("Nebenkosten ohne Kopfzahl", {b.count for b in _nk}, {1})

# Eine Zeile ohne Zuordnung darf nichts buchen: sonst lägen Kosten auf einer
# Position, die niemand gewählt hat.
_bp2 = CrewPlan(date_from="2026-03-09", date_to="2026-03-10")
_bp2.positions = ["a"]
_br2 = _bp2.add_row("Ohne Position", 502)
_bp2.set_cell(_br2.id, "2026-03-09", 1)
_br2.tagessatz, _br2.hotel_naechte = 500.0, 1
check("ohne Zuordnung keine Buchung", bookings(_bp2), [])
check("leere Planung", bookings(CrewPlan(date_from="2026-03-09",
                                         date_to="2026-03-10")), [])


# ─── Ergebnis ────────────────────────────────────────────────────────────────

print("\n" + "=" * 62)
if _fails:
    print(f"FEHLGESCHLAGEN: {len(_fails)}")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("Alle Prüfungen bestanden.")
