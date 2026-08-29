"""Endpunkt-Test der Personalplanung (/api/crew/*).

Ruft die Route-Funktionen direkt mit einem minimalen Request auf — ohne laufenden
Server und ohne Easyjob-Anmeldung. Geprüft wird vor allem, was passiert, wenn etwas
NICHT stimmt: keine Planung, krummes Datum, unbekannte Zeile, Zeitraum zu lang.

    .venv/Scripts/python.exe tests/test_crew_routes.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import io
import json
import os
import re as _re
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="crewroutes_"), "test.db")
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("SESSION_SECRET", "test-secret-fuer-den-testlauf-1234567890")

import db as _db

_db.init_db()
with _db.get_conn() as conn:
    conn.executemany(
        "INSERT INTO personal (id, funktion, ressourcenart, tagessatz, eigenkosten, satzname) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(6, "Lichttechniker", "Personal", 520.0, 468.0, "Standard"),
         (13, "Rigger", "Personal", 570.0, 513.0, "Standard"),
         (2, "LKW 7,5t", "Fahrzeug", 150.0, 0.0, "Standard")],
    )

from starlette.requests import Request

import routes.crew as crew
from state import UserSession

_fails: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {label}: {got!r}" + ("" if ok else f"  ERWARTET {want!r}"))
    if not ok:
        _fails.append(label)


def check_in(label: str, needle: str, hay: str) -> None:
    ok = needle in hay
    print(f"  {'✓' if ok else '✗'} {label}" + ("" if ok else f"  — „{needle}“ fehlt"))
    if not ok:
        _fails.append(label)


# ─── Fake-Request mit eigener Session ────────────────────────────────────────

_SESSION: dict = {}


def req() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/crew/x",
                    "headers": [], "query_string": b"", "session": _SESSION,
                    "app": None})


_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def _naechster(iso: str) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(iso) + timedelta(days=1)).isoformat()


def body(resp) -> str:
    return resp.body.decode("utf-8")


def payload(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


ss = crew.get_session(_SESSION)
check("frische Session ohne Planung", ss.crew, None)


# ─── 1. Ohne Planung ─────────────────────────────────────────────────────────

print("\n[1] Aufrufe ohne Planung")
r = run(crew.crew_cell(req(), row_id=1, day="2026-04-01", persons=2))
check("Zelle: Statuscode", r.status_code, 400)
check("Zelle: Fehlertext", payload(r)["error"], "Keine Planung")

r = run(crew.crew_row_add(req(), resource_id=6, group_key="Licht"))
check_in("Zeile hinzufügen weist ab", "zuerst den Zeitraum", body(r))

r = run(crew.crew_row_field(1, req(), field="label", value="x"))
check("Feld: Statuscode", r.status_code, 400)

r = run(crew.crew_panel(req()))
check_in("Panel zeigt das Startformular", "Planung beginnen", body(r))


# ─── 2. Zeitachse ────────────────────────────────────────────────────────────

print("\n[2] Zeitachse")
r = run(crew.crew_range(req(), date_from="quatsch", date_to=""))
check_in("krummes Datum", "Start- und Enddatum", body(r))
check("keine Planung angelegt", ss.crew, None)

r = run(crew.crew_range(req(), date_from="2026-01-01", date_to="2030-01-01"))
check_in("zu langer Zeitraum", "Zeitraum zu lang", body(r))
check("weiterhin keine Planung", ss.crew, None)

r = run(crew.crew_range(req(), date_from="2026-04-30", date_to="2026-03-31"))
check("verdrehte Daten getauscht", (ss.crew.date_from, ss.crew.date_to),
      ("2026-03-31", "2026-04-30"))
check("Phasen vorbelegt", [p.name for p in ss.crew.phases],
      ["Aufbau", "Veranstaltung", "Abbau"])
check_in("Matrix gerendert", 'class="crew-table"', body(r))

r = run(crew.crew_phase(req(), index=0, name="", day_from="2026-03-31",
                        day_to="2026-04-19"))
check("Phase verschoben", ss.crew.phases[0].day_to, "2026-04-19")
r = run(crew.crew_phase(req(), index=99, name="", day_from="2026-04-01",
                        day_to=""))
check_in("unbekannter Termin", "Termin nicht gefunden", body(r))


# ─── 3. Zeilen ───────────────────────────────────────────────────────────────

print("\n[3] Zeilen")
r = run(crew.crew_row_add(req(), resource_id=6, group_key="Licht"))
check("Zeile angelegt", len(ss.crew.rows), 1)
row = ss.crew.rows[0]
check("Bezeichnung aus dem Stamm", row.label, "Lichttechniker")
check("Tagessatz aus dem Stamm", row.tagessatz, 520.0)
check("Eigenkosten aus dem Stamm", row.eigenkosten, 468.0)
# Ein Menüpunkt, den es nicht gibt, wird nicht übernommen — die Zeile
# landet oben unter „Noch nicht einsortiert".
check("unbekannter Menüpunkt ignoriert", row.group_key, "")

r = run(crew.crew_row_add(req(), resource_id=4711, group_key=""))
check_in("unbekannte Ressource", "Ressource nicht gefunden", body(r))
check("keine Zeile dazu", len(ss.crew.rows), 1)

r = run(crew.crew_resource_search(req(), q="rigg"))
check_in("Suche findet Rigger", "Rigger", body(r))
r = run(crew.crew_resource_search(req(), q="zzz"))
check_in("Suche ohne Treffer", "Keine Treffer", body(r))


# ─── 4. Zellen ───────────────────────────────────────────────────────────────

print("\n[4] Zellen")
r = run(crew.crew_cell(req(), row_id=row.id, day="2026-04-01", persons=5))
j = payload(r)
check("Personen gesetzt", j["persons"], 5)
check("Manntage der Zeile", j["row_mt"], 5)
# 5 Manntage ohne Übernachtung: dazu der halbe Spesensatz je Tag.
check("Zeilensumme", j["row_total"], 5 * 520.0 + 5 * 16.0)
check("Tagessumme", j["day_total"], 5)
check("Gesamtsumme", j["totals"]["summe"], 5 * 520.0 + 5 * 16.0)

r = run(crew.crew_cell(req(), row_id=row.id, day="2026-04-01", persons=0))
check("0 räumt die Zelle", payload(r)["persons"], 0)

r = run(crew.crew_cell(req(), row_id=row.id, day="01.04.2026", persons=1))
check("deutsches Datumsformat abgelehnt", r.status_code, 400)
r = run(crew.crew_cell(req(), row_id=999, day="2026-04-01", persons=1))
check("unbekannte Zeile", r.status_code, 404)

# Tag außerhalb der Zeitachse: annehmen, aber nicht mitrechnen — sonst verliert
# jemand seine Eingaben, nur weil er den Zeitraum kurz zu eng gezogen hatte.
r = run(crew.crew_cell(req(), row_id=row.id, day="2027-01-01", persons=3))
check("Tag außerhalb wird gespeichert", payload(r)["persons"], 3)
check("zählt aber nicht mit", payload(r)["row_mt"], 0)


# ─── 5. Felder ───────────────────────────────────────────────────────────────

print("\n[5] Felder der Zeile")
run(crew.crew_cell(req(), row_id=row.id, day="2026-04-02", persons=2))

r = run(crew.crew_row_field(row.id, req(), field="tagessatz", value="1.250,50"))
check("deutsche Zahl gelesen", ss.crew.row(row.id).tagessatz, 1250.5)
check("Summe neu gerechnet", payload(r)["row_total"], 2 * 1250.5 + 2 * 16.0)

# Der Spesensatz steht in der Planung, nicht in der Zeile — er ist eine Eigenschaft
# der Reise, nicht der Person.
r = run(crew.crew_row_field(row.id, req(), field="spesen_satz", value="32"))
check("Spesensatz ist kein Zeilenfeld", r.status_code, 400)

# Hotelnächte werden auf die Manntage gedeckelt: mehr Nächte als Einsatztage sind
# eine Fehleingabe und würden die Spesen ins Negative rechnen.
r = run(crew.crew_row_field(row.id, req(), field="hotel_naechte", value="3"))
check("Hotelnächte", ss.crew.row(row.id).hotel_naechte, 3)
check("auf die Manntage gedeckelt", ss.crew.naechte(ss.crew.row(row.id)), 2)
check("Hotel wirkt auf die Summe",
      payload(r)["row_total"], 2 * 1250.5 + 2 * 32.0 + 2 * 150)
check("Spesen: beide Tage mit Übernachtung", payload(r)["row_spesen"], 2 * 32.0)

# Reisekosten sind ein halber Tagessatz der Zeile.
r = run(crew.crew_row_field(row.id, req(), field="rk_anzahl", value="2"))
check("Reisekosten", payload(r)["row_rk"], 2 * 1250.5 * 0.5)
run(crew.crew_row_field(row.id, req(), field="rk_anzahl", value="0"))
run(crew.crew_row_field(row.id, req(), field="hotel_naechte", value="0"))

r = run(crew.crew_row_field(row.id, req(), field="tagessatz", value="-99"))
check("negative Sätze werden gekappt", ss.crew.row(row.id).tagessatz, 0.0)
r = run(crew.crew_row_field(row.id, req(), field="hotel_naechte", value="drei"))
check("Buchstaben statt Zahl", r.status_code, 400)
r = run(crew.crew_row_field(row.id, req(), field="resource_id", value="1"))
check("unbekanntes Feld", r.status_code, 400)
r = run(crew.crew_row_field(999, req(), field="label", value="x"))
check("unbekannte Zeile", r.status_code, 404)

# Die Bezeichnung ist der Name der Ressource aus dem Stamm — sie wird nicht
# überschrieben. Umbenennen wäre eine zweite Wahrheit neben Easyjob.
r = run(crew.crew_row_field(row.id, req(), field="label", value="Hans"))
check("Bezeichnung nicht änderbar", r.status_code, 400)
check("Bezeichnung unverändert", ss.crew.row(row.id).label, "Lichttechniker")


# ─── 6. Löschen und Verwerfen ────────────────────────────────────────────────

print("\n[6] Löschen")
run(crew.crew_row_add(req(), resource_id=13, group_key="Traverse"))
check("zwei Zeilen", len(ss.crew.rows), 2)
r = run(crew.crew_row_delete(row.id, req()))
check("Zeile entfernt", [x.label for x in ss.crew.rows], ["Rigger"])
r = run(crew.crew_row_delete(row.id, req()))
check_in("zweites Löschen meldet sauber", "Zeile nicht gefunden", body(r))

r = run(crew.crew_reset(req()))
check("Planung verworfen", ss.crew, None)
check_in("zurück im Startzustand", "Planung beginnen", body(r))


# ─── 7. Spaltenraster ────────────────────────────────────────────────────────
# Eine Matrix, in der eine Zeile eine Spalte zu viel hat, verrutscht optisch um
# einen Tag — und niemand sieht es, weil die Zahlen ja alle da sind. Deshalb wird
# jede Zeile nachgezählt (colspan/rowspan mitgerechnet).

print("\n[7] Spaltenraster der Matrix")
from html.parser import HTMLParser


class _Grid(HTMLParser):
    """Zählt Zellen je Tabellenzeile inklusive colspan und übergreifender rowspan."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.widths: list[int] = []
        self._cur = 0
        self._pending: list[list[int]] = []   # [Restzeilen, colspan] laufender rowspans
        self._new: list[list[int]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._cur = sum(span for _, span in self._pending)
            self._new = []
        elif tag in ("td", "th"):
            span = int(a.get("colspan") or 1)
            self._cur += span
            rows = int(a.get("rowspan") or 1)
            if rows > 1:
                self._new.append([rows - 1, span])

    def handle_endtag(self, tag):
        if tag != "tr":
            return
        self.widths.append(self._cur)
        # Erst die schon laufenden rowspans altern lassen, dann die neuen aufnehmen —
        # eine Zelle mit rowspan=3 gilt für DIESE Zeile und zwei weitere.
        self._pending = [[r - 1, s] for r, s in self._pending if r - 1 > 0]
        self._pending += self._new


run(crew.crew_range(req(), date_from="2026-03-31", date_to="2026-04-30"))
run(crew.crew_row_add(req(), resource_id=6, group_key="Licht"))
run(crew.crew_row_add(req(), resource_id=13, group_key="Traverse"))
run(crew.crew_cell(req(), row_id=ss.crew.rows[0].id, day="2026-04-01", persons=4))
matrix = body(run(crew.crew_panel(req())))

# Genau die Matrix greifen: über ihr steht inzwischen die Termin-Tabelle.
start = matrix.index('<table class="crew-table"')
grid = _Grid()
grid.feed(matrix[start:matrix.index("</table>", start) + 8])
tage = len(ss.crew.day_keys())
# Zeilenkopf + Tage + MT/TS/Spesen/Hotel/RK/Summe. Eine eigene Löschspalte gab es
# rechts außen — sie lag hinter dem Scrollbereich und ist entfallen; der Knopf sitzt
# jetzt im Zeilenkopf, der links klebt.
erwartet = tage + 7
check("alle Zeilen gleich breit", sorted(set(grid.widths)), [erwartet])
# 3 Kopfzeilen + je Abschnitt eine Überschrift + je Ressource EINE Zeile +
# Summenzeile + die Zeile mit „+ Menüpunkt" am Fuß. Der frühere Bandstreifen unter
# jeder Zeile ist entfallen — er hat die Matrix doppelt so hoch gemacht; die Position
# steckt jetzt als Hinterlegung in der Zahlenzelle.
check("Zeilenanzahl", len(grid.widths), 3 + 1 + 2 + 1 + 1)
check("kein Bandstreifen mehr", "crew-bandcell" in matrix, False)
check("Überschrift für Zeilen ohne Abschnitt",
      'class="crew-gewerk crew-gewerk-offen"' in matrix, True)


# ─── 8. Zuordnung (Stufe 2) ──────────────────────────────────────────────────

print("\n[8] Positionen und Zuordnung")
from gaeb_parser import GaebItem, GaebProject


def _item(iid, oz, desc, qty, unit):
    return GaebItem(item_id=iid, rno_part=0, oz=oz, description=desc, long_text="",
                    qty=qty, unit=unit, category_path=["Extra", "Labour"])


ss.d83_project = GaebProject(
    name="T", label="T", phase="83", date="", currency="EUR",
    items=[_item("i1", "03.03.01", "Installation", 1, "psch"),
           _item("i2", "03.03.02", "Dismantling", 1, "psch"),
           _item("i3", "03.05.02", "Light operator", 5, "d")])

# Zeitraum ändern beschneidet die Phasen: alles außerhalb fällt weg, sonst zeigen
# sie nach einer Korrektur auf Tage, die es nicht mehr gibt.
run(crew.crew_range(req(), date_from="2026-03-31", date_to="2026-04-30"))
check("frische Planung hat drei Phasen", len(ss.crew.phases), 3)
run(crew.crew_range(req(), date_from="2026-04-01", date_to="2026-04-12"))
check("Phasen auf den neuen Zeitraum beschnitten",
      [(p.name, p.day_from, p.day_to) for p in ss.crew.phases],
      [("Aufbau", "2026-04-01", "2026-04-12")])

# Für die Zuordnungsprüfungen mit frischen Standardphasen weitermachen.
run(crew.crew_reset(req()))
run(crew.crew_range(req(), date_from="2026-04-01", date_to="2026-04-12"))
check("Standardphasen", [(p.name, p.day_from, p.day_to) for p in ss.crew.phases],
      [("Aufbau", "2026-04-01", "2026-04-08"),
       ("Veranstaltung", "2026-04-09", "2026-04-10"),
       ("Abbau", "2026-04-11", "2026-04-12")])
run(crew.crew_row_add(req(), resource_id=6, group_key="Licht"))
rid = ss.crew.rows[0].id
for tag, n in [("2026-04-01", 2), ("2026-04-02", 2), ("2026-04-11", 3)]:
    run(crew.crew_cell(req(), row_id=rid, day=tag, persons=n))

r = run(crew.crew_pos_add(req(), item_id="i1"))
check("Position aufgenommen", ss.crew.positions, ["i1"])
r = run(crew.crew_pos_add(req(), item_id="gibtsnicht"))
check_in("unbekannte Position", "Position nicht gefunden", body(r))
run(crew.crew_pos_add(req(), item_id="i2"))

r = run(crew.crew_assign(req(), row_id=rid, day_from="2026-04-01",
                         day_to="2026-04-02", item_id="i1"))
check("zwei Tage zugeordnet",
      [(s.day_from, s.day_to, s.item_id)
       for s in ss.crew.row(rid).segments(ss.crew.day_keys())],
      [("2026-04-01", "2026-04-02", "i1")])
check_in("Zelle trägt ihren Positionsrahmen", 'style="box-shadow:inset', body(r))
check_in("Positionsliste gerendert", 'class="crew-pos-table"', body(r))

r = run(crew.crew_assign(req(), row_id=rid, day_from="2026-04-01",
                         day_to="2026-04-02", item_id="i-fremd"))
check_in("Position außerhalb der Planung abgewiesen", "gehört nicht zur Planung", body(r))

check("offener Tag bleibt offen",
      [u["days"] for u in ss.crew.unassigned()], [["2026-04-11"]])
run(crew.crew_fill_phase(req(), index=2, item_id="i2", row_id=0))
check("Phase Abbau gefüllt", ss.crew.row(rid).assign.get("2026-04-11"), "i2")
check("keine offenen Tage mehr", ss.crew.unassigned(), [])

r = run(crew.crew_fill_phase(req(), index=9, item_id="i2", row_id=0))
check("unbekannte Phase ändert nichts", ss.crew.row(rid).assign.get("2026-04-11"), "i2")

# Zuordnung wieder lösen
run(crew.crew_assign(req(), row_id=rid, day_from="2026-04-01",
                     day_to="2026-04-01", item_id=""))
check("Tag gelöst", ss.crew.row(rid).assign.get("2026-04-01"), None)
run(crew.crew_assign(req(), row_id=rid, day_from="2026-04-01",
                     day_to="2026-04-01", item_id="i1"))

# Abschnitt / reines Ziel. Standard ist „nur Ziel" — sonst hätte die Matrix so
# viele Überschriften wie das LV Personalpositionen.
check("LV-Position ist erst einmal nur ein Ziel", ss.crew.pos_mode("i1"), "batch")
check("keine Abschnitte am Anfang", ss.crew.menu_keys(), [])
r = run(crew.crew_pos_mode(req(), item_id="i1", mode="menu"))
check("zum Abschnitt gemacht", ss.crew.menu_keys(), ["i1"])
check_in("im Panel beschriftet", "Abschnitt", body(r))
check("i2 hängt am Abschnitt darüber", ss.crew.position_parent("i2"), "i1")
r = run(crew.crew_pos_mode(req(), item_id="i2", mode="quatsch"))
check_in("unbekannter Modus", "Unbekannter Modus", body(r))

run(crew.crew_pos_move(req(), item_id="i2", delta=-1))
check("verschoben", ss.crew.positions, ["i2", "i1"])
check("ohne Abschnitt darüber hängt sie an nichts",
      ss.crew.position_parent("i2"), None)

stats = ss.crew.position_stats()
check("Manntage je Position", {k: v["mt"] for k, v in stats.items()}, {"i1": 4, "i2": 3})
# Tageskosten plus anteilige Spesen: 7 Manntage ohne Übernachtung × 16 € werden
# im Verhältnis der Manntage auf die beiden Positionen verteilt.
check("Kosten je Position",
      {k: round(v["cost"]) for k, v in stats.items()},
      {"i1": 4 * 520 + 4 * 16, "i2": 3 * 520 + 3 * 16})

n = run(crew.crew_pos_remove(req(), item_id="i1"))
check("Position entfernt", ss.crew.positions, ["i2"])
check("ihre Tage sind wieder offen",
      [u["days"] for u in ss.crew.unassigned()], [["2026-04-01", "2026-04-02"]])

# ─── 10. Menüpunkte, Kontext und Termine ─────────────────────────────────────

print("\n[10] Menüpunkte und aktiver Kontext")

run(crew.crew_reset(req()))
run(crew.crew_range(req(), date_from="2026-05-01", date_to="2026-05-14"))
run(crew.crew_pos_add(req(), item_id="i1"))
run(crew.crew_pos_add(req(), item_id="i3"))
# Beide zu Abschnitten machen — nur die nehmen Zeilen auf.
run(crew.crew_pos_mode(req(), item_id="i1", mode="menu"))
run(crew.crew_pos_mode(req(), item_id="i3", mode="menu"))

# Zeile unter einem Menüpunkt anlegen
r = run(crew.crew_row_add(req(), resource_id=6, group_key="i1"))
zeile = ss.crew.rows[-1]
check("Zeile unter dem Menüpunkt", zeile.group_key, "i1")
check("Gliederung nach Menüpunkt",
      [k for k, _ in ss.crew.groups()], ["i1", "i3"])

# Ein Menüpunkt, den es nicht gibt, wird nicht übernommen
r = run(crew.crew_row_add(req(), resource_id=13, group_key="gibtsnicht"))
check("unbekannter Menüpunkt → nicht einsortiert", ss.crew.rows[-1].group_key, "")
check("nicht einsortierte Zeilen stehen oben",
      [k for k, _ in ss.crew.groups()][0], "")

# Umsortieren
r = run(crew.crew_row_move_to(ss.crew.rows[-1].id, req(), group_key="i3"))
check("umsortiert", ss.crew.rows[-1].group_key, "i3")
r = run(crew.crew_row_move_to(ss.crew.rows[-1].id, req(), group_key=""))
check("wieder herausgenommen", ss.crew.rows[-1].group_key, "")

# Eigener Menüpunkt
r = run(crew.crew_menu_add(req(), title="  Projektleitung  "))
key = ss.crew.positions[-1]
check("eigener Menüpunkt angelegt", (key, ss.crew.menu_title(key)),
      ("eigen:1", "Projektleitung"))
check("gilt als Menüpunkt", ss.crew.pos_mode(key), "menu")
r = run(crew.crew_menu_rename(req(), key=key, title="Bauleitung"))
check("umbenannt", ss.crew.menu_title(key), "Bauleitung")
r = run(crew.crew_menu_rename(req(), key="eigen:99", title="x"))
check_in("unbekannter Menüpunkt", "Menüpunkt nicht gefunden", body(r))
check_in("Titel im Panel", "Bauleitung", body(run(crew.crew_panel(req()))))

# Aktive Position als Kontext: Manntage laufen sofort darauf
rid = zeile.id
r = run(crew.crew_cell(req(), row_id=rid, day="2026-05-04", persons=2, assign_to="i1"))
check("Zelle gesetzt", payload(r)["persons"], 2)
check("gleich zugeordnet", payload(r)["assigned"], "i1")
check("Zuordnung steht", ss.crew.row(rid).assign.get("2026-05-04"), "i1")

# Ohne ausdrückliche Auswahl greift der Standard der Zeile — hier die Position, aus
# der sie entstanden ist. Ohne Personen wird nichts zugeordnet.
r = run(crew.crew_cell(req(), row_id=rid, day="2026-05-05", persons=1))
check("ohne Auswahl greift der Standard der Zeile", payload(r)["assigned"], "i1")
r = run(crew.crew_cell(req(), row_id=rid, day="2026-05-06", persons=0, assign_to="i1"))
check("leere Zelle ordnet nicht zu", payload(r)["assigned"], "")
r = run(crew.crew_cell(req(), row_id=rid, day="2026-05-07", persons=1, assign_to="fremd"))
check("fremde Auswahl fällt auf den Standard zurück", payload(r)["assigned"], "i1")

# Eine Zeile ohne Position und ohne Abschnitt bleibt offen.
run(crew.crew_row_add(req(), resource_id=6, group_key=""))
frei = ss.crew.rows[-1].id
r = run(crew.crew_cell(req(), row_id=frei, day="2026-05-05", persons=1))
check("Zeile ohne Position bleibt offen", payload(r)["assigned"], "")

print("\n[11] Termine bearbeiten")
check("Termine vorbelegt", len(ss.crew.phases), 3)
r = run(crew.crew_phase(req(), index=0, name="Anlieferung", day_from="", day_to=""))
check("umbenannt", ss.crew.phases[0].name, "Anlieferung")

# Termin verlängern: der Nachbar weicht, statt zu überlappen.
vorher = ss.crew.phases[1].day_from
r = run(crew.crew_phase(req(), index=0, name="",
                        day_from="2026-05-01", day_to=vorher))
check("Nachbar weicht", ss.crew.phases[1].day_from > vorher, True)
check("keine Überlappung",
      all(a.day_to < b.day_from for a, b in zip(ss.crew.phases, ss.crew.phases[1:])),
      True)

anzahl = len(ss.crew.phases)
r = run(crew.crew_phase_add(req(), name="Proben",
                            day_from="2026-05-01", day_to="2026-05-02"))
check_in("überlappender Termin abgewiesen", "überlappt", body(r))
check("nichts angelegt", len(ss.crew.phases), anzahl)

run(crew.crew_phase_remove(req(), index=1))
check("Termin entfernt", len(ss.crew.phases), anzahl - 1)
r = run(crew.crew_phase_remove(req(), index=99))
check_in("unbekannter Termin", "Termin nicht gefunden", body(r))

luecke = ss.crew.phases[0].day_to
r = run(crew.crew_phase_add(req(), name="Proben",
                            day_from=_naechster(luecke), day_to=_naechster(luecke)))
check("eigener Termin angelegt",
      [p.name for p in ss.crew.phases].count("Proben"), 1)
check("Termine sortiert",
      [p.day_from for p in ss.crew.phases],
      sorted(p.day_from for p in ss.crew.phases))
check_in("Termin-Editor im Panel", 'class="crew-terms"', body(run(crew.crew_panel(req()))))


# ─── 12. Positionen und Zeilen folgen dem Matching ───────────────────────────
# Kein „LV übernehmen"-Knopf: was im Matching eine Personal-Ressource trifft, steht
# sofort oben. Das gilt in beide Richtungen — mit einer Ausnahme, damit niemand
# stillschweigend Arbeit verliert.

print("\n[12] Laufender Abgleich mit dem Matching")

from matcher import Resource as _Res


class _M:
    def __init__(self, obj, score=99.0, method="manual"):
        self.matched = obj
        self.score = score
        self.method = method
        self.article = None
        self.confident = True


def _res(rid, funktion, satz):
    return _Res(id=rid, funktion=funktion, ressourcenart="Personal",
                tagessatz=satz, eigenkosten=satz * 0.9, satzname="S",
                gaeb_synonyms=[])


run(crew.crew_reset(req()))
ss.d83_project = GaebProject(
    name="T", label="T", phase="83", date="", currency="EUR",
    items=[_item("p1", "03.03.01", "Installation", 1, "psch"),
           _item("p2", "03.05.01", "Audio operator", 5, "d"),
           _item("p3", "03.05.02", "Light operator", 5, "d")])
ss.matches = {"p2": _M(_res(13, "Rigger", 570))}
run(crew.crew_range(req(), date_from="2026-06-01", date_to="2026-06-10"))
check("Position aus dem Match sofort da", ss.crew.positions, ["p2"])
check("Zeile aus dem Match sofort da",
      [(r.label, r.group_key) for r in ss.crew.rows], [("Rigger", "p2")])

# Zweiter Match kommt dazu
ss.matches["p3"] = _M(_res(6, "Lichttechniker", 520))
run(crew.crew_panel(req()))
check("neuer Match erscheint", ss.crew.positions, ["p2", "p3"])
check("neue Zeile erscheint", len(ss.crew.rows), 2)

# Dieselbe Ressource an zwei Positionen → weiterhin nur eine Zeile
ss.matches["p1"] = _M(_res(6, "Lichttechniker", 520))
run(crew.crew_panel(req()))
check("drei Positionen", sorted(ss.crew.positions), ["p1", "p2", "p3"])
check("aber nur eine Zeile je Ressource", len(ss.crew.rows), 2)

# Match entfällt → Position verschwindet, Zeilen bleiben (die tragen Eingaben)
del ss.matches["p1"]
run(crew.crew_panel(req()))
check("Position ohne Match verschwindet", sorted(ss.crew.positions), ["p2", "p3"])
check("Zeilen bleiben", len(ss.crew.rows), 2)

# Zugeordnete Tage schützen die Position, auch wenn der Match wegfällt — sonst
# würden Manntage still ihr Ziel verlieren.
rid = [r for r in ss.crew.rows if r.resource_id == 6][0].id
run(crew.crew_cell(req(), row_id=rid, day="2026-06-03", persons=2, assign_to="p3"))
del ss.matches["p3"]
run(crew.crew_panel(req()))
check("Position mit Zuordnung bleibt", "p3" in ss.crew.positions, True)
check("Zuordnung unberührt", ss.crew.row(rid).assign.get("2026-06-03"), "p3")

# Gelöschte Zeile bleibt gelöscht
run(crew.crew_row_delete(rid, req()))
run(crew.crew_panel(req()))
check("Ressource abgewählt", ss.crew.dismissed, [6])
check("kommt nicht wieder", [r.resource_id for r in ss.crew.rows], [13])
ss.matches["p3"] = _M(_res(6, "Lichttechniker", 520))
run(crew.crew_panel(req()))
check("auch nicht bei neuem Match", [r.resource_id for r in ss.crew.rows], [13])

# Von Hand wieder hinzufügen hebt die Abwahl auf — wer sie sucht und anklickt, will
# sie haben. Sonst bliebe sie in `dismissed` stehen und käme nach dem nächsten
# Löschen nie wieder von selbst.
run(crew.crew_row_add(req(), resource_id=6, group_key=""))
run(crew.crew_panel(req()))
check("wieder hinzugefügt", sorted(r.resource_id for r in ss.crew.rows), [6, 13])
check("Abwahl aufgehoben", ss.crew.dismissed, [])

# Von Hand dazugelegte Position (kein Personal-Match) bleibt stehen
run(crew.crew_pos_add(req(), item_id="p1"))
run(crew.crew_panel(req()))
check("von Hand dazugelegt", ss.crew.manual, ["p1"])
check("bleibt trotz fehlendem Match", "p1" in ss.crew.positions, True)
run(crew.crew_pos_remove(req(), item_id="p1"))
run(crew.crew_panel(req()))
check("und wieder entfernbar", "p1" in ss.crew.positions, False)

# Bezeichnungen sind fest — es sind Ressourcen aus dem Stamm
r = run(crew.crew_row_field(ss.crew.rows[0].id, req(), field="label", value="Hans"))
check("Umbenennen nicht möglich", r.status_code, 400)
check("Bezeichnung unverändert", ss.crew.rows[0].label, "Rigger")

h = body(run(crew.crew_panel(req())))
check("Zeilenkopf ist ziehbar", 'draggable="true"' in h, True)
check("Ablageziele vorhanden", h.count('data-drop="1"') >= 1, True)


# ─── 13. Mehrere Zellen gemeinsam füllen ─────────────────────────────────────

print("\n[13] Bereich füllen")

run(crew.crew_reset(req()))
ss.matches = {}
run(crew.crew_range(req(), date_from="2026-07-01", date_to="2026-07-10"))
run(crew.crew_pos_add(req(), item_id="p1"))
run(crew.crew_row_add(req(), resource_id=13, group_key=""))
run(crew.crew_row_add(req(), resource_id=6, group_key=""))
f1, f2 = [r.id for r in ss.crew.rows]

r = run(crew.crew_cells_fill(req(), row_ids=f"{f1},{f2}", day_from="2026-07-02",
                             day_to="2026-07-06", persons=3, assign_to="p1"))
check_in("Antwort ist das Panel", '<div id="crew-panel"', body(r))
check("zehn Zellen gefüllt", ss.crew.totals()["manntage"], 30)
check("gleich zugeordnet", ss.crew.unassigned(), [])

# Eine einzelne Zelle antwortet dagegen mit JSON und lässt das Panel stehen — sonst
# ginge beim Tippen der Eingabefokus verloren.
r = run(crew.crew_cell(req(), row_id=f1, day="2026-07-08", persons=1, assign_to="p1"))
check("Einzelzelle bleibt JSON", payload(r)["assigned"], "p1")

r = run(crew.crew_cells_fill(req(), row_ids="", day_from="2026-07-01",
                             day_to="2026-07-02", persons=1, assign_to=""))
check_in("ohne Zeile", "Keine Zeile ausgewählt", body(r))
r = run(crew.crew_cells_fill(req(), row_ids="abc", day_from="2026-07-01",
                             day_to="2026-07-02", persons=1, assign_to=""))
check_in("unbrauchbare IDs", "Keine Zeile ausgewählt", body(r))
r = run(crew.crew_cells_fill(req(), row_ids=str(f1), day_from="01.07.2026",
                             day_to="2026-07-02", persons=1, assign_to=""))
check_in("krummes Datum", "Datum ungültig", body(r))

# Fremdes Ziel wird ignoriert, gefüllt wird trotzdem
run(crew.crew_cells_fill(req(), row_ids=str(f2), day_from="2026-07-09",
                         day_to="2026-07-09", persons=2, assign_to="fremd"))
check("gefüllt", ss.crew.row(f2).cells.get("2026-07-09"), 2)
check("aber nicht zugeordnet", ss.crew.row(f2).assign.get("2026-07-09"), None)

h = body(run(crew.crew_panel(req())))
check("Bandzellen tragen ihre Position", 'data-item="p1"' in h, True)
check("Tagesköpfe tragen ihr Datum", 'class="crew-day" data-day="2026-07-01"' in h
      or 'data-day="2026-07-01"' in h, True)
check("Auswahl-Hinweis vorhanden", 'id="crew-sel-hint"' in h, True)


# ─── 14. Standard-Positionen je Abschnitt ────────────────────────────────────

print("\n[14] Standard-Positionen")

run(crew.crew_reset(req()))
ss.matches = {}
run(crew.crew_range(req(), date_from="2026-09-01", date_to="2026-09-10"))
run(crew.crew_pos_add(req(), item_id="p1"))
run(crew.crew_pos_add(req(), item_id="p2"))
run(crew.crew_menu_add(req(), title="Licht"))
abschnitt = ss.crew.menu_keys()[0]
run(crew.crew_row_add(req(), resource_id=13, group_key=abschnitt))
srid = ss.crew.rows[0].id

r = run(crew.crew_menu_positions(req(), key=abschnitt, item_id="p1", action="add"))
check("Standard gesetzt", ss.crew.menu_pos(abschnitt), "p1")
check_in("in der Überschrift sichtbar", 'class="crew-mp"', body(r))
# Beide Marken — Ziel und Nebenkosten — tragen die Farbe ihrer Position.
check_in("Ziel-Marke mit Farbe", 'class="crew-mp-dot" style="background:', body(r))

# Zweimal dieselbe Position: folgenlos, keine Fehlermeldung. „Schon gesetzt" und
# „gibt es nicht" gaben früher dasselbe False und wurden beide als Fehler gemeldet.
r = run(crew.crew_menu_positions(req(), key=abschnitt, item_id="p1", action="add"))
check("zweiter Klick ändert nichts", ss.crew.menu_pos(abschnitt), "p1")
check("und meldet keinen Fehler", "error-msg" in body(r), False)
check_in("das Panel kommt zurück", '<div id="crew-panel"', body(r))

r = run(crew.crew_menu_positions(req(), key=abschnitt, item_id="", action="add"))
check_in("ohne Auswahl abgewiesen", "Erst oben eine Position", body(r))
r = run(crew.crew_menu_positions(req(), key=abschnitt, item_id=abschnitt, action="add"))
check_in("eigener Menüpunkt als Standard abgewiesen", "nur eine Überschrift", body(r))
run(crew.crew_menu_positions(req(), key=abschnitt, item_id="p1", action="add"))
r = run(crew.crew_menu_positions(req(), key="eigen:99", item_id="p1", action="add"))
check_in("unbekannter Abschnitt", "Abschnitt nicht gefunden", body(r))
r = run(crew.crew_menu_positions(req(), key=abschnitt, item_id="gibtsnicht", action="add"))
check_in("unbekannte Position", "Nur Positionen aus dem Leistungsverzeichnis", body(r))
# Ein zweites Setzen ersetzt — je Abschnitt gibt es genau eine Standard-Position.
r = run(crew.crew_menu_positions(req(), key=abschnitt, item_id="p2", action="add"))
check("ersetzt statt angehängt", ss.crew.menu_pos(abschnitt), "p2")
run(crew.crew_menu_positions(req(), key=abschnitt, item_id="p1", action="add"))
# Entfernen nimmt sie heraus, ohne Fehlermeldung.
r = run(crew.crew_menu_positions(req(), key=abschnitt, item_id="", action="remove"))
check("herausgenommen", ss.crew.menu_pos(abschnitt), "")
check("ohne Fehler", "error-msg" in body(r), False)
run(crew.crew_menu_positions(req(), key=abschnitt, item_id="p1", action="add"))

# Tippen ohne Chip-Auswahl nutzt den Standard des Abschnitts.
r = run(crew.crew_cell(req(), row_id=srid, day="2026-09-03", persons=3, assign_to=""))
check("ohne Auswahl greift der Standard", payload(r)["assigned"], "p1")
check("keine offenen Tage", ss.crew.unassigned(), [])

# Eine ausdrücklich gewählte Position schlägt den Standard.
r = run(crew.crew_cell(req(), row_id=srid, day="2026-09-04", persons=1, assign_to="p2"))
check("Auswahl schlägt den Standard", payload(r)["assigned"], "p2")

# Bereichsfüllen nutzt den Standard ebenfalls.
run(crew.crew_cells_fill(req(), row_ids=str(srid), day_from="2026-09-06",
                         day_to="2026-09-08", persons=2, assign_to=""))
check("Bereich mit Standard zugeordnet",
      [ss.crew.row(srid).assign.get("2026-09-0" + d) for d in "678"],
      ["p1", "p1", "p1"])

run(crew.crew_menu_positions(req(), key=abschnitt, item_id="", action="remove"))
check("Standard entfernt", ss.crew.menu_pos(abschnitt), "")
r = run(crew.crew_cell(req(), row_id=srid, day="2026-09-09", persons=1, assign_to=""))
check("danach wieder offen", payload(r)["assigned"], "")

# Eigene Menüpunkte tauchen nicht als Chip auf — sie sind Überschriften.
h = body(run(crew.crew_panel(req())))
chips = _re.findall(r'data-item-id="([^"]*)" data-mode', h)
check("Chips nur aus LV-Positionen", sorted(c for c in chips if c), ["p1", "p2"])
check("kein Titelfeld im Chip", 'class="crew-chip-input"' in h, False)
check("Abschnitt in der Matrix beschriftbar", 'class="crew-head-input"' in h, True)

# Höhe: die Notizen im Bandstreifen und die Liste offener Tage sind weg.
check("keine Bandnotizen", "crew-band-note" in h, False)
check("keine Liste offener Tage", "crew-open-list" in h, False)
check("Suche nicht im getauschten Bereich", 'id="crew-search"' in h, False)


# ─── 15. Farbe in der Zelle, Zuordnung über die Auswahl ──────────────────────

print("\n[15] Zellenfarbe und Bereichszuordnung")

run(crew.crew_reset(req()))
ss.matches = {}
run(crew.crew_range(req(), date_from="2026-10-01", date_to="2026-10-08"))
run(crew.crew_pos_add(req(), item_id="p1"))
run(crew.crew_pos_add(req(), item_id="p2"))
run(crew.crew_row_add(req(), resource_id=13, group_key=""))
zid = ss.crew.rows[0].id
run(crew.crew_cell(req(), row_id=zid, day="2026-10-02", persons=3, assign_to="p1"))
run(crew.crew_cell(req(), row_id=zid, day="2026-10-03", persons=2, assign_to=""))

h = body(run(crew.crew_panel(req())))
# Die Zahlenzelle trägt Position und Hinterlegung; ein eigener Bandstreifen entfällt.
check("Zelle kennt ihre Position", 'data-day="2026-10-02" data-item="p1"' in h, True)
check("und trägt ihren Rahmen",
      bool(_re.search(r'data-item="p1"[^>]*style="box-shadow:inset 0 0 0 2px #', h)), True)
check("Tag ohne Position ist schraffiert",
      bool(_re.search(r'crew-open[^>]*data-day="2026-10-03"', h)), True)

# Zelle leeren nimmt die Zuordnung mit — eine stehengebliebene Farbe würde etwas
# behaupten, was nicht mehr stimmt.
r = run(crew.crew_cell(req(), row_id=zid, day="2026-10-02", persons=0, assign_to="p1"))
check("geleert: keine Zuordnung", ss.crew.row(zid).assign.get("2026-10-02"), None)
check("und nichts gemeldet", payload(r)["assigned"], "")

# Umhängen über die Auswahl: Felder markieren, Position anklicken.
run(crew.crew_cells_fill(req(), row_ids=str(zid), day_from="2026-10-05",
                         day_to="2026-10-07", persons=1, assign_to="p1"))
r = run(crew.crew_assign_range(req(), row_ids=str(zid), day_from="2026-10-05",
                               day_to="2026-10-07", item_id="p2"))
check("Bereich umgehängt",
      [ss.crew.row(zid).assign.get("2026-10-0" + d) for d in "567"],
      ["p2", "p2", "p2"])
r = run(crew.crew_assign_range(req(), row_ids=str(zid), day_from="2026-10-05",
                               day_to="2026-10-07", item_id=""))
check("Radierer löst",
      [ss.crew.row(zid).assign.get("2026-10-0" + d) for d in "567"], [None, None, None])
check("und die Tage sind offen",
      [u["days"] for u in ss.crew.unassigned()],
      [["2026-10-03", "2026-10-05", "2026-10-06", "2026-10-07"]])

r = run(crew.crew_assign_range(req(), row_ids="", day_from="2026-10-01",
                               day_to="2026-10-02", item_id="p1"))
check_in("ohne Zeile", "Keine Zeile ausgewählt", body(r))
r = run(crew.crew_assign_range(req(), row_ids=str(zid), day_from="1.10.2026",
                               day_to="2026-10-02", item_id="p1"))
check_in("krummes Datum", "Datum ungültig", body(r))
r = run(crew.crew_assign_range(req(), row_ids=str(zid), day_from="2026-10-01",
                               day_to="2026-10-02", item_id="fremd"))
check_in("fremde Position", "gehört nicht zur Planung", body(r))

# Zeilen aus dem Matching kennen ihre Position, auch ohne Abschnitt.
run(crew.crew_reset(req()))
ss.matches = {"p2": _M(_res(84, "Licht-Operator", 600))}
run(crew.crew_range(req(), date_from="2026-11-01", date_to="2026-11-08"))
lz = ss.crew.rows[0]
check("Zeile kennt ihre LV-Position", ss.crew.default_pos_for(lz), "p2")
check("steht aber in keinem Abschnitt", ss.crew.home_of(lz), "")
r = run(crew.crew_cell(req(), row_id=lz.id, day="2026-11-04", persons=2, assign_to=""))
check("Eingabe läuft trotzdem auf die Position", payload(r)["assigned"], "p2")
h = body(run(crew.crew_panel(req())))
check("Zeilenkopf zeigt die Position", 'class="crew-std"' in h, True)


# ─── 16. Zellenfarben und Abschnittswahl ─────────────────────────────────────

print("\n[16] Farben und Abschnittswahl")

run(crew.crew_reset(req()))
ss.matches = {}
run(crew.crew_range(req(), date_from="2026-12-01", date_to="2026-12-06"))
run(crew.crew_pos_add(req(), item_id="p1"))
run(crew.crew_pos_add(req(), item_id="p2"))
run(crew.crew_menu_add(req(), title="Licht"))
absch = ss.crew.menu_keys()[0]
run(crew.crew_row_add(req(), resource_id=13, group_key=absch))
fid = ss.crew.rows[0].id
run(crew.crew_cell(req(), row_id=fid, day="2026-12-02", persons=2, assign_to="p1"))
run(crew.crew_cell(req(), row_id=fid, day="2026-12-03", persons=1, assign_to="p2"))
run(crew.crew_cell(req(), row_id=fid, day="2026-12-04", persons=1, assign_to=""))
h = body(run(crew.crew_panel(req())))

# Chip und Zelle müssen dieselbe Position gleich einfärben — sonst sieht eine
# Zuordnung nach einer anderen Position aus, als sie ist.
chips = dict(_re.findall(
    r'data-item-id="([^"]+)"[^>]*>\s*<span class="crew-chip-dot" style="background:([^"]+)"', h))
zellen = dict(_re.findall(
    r'data-day="[^"]*" data-item="([^"]+)"[^>]*style="box-shadow:inset 0 0 0 2px ([^"]+)"', h))

# Die Position zeigt sich als Rahmen, nicht als Füllung: das Feld bleibt weiß und
# die Zahl lesbar. Die Farbe muss dieselbe sein wie im Chip — abgeschwächt sähe
# dieselbe Position an zwei Orten verschieden aus.
check("Rahmenfarbe = Chipfarbe", zellen, {k: chips[k] for k in zellen})
check("kein Farbhintergrund",
      bool(_re.search(r'data-item="p1"[^>]*style="background:', h)), False)

# Besetzt ohne Position ist schraffiert, nicht getönt — eine dritte Tönung sähe aus
# wie eine falsche Zuordnung (Lila ist selbst eine Positionsfarbe).
check("Tag ohne Position schraffiert",
      bool(_re.search(r'class="crew-cell[^"]*crew-open[^"]*"[^>]*data-day="2026-12-04"', h)),
      True)
check("und ohne Rahmen",
      bool(_re.search(r'data-day="2026-12-04" data-item=""[^>]*style=', h)), False)

# Der Abschnitt wird über seine Überschrift gewählt — eigene Menüpunkte sind keine
# Chips mehr, es gäbe sonst keinen Weg dorthin.
check_in("Überschrift trägt ihren Schlüssel", 'data-menu="' + absch + '"', h)
check_in("und ist als anklickbar ausgewiesen", "neue Ressourcen landen unter", h)

_js = io.open(os.path.join(os.path.dirname(__file__), "..", "static",
                           "crew_matrix.js"), encoding="utf-8").read()
check("Abschnitt getrennt von der Position geführt", "var activeMenu" in _js, True)
check("crewActiveMenu liefert ihn", "return activeMenu;" in _js, True)
check("Überschrift-Klick verdrahtet", "tr.crew-gewerk" in _js, True)


# ─── 17. Nachziehen und Umbenennen per Doppelklick ───────────────────────────

print("\n[17] Zuordnung nachreichen")

run(crew.crew_reset(req()))
ss.matches = {}
run(crew.crew_range(req(), date_from="2027-01-04", date_to="2027-01-08"))
run(crew.crew_pos_add(req(), item_id="p1"))
run(crew.crew_menu_add(req(), title="Licht"))
nid = ss.crew.menu_keys()[0]
run(crew.crew_row_add(req(), resource_id=13, group_key=nid))
oid = ss.crew.rows[0].id
# Ohne Standard und ohne Auswahl: die Tage bleiben offen — genau der Fall, für den
# das Nachziehen da ist.
for d in ("2027-01-05", "2027-01-06"):
    run(crew.crew_cell(req(), row_id=oid, day=d, persons=2, assign_to=""))
check("Tage offen", sum(u["mt"] for u in ss.crew.unassigned()), 4)

h = body(run(crew.crew_panel(req())))
check_in("Nachzieh-Knopf in der Warnzeile", "der gewählten Position", h)

r = run(crew.crew_assign_open(req(), item_id="p1", row_id=0))
check("nachgezogen", ss.crew.unassigned(), [])
check("auf der Position gelandet", ss.crew.position_stats()["p1"]["mt"], 4)

r = run(crew.crew_assign_open(req(), item_id="", row_id=0))
check_in("ohne Auswahl", "Erst oben eine Position", body(r))
r = run(crew.crew_assign_open(req(), item_id="fremd", row_id=0))
check_in("fremde Position", "gehört nicht zur Planung", body(r))

# Der Titel eines Abschnitts ist erst per Doppelklick änderbar — das Feld nimmt
# vorher keine Klicks an, sonst bliebe nichts zum Auswählen übrig.
check_in("Doppelklick-Hinweis am Feld", "Doppelklick zum Umbenennen", h)
_css = io.open(os.path.join(os.path.dirname(__file__), "..", "static",
                            "style.css"), encoding="utf-8").read()
check("Feld gesperrt", ".crew-head-input                { pointer-events:none" in _css, True)
check("Freigabe per Klasse", ".crew-head-input.crew-head-edit { pointer-events:auto" in _css, True)
_js2 = io.open(os.path.join(os.path.dirname(__file__), "..", "static",
                            "crew_matrix.js"), encoding="utf-8").read()
check("dblclick verdrahtet", 'addEventListener("dblclick"' in _js2, True)
check("Sperre nach dem Verlassen", 'addEventListener("focusout"' in _js2, True)


# ─── 18. Bereich über mehrere Zeilen ─────────────────────────────────────────

print("\n[18] Bereichsfüllen über mehrere Zeilen")

run(crew.crew_reset(req()))
ss.matches = {"p1": _M(_res(13, "Rigger", 570)), "p2": _M(_res(6, "Lichttechniker", 520))}
run(crew.crew_range(req(), date_from="2027-02-01", date_to="2027-02-08"))
mids = [r.id for r in ss.crew.rows]
check("zwei Zeilen aus dem Matching", len(mids), 2)
check("mit je eigener Position",
      [ss.crew.default_pos_for(r) for r in ss.crew.rows], ["p1", "p2"])

run(crew.crew_cells_fill(req(), row_ids=",".join(str(i) for i in mids),
                         day_from="2027-02-03", day_to="2027-02-04",
                         persons=2, assign_to=""))
check("jede Zeile auf ihre eigene Position",
      [r.assign.get("2027-02-03") for r in ss.crew.rows], ["p1", "p2"])
check("nicht alle auf die der ersten",
      len({r.assign.get("2027-02-03") for r in ss.crew.rows}), 2)

run(crew.crew_cells_fill(req(), row_ids=",".join(str(i) for i in mids),
                         day_from="2027-02-06", day_to="2027-02-06",
                         persons=1, assign_to="p1"))
check("ausdrückliche Auswahl gilt für alle",
      [r.assign.get("2027-02-06") for r in ss.crew.rows], ["p1", "p1"])


# ─── 19. Mehrfachauswahl bleibt sichtbar ─────────────────────────────────────
# Die Auswahl muss auch über einer schon eingefärbten Zelle zu erkennen sein, und
# das Ziehen darf an einer gefüllten Zelle nicht hängenbleiben.

print("\n[19] Sichtbarkeit der Auswahl")

_css2 = io.open(os.path.join(os.path.dirname(__file__), "..", "static",
                             "style.css"), encoding="utf-8").read()
_js3 = io.open(os.path.join(os.path.dirname(__file__), "..", "static",
                            "crew_matrix.js"), encoding="utf-8").read()

check("Auswahl als kräftige Fläche",
      ".crew-row .crew-cell.crew-sel   { background:#b39ddb !important; }" in _css2, True)
# Der Positionsrahmen ist ein inset-Schatten und wird über den Hintergrund gezeichnet
# — Auswahl und Zuordnung sind damit gleichzeitig zu sehen.
check("Positionsfarbe als Rahmen, nicht als Fläche",
      "box-shadow:inset 0 0 0 2px" in body(run(crew.crew_panel(req()))), True)
# Die Schraffur offener Tage trägt !important; die Auswahl muss dahinter stehen,
# sonst verschwindet sie ausgerechnet dort.
check("Auswahl steht nach der Schraffur",
      _css2.index(".crew-row .crew-cell.crew-sel   {")
      > _css2.index(".crew-row .crew-cell.crew-open  {"), True)
check("Rand der Auswahl", ".crew-row .crew-cell.crew-sel::after" in _css2, True)
# Beim Verschieben einer Zeile sitzt die Einfügelinie unten an der Überschrift —
# dort landet sie. Oben gezeichnet sah es aus, als käme sie über den Abschnitt.
check("Einfügelinie unterhalb der Überschrift",
      "box-shadow:inset 0 -3px 0 #5c35b0" in _css2, True)
check("Löschknopf im Zeilenkopf",
      'class="crew-x crew-row-x"' in body(run(crew.crew_panel(req()))), True)
# Der Auswahl-Hinweis trägt display:inline-block — ohne diese Regel übersteuert das
# das display:none des hidden-Attributs, und unter der Tabelle bleibt eine leere
# Pille stehen.
check("versteckter Hinweis bleibt versteckt",
      ".crew-sel-hint[hidden]          { display:none; }" in _css2, True)
# Abschnitts-Überschrift und ihre Standard-Positionen kleben links wie die
# Zeilenköpfe, sonst wandern sie beim seitlichen Scrollen unter die Tagesspalten.
check("erste Spalte klebt links",
      ".crew-table .crew-head          { position:sticky; left:0; }" in _css2, True)
check("Positionsleiste daneben klebt mit",
      "left:var(--crew-kopf-ist, var(--crew-kopf));" in _css2, True)
# Die Marken lagen in einer Zelle über die ganze Tabellenbreite. Eine Zelle, die den
# Sichtbereich ohnehin füllt, kann nicht kleben — ihr Inhalt scrollte einfach mit
# weg. Deshalb klebt jetzt der Inhalt und nicht die Zelle.
check("nicht mehr die breite Zelle",
      ".crew-table .crew-menu-bar      { position:sticky" in _css2, False)

# Ziehen über gefüllte Zellen: sonst markiert der Browser deren Zahl statt die
# Auswahl zu erweitern. Verhindert wird das an der Wurzel — kein Standardverhalten
# beim Drücken. Der Fokus fällt damit weg und wird selbst gesetzt; ohne das ließe
# sich in eine Auswahl nichts mehr schreiben.
check("kein Standardverhalten beim Drücken",
      "e.preventDefault();" in _js3.split('contains("crew-in")')[1][:600], True)
check("Fokus wird selbst gesetzt",
      "el.focus();" in _js3 and "el.select();" in _js3, True)
# Die beiden Notlösungen von zwischendurch nahmen dem Feld den Cursor.
check("kein removeAllRanges", "removeAllRanges" in _js3, False)
check("kein user-select auf den Feldern", ".crew-dragsel input" in _css2, False)


# ─── 20. Nebenkosten: Spesen, Hotel, Reisekosten ─────────────────────────────
# Eingegeben wird nur die Anzahl. Der Spesensatz kommt aus dem Stamm (je Land ein
# Arbeitsmittel) und gilt für die ganze Planung: das Land hängt an der Reise, nicht
# an der Person. Voller Satz für Tage mit Übernachtung, halber für Tage ohne.

print("\n[20] Nebenkosten")

with _db.get_conn() as _c:
    _c.executemany("INSERT OR IGNORE INTO personal "
                   "(id, funktion, ressourcenart, tagessatz, eigenkosten, satzname) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   [(123, "Spesensatz Inland", "Arbeitsmittel", 32.0, 0.0, "S"),
                    (135, "Spesensatz Schweiz", "Arbeitsmittel", 62.0, 0.0, "S")])

run(crew.crew_reset(req()))
ss.matches = {}
run(crew.crew_range(req(), date_from="2027-03-01", date_to="2027-03-12"))
run(crew.crew_pos_add(req(), item_id="p1"))
run(crew.crew_pos_add(req(), item_id="p2"))
run(crew.crew_row_add(req(), resource_id=13, group_key=""))     # Rigger, 570 €
nk = ss.crew.rows[0].id
tage = ss.crew.day_keys()
for d in tage[:3]:
    run(crew.crew_cell(req(), row_id=nk, day=d, persons=1, assign_to="p1"))
for d in tage[3:8]:
    run(crew.crew_cell(req(), row_id=nk, day=d, persons=1, assign_to="p2"))
run(crew.crew_row_field(nk, req(), field="hotel_naechte", value="5"))
r = run(crew.crew_row_field(nk, req(), field="rk_anzahl", value="1"))
j = payload(r)

check("Vorgabe ist der Inlandssatz",
      (ss.crew.spesen_name, ss.crew.spesen_satz), ("Spesensatz Inland", 32.0))
check("Spesen: 3 Tage halb, 5 Nächte voll", j["row_spesen"], 3 * 16.0 + 5 * 32.0)
check("Hotel: Nächte × Satz", j["row_hotel"], 5 * 150.0)
check("Reisekosten: halber Tagessatz", j["row_rk"], 570.0 * 0.5)
check("Zeilensumme", j["row_total"], 8 * 570.0 + 208.0 + 750.0 + 285.0)

# Satz wechseln gilt rückwirkend für alle Zeilen — es ist dieselbe Reise.
r = run(crew.crew_kosten(req(), art="spesen", resource_id=135))
check("Satz gewechselt", ss.crew.spesen_name, "Spesensatz Schweiz")
check("Spesen neu gerechnet",
      round(ss.crew.row_spesen(ss.crew.row(nk))), round(3 * 31.0 + 5 * 62.0))
r = run(crew.crew_kosten(req(), art="spesen", resource_id=4711))
check_in("unbekannter Satz", "nicht gefunden", body(r))
h = body(run(crew.crew_kosten_list(req(), art="spesen")))
check("Auswahlliste zeigt die Sätze", h.count('class="crew-spesen-opt'), 2)
check_in("und markiert den gewählten", "crew-spesen-on", h)
run(crew.crew_kosten(req(), art="spesen", resource_id=123))

# Hotel und Reisekosten laufen auf eigene Ressourcen. In der Angebotsphase gibt es je
# genau eine, deshalb keine Auswahl in der Leiste — aber gebucht werden muss auf sie,
# nicht als Zuschlag auf den Tagessatz der Techniker.
check("Hotel-Ressource gesetzt",
      (ss.crew.hotel_id, ss.crew.hotel_name), (37, "Hotelkosten"))
check("Reisekosten-Ressource gesetzt",
      (ss.crew.rk_id, ss.crew.rk_name), (126, "Reisekosten Pauschal"))
check("Hotelpreis bleibt beim Wechsel stehen", ss.crew.hotel_satz, 150.0)
# Der Preis je Nacht geht auch je Zeile — ein Kollege im teureren Hotel ändert nichts
# an den übrigen. Leer heißt: der Preis aus der Leiste gilt.
_hz = ss.crew.row(nk)
run(crew.crew_row_field(nk, req(), field="hotel_satz", value="210,50"))
check("Preis je Zeile gesetzt", _hz.hotel_satz, 210.5)
check("Hotel der Zeile neu gerechnet", ss.crew.row_hotel(_hz), 5 * 210.5)
check("andere Zeilen unberührt", ss.crew.hotel_satz, 150.0)
run(crew.crew_row_field(nk, req(), field="hotel_satz", value="0"))
check("leer = Preis der Planung", ss.crew.row_hotel(_hz), 5 * 150.0)
# Dasselbe für die Reisen: vorbelegt ist ein halber Tagessatz der Zeile, wer für den
# Flug etwas anderes ansetzt, trägt es ein.
check("Vorgabe halber Tagessatz", ss.crew.rk_satz_of(_hz), 570.0 * 0.5)
run(crew.crew_row_field(nk, req(), field="rk_satz", value="420"))
check("Preis je Reise gesetzt", ss.crew.rk_satz_of(_hz), 420.0)
check("Reisekosten neu gerechnet", ss.crew.row_rk(_hz), 1 * 420.0)
run(crew.crew_row_field(nk, req(), field="rk_satz", value="0"))
check("leer = halber Tagessatz", ss.crew.row_rk(_hz), 570.0 * 0.5)
_h2 = body(run(crew.crew_panel(req())))
# Der Tagessatz braucht Platz für vierstellige Beträge. Die allgemeine Regel
# `input[type=text] { width:100% }` ist spezifischer als eine einzelne Klasse — ein
# Attributselektor zählt wie eine Klasse, und `input` kommt dazu. Sie hat die Maße der
# Matrixfelder übersteuert, und das Feld schrumpfte mit dem Innenabstand der Zelle.
check("Matrixfelder schlagen die Formularregel",
      ".crew-table input.crew-rate             { width:56px; padding:2px 5px; }"
      in _css, True)
check("Anzahlfelder ebenso",
      ".crew-table .crew-hotel input.crew-cnt  { width:30px; }" in _css, True)
check("Feld in der Hotelspalte", 'data-field="hotel_satz"' in _h2, True)
check("mit dem Planungspreis als Platzhalter", 'placeholder="150"' in _h2, True)
check("Feld in der Reisespalte", 'data-field="rk_satz"' in _h2, True)
check("mit dem halben Tagessatz als Platzhalter", 'placeholder="285"' in _h2, True)
check("× zwischen Anzahl und Preis", _h2.count('class="crew-mal"'),
      2 * _h2.count('data-field="hotel_naechte"'))
check_in("unbekannte Kostenart", "Unbekannte Kostenart",
         body(run(crew.crew_kosten(req(), art="quatsch", resource_id=123))))

# Verteilung: anteilig nach Manntagen — oder komplett auf eine Position.
#
# Tageskosten gehen glatt im Verhältnis 3:5 auf. Bei den Nebenkosten wird die
# **Menge** verteilt und auf zwei Stellen gerundet, nicht der Betrag: `DaysInAction`
# hat in Easyjob zwei Nachkommastellen, ein anteiliger Betrag ließe sich dort gar
# nicht abbilden. Die Matrix zeigt deshalb genau das, was gebucht wird — auch wenn
# der Anteil dadurch um ein, zwei Euro von der reinen Verhältnisrechnung abweicht.
anteilig = {k: round(v["cost"]) for k, v in ss.crew.position_stats().items()}
_nk_je_pos = {}
for _a, _r, _item, _menge, _satz, _l in ss.crew.nebenkosten_posten(ss.crew.row(nk)):
    _nk_je_pos[_item] = _nk_je_pos.get(_item, 0.0) + _menge * _satz
check("anteilig verteilt", anteilig,
      {"p1": round(8 * 570 * 3 / 8 + _nk_je_pos["p1"]),
       "p2": round(8 * 570 * 5 / 8 + _nk_je_pos["p2"])})
check("und die Nebenkosten vollständig",
      round(sum(_nk_je_pos.values()), 2),
      round(ss.crew.row_nebenkosten(ss.crew.row(nk)), 2))

run(crew.crew_menu_add(req(), title="Licht"))
nkm = ss.crew.menu_keys()[0]
run(crew.crew_row_move_to(nk, req(), group_key=nkm))
r = run(crew.crew_menu_nk(req(), key=nkm, item_id="p2"))
gelenkt = {k: round(v["cost"]) for k, v in ss.crew.position_stats().items()}
check("Nebenkosten komplett auf p2",
      gelenkt, {"p1": 3 * 570, "p2": 5 * 570 + 1243})
# Beide Marken — Ziel und Nebenkosten — sind gleich aufgebaut und tragen die Farbe
# ihrer Position.
check_in("in der Überschrift sichtbar", 'class="crew-mp-titel">NK<', body(r))
check_in("mit Farbe", 'class="crew-mp-dot" style="background:', body(r))
run(crew.crew_menu_nk(req(), key=nkm, item_id="", action="remove"))
check("wieder anteilig",
      {k: round(v["cost"]) for k, v in ss.crew.position_stats().items()}, anteilig)
r = run(crew.crew_menu_nk(req(), key="eigen:99", item_id="p1"))
check_in("unbekannter Abschnitt", "nicht gefunden", body(r))
r = run(crew.crew_menu_nk(req(), key=nkm, item_id=""))
check_in("ohne Auswahl", "Erst oben eine Position", body(r))

# Spesen sind kein Eingabefeld mehr; Hotel und Reisen zeigen Anzahl und Betrag.
h = body(run(crew.crew_panel(req())))
check("Spesen werden gerechnet", 'data-field="spesen_satz"' in h, False)
check("Hotel und RK mit Betrag daneben", h.count('class="crew-nk-sum"'), 2)
check_in("Satzwähler in der Leiste", "Spesensatz Inland", h)


# ─── 21. Hotel und Reisen für mehrere Zeilen ─────────────────────────────────

print("\n[21] Ein Feld für mehrere Zeilen")

run(crew.crew_reset(req()))
ss.matches = {}
run(crew.crew_range(req(), date_from="2027-04-01", date_to="2027-04-08"))
run(crew.crew_pos_add(req(), item_id="p1"))
run(crew.crew_row_add(req(), resource_id=13, group_key=""))   # Rigger 570
run(crew.crew_row_add(req(), resource_id=6, group_key=""))    # Lichttechniker 520
ma, mb = [r.id for r in ss.crew.rows]
for rid in (ma, mb):
    for d in ss.crew.day_keys()[:5]:
        run(crew.crew_cell(req(), row_id=rid, day=d, persons=1, assign_to="p1"))

r = run(crew.crew_rows_field(req(), row_ids=f"{ma},{mb}",
                             field="hotel_naechte", value="4"))
check("Nächte für beide Zeilen", [x.hotel_naechte for x in ss.crew.rows], [4, 4])
# Die Spesen ziehen mit: 1 Tag halb, 4 Nächte voll.
check("Spesen ziehen mit",
      [round(ss.crew.row_spesen(x)) for x in ss.crew.rows],
      [round(1 * 16 + 4 * 32)] * 2)
check_in("Antwort ist das Panel", '<div id="crew-panel"', body(r))

run(crew.crew_rows_field(req(), row_ids=f"{ma},{mb}", field="rk_anzahl", value="1"))
# Reisekosten hängen am Tagessatz der jeweiligen Zeile, nicht an einem Pauschalwert.
check("Reisekosten je Zeile verschieden",
      [round(ss.crew.row_rk(x)) for x in ss.crew.rows], [285, 260])

r = run(crew.crew_rows_field(req(), row_ids="", field="hotel_naechte", value="2"))
check_in("ohne Zeile", "Keine Zeile ausgewählt", body(r))
r = run(crew.crew_rows_field(req(), row_ids=str(ma), field="label", value="x"))
check_in("nur Zahlenfelder", "lässt sich nicht für mehrere Zeilen", body(r))
r = run(crew.crew_rows_field(req(), row_ids=str(ma), field="hotel_naechte", value="drei"))
check_in("keine Zahl", "Zahl erwartet", body(r))
r = run(crew.crew_rows_field(req(), row_ids="9999", field="hotel_naechte", value="2"))
check("unbekannte Zeile bleibt folgenlos", "error-msg" in body(r), False)

# Preis je Hotelnacht: eine Zahl für die ganze Planung — derselbe Ort, dieselbe Zeit,
# derselbe Preis.
check("Vorgabe", ss.crew.hotel_satz, 150.0)
r = run(crew.crew_hotelsatz(req(), value="180"))
check("Hotelpreis geändert", ss.crew.hotel_satz, 180.0)
check("wirkt auf die Zeile", round(ss.crew.row_hotel(ss.crew.row(ma))), 4 * 180)
check_in("Feld in der Leiste", 'class="crew-hotelsatz"', body(r))
r = run(crew.crew_hotelsatz(req(), value="abc"))
check_in("keine Zahl", "Zahl erwartet", body(r))
run(crew.crew_hotelsatz(req(), value="150"))

_js4 = io.open(os.path.join(os.path.dirname(__file__), "..", "static",
                            "crew_matrix.js"), encoding="utf-8").read()
check("Wert gilt auch beim Verlassen des Feldes",
      "if (!fuelleZeilen(el)) saveField(el);" in _js4, True)
check("eine gemeinsame Funktion dafür", _js4.count("function fuelleZeilen"), 1)

# Der Zeilenkopf ist wieder eine gewöhnliche Zelle: mit display:flex wächst sie nicht
# auf die Zeilenhöhe, und die Tageszellen schauten samt Rahmen darunter hervor.
h = body(run(crew.crew_panel(req())))
check("Flex steckt im inneren Element", h.count('class="crew-head-inner"'), 2)
_css3 = io.open(os.path.join(os.path.dirname(__file__), "..", "static",
                             "style.css"), encoding="utf-8").read()
check("Kopfzelle selbst ohne Flex",
      ".crew-row .crew-head            { display:flex" in _css3, False)
# Ebenen und Hintergründe der geklebten Spalte liegen an EINER Stelle. Vorher waren
# sie über fünf Stellen verstreut, und zuletzt färbte eine allgemeine Regel die
# Überschriften weiß — beim Scrollen sah die geklebte Zelle wie eine Lücke aus.
check("Zeilenkopf deckend weiß",
      ".crew-table tr.crew-row    td.crew-head { z-index:3; background:#fff; }" in _css3, True)
check("Überschrift behält ihr Grau",
      ".crew-table tr.crew-gewerk td.crew-menu-bar { z-index:5; background:#eef1f6; }"
      in _css3, True)
check("Positionsleiste klebt mit",
      "left:var(--crew-kopf-ist, var(--crew-kopf));" in _css3, True)
# Der Anschlag ist die gemessene Breite der Kopfspalte. Eine feste Zahl stimmt nur,
# solange die Spalte exakt so breit ist — ist sie breiter, rutscht die Leiste bis
# dahin mit, und die Überschrift wandert beim Scrollen ein Stück weg.
check("Breite wird gemessen", "function messeKopf(" in _js4, True)
check("beim Laden und nach jedem Tausch",
      _js4.count("messeKopf();") >= 2, True)
check("und beim Verändern des Fensters",
      'addEventListener("resize", messeKopf)' in _js4, True)
# Gemessen wird, wo die Zelle mit den Marken anfängt — nicht die Breite der
# Kopfspalte. Sonst fehlen Rahmen und Zellabstand.
_css4 = _css3
check("Anfang der Leiste statt Breite der Spalte",
      'tab.querySelector("td.crew-menu-bar")' in _js4, True)
# Das Padding lag auf der Zelle: der geklebte Inhalt beginnt damit rechts von der
# Zellkante, während der Anschlag auf die Kante zeigt — um genau diese Differenz
# rutschte die Leiste, bis sie stand. Padding gehört deshalb nach innen.
check("Zelle ohne Padding",
      ".crew-menu-bar      { padding:0 !important; white-space:nowrap; }"
      in _css4, True)
check("Padding im geklebten Streifen",
      "                                  padding:0 8px; }" in _css4, True)
# Hotel und Reisen tragen je zwei Felder und ein × dazwischen — ohne eigene Breite
# rechnet die Zelle nur mit einem und schneidet das zweite ab.
check("eigene Breite für Anzahl mal Preis",
      ".crew-hotel                     { min-width:108px; }" in _css3, True)
check("× zwischen den Feldern", ".crew-mal" in _css3, True)
# Die Trennlinie zwischen den Kostenspalten stand direkt an den Feldern und ging
# dadurch unter.
check("Trennlinie mit Luft",
      ".crew-table .crew-cost          { padding-left:11px;" in _css3, True)
# `.crew-gewerk` war ein Rest vom alten Gewerk-Eingabefeld. Die Klasse trägt heute
# die Überschriftzeile, und display:block hat sie aus dem Tabellenraster geworfen:
# die geklebte Spalte hörte auf zu kleben, die Marken landeten unter den Zeilen.
check("kein display:block auf der Überschriftzeile",
      ".crew-gewerk        { display:block" in _css3, False)
# Eine Tabellenzelle richtet sich im automatischen Layout nach ihrem Inhalt —
# max-width bremst sie nicht. Ohne min-width:0 schrumpft der Name nicht unter seine
# Textbreite und zieht die Spalte auf mehr als das Doppelte auf.
check("Name darf schrumpfen",
      ".crew-head-inner .crew-label    { min-width:0;" in _css3, True)
check("Kopfspalte an einer Stelle",
      "--crew-kopf:" in _css3, True)
check("keine allgemeine weiße Übermalung",
      ".crew-table td.crew-head        { z-index:3; background:#fff; }" in _css3, False)
check("Titelfeld mit fester Höhe",
      "height:17px; line-height:17px;" in _css3, True)
# Hotel und Reisen hatten den Betrag unter der Anzahl stehen — zwei Zeilen, und
# damit war jede Matrixzeile eineinhalb Zeilen hoch. Der Betrag steht im Tooltip.
check("Zeilenhöhe festgenagelt",
      ".crew-table tr.crew-row td      { height:23px; }" in _css3, True)
# Ausgewählte Zeilen werden auch in den Kostenspalten markiert — sonst sieht man
# nicht, worauf Hotel und Reisen mit Enter übertragen werden.
# Markiert wird genau, was aufgezogen wurde — wie vorne in der Matrix. Vorher lag
# die Markierung auf der ganzen Zeile und färbte auch Felder, die nichts abbekommen.
# Die Auswahlfarbe muss auf dem Eingabefeld liegen, nicht nur auf der Zelle: die
# allgemeine Regel `input[type=text] { background:#fff }` ist spezifischer als
# `.crew-in` und übermalt sie sonst. Sichtbar blieb nur der Rand — und der geht
# unter, sobald eine Zahl im Feld steht.
check("Auswahl färbt das Feld",
      ".crew-table .crew-cell.crew-sel input.crew-in {" in _css3, True)
check("Auswahl sichtbar am Feld",
      ".crew-rate.crew-rate-sel        { background:#ede7f6;" in _css3, True)
check("nicht an der ganzen Zeile",
      "crew-sel-row" in _css3, False)
# Ziehen darf auch neben dem Feld beginnen. Sonst fängt der Browser über den
# Beträgen eine Textmarkierung an, die als zweite Auswahl darüberliegt.
check("Ziehen beginnt auch neben dem Feld",
      'el.closest("#crew-table td.crew-cost")' in _js4, True)
# Die Ressourcensuche steht unter Matrix und Positionsliste — dort wird zugeordnet.
# Sie bleibt dabei außerhalb von #crew-panel, sonst flackert sie bei jeder Änderung.
_imp = io.open(os.path.join(os.path.dirname(__file__), "..", "templates",
                            "import.html"), encoding="utf-8").read()
check("Suche unter der Matrix",
      _imp.index("partials/crew_search.html")
      > _imp.index("partials/crew_matrix.html"), True)
check("und der Knopf führt hin", "scrollIntoView" in _js4, True)
# Der Anlege-Knopf darf beim Zwischenspeichern nicht mitreagieren. Zwei Wege führten
# dorthin: htmx vererbt hx-indicator und hx-disabled-elt aus dem Formular an alles
# darin, und htmx-Ereignisse steigen zum Formular auf.
check("Zwischenspeichern mit eigenem Indikator",
      'hx-indicator="this" hx-disabled-elt="this"' in _imp, True)
check("Knopftext nicht beim Zwischenspeichern",
      "if (event.detail.elt.id !== 'imp-draft-save-btn')" in _imp, True)
# Der Vergleich mit dem Formular selbst griff zu kurz: bei einem Absende-Knopf ist
# das Ziel der Knopf, und damit sprang der Anlege-Knopf gar nicht mehr um.
check("nicht über das Ereignisziel", "event.target === this" in _imp, False)
# Die Kennzahlenzeile kommt bei jeder Änderung der Matrix mit — die Personalkosten
# stehen darüber und zeigten sonst die Summe von vorhin.
check("Kennzahlen als eigene Vorlage",
      'partials/import_metrics.html' in _imp
      or 'partials/import_metrics.html' in io.open(
          os.path.join(os.path.dirname(__file__), "..", "templates", "partials",
                       "import_groups.html"), encoding="utf-8").read(), True)
check("Matrix schickt sie mit",
      "from routes.import_ import metrics_oob_html" in io.open(
          os.path.join(os.path.dirname(__file__), "..", "routes", "crew.py"),
          encoding="utf-8").read(), True)
_hm = body(run(crew.crew_panel(req())))
check("und sie hängt an der Antwort", 'id="imp-metrics"' in _hm, True)
check("als Out-of-band-Stück", 'hx-swap-oob="true"' in _hm, True)
# „+ Menüpunkt" steht am Fuß der Matrix in derselben Spalte wie die übrigen
# Überschriften — dort entsteht der Abschnitt. Der Chip in der Positionsliste
# entfällt: zweimal derselbe Knopf verwirrt mehr, als er hilft.
_h4 = body(run(crew.crew_panel(req())))
check("Knopf am Fuß der Matrix", 'class="crew-neu-btn"' in _h4, True)
check("nicht mehr als Chip", "crew-chip-new" in _h4, False)
# „+ Ressource" steht daneben, im klebenden Streifen — sonst wäre der Knopf weg,
# sobald jemand nach rechts scrollt.
check("beide Knöpfe am Fuß", _h4.count('class="crew-neu-btn"'), 2)
check("und nicht mehr in der Kopfleiste",
      "crewToggleSearch()" in _h4.split('class="crew-actions"')[1].split("</div>")[0],
      False)
# Die Anleitung steht ganz unten auf der Seite, unter der Ressourcenauswahl — nicht
# mehr im Panel, das bei jeder Änderung getauscht wird.
check("Anleitung nicht mehr im Panel", "crew-foot" in _h4, False)
check("sondern unter der Suche",
      _imp.index("crew-foot") > _imp.index("partials/crew_search.html"), True)
# Der frisch angelegte Abschnitt bekommt den Fokus, sonst muss man ihn erst suchen
# und doppelklicken, um ihn zu benennen.
_h5 = body(run(crew.crew_menu_add(req(), title="")))
check("neuer Abschnitt ist markiert", 'data-fresh="1"' in _h5, True)
check("und nur einer", _h5.count('data-fresh="1"'), 1)
check("das Panel sonst nicht", 'data-fresh="1"' in body(run(crew.crew_panel(req()))),
      False)
# Die beiden Auswahlen sind getrennt: vorne Tage, hinten Zeilen. Eine Tagesauswahl
# in der Matrix hat vorher die Kostenfelder mitgefärbt, obwohl dort nichts passiert
# — und mit Enter still auf alle Zeilen übertragen.
check("Tagesauswahl markiert hinten nichts",
      "var spalte = sel && !sel.from ? sel.feld : null;" in _js4, True)
check("und überträgt dort nichts",
      "if (!sel || sel.from || sel.rows.length < 2 || !window.htmx) return false;"
      in _js4, True)
# Eine Zahl für zwei verschiedene Felder gleichzeitig ergibt keinen Sinn: 600 als
# Tagessatz ist etwas anderes als 600 Hotelnächte. Die Auswahl bleibt in ihrer Spalte.
check("Auswahl merkt sich die Spalte", "feld: a.feld, anchor: a" in _js4, True)
check("und überträgt nur dort",
      "if (sel.feld !== input.dataset.field) return false;" in _js4, True)
check("seitliches Abkommen ändert die Spalte nicht",
      "feld: sel.feld });" in _js4, True)
# MT, Spesen und Summe werden gerechnet — dort gibt es kein Feld und nichts
# auszuwählen.
check("gerechnete Spalten sind nicht wählbar",
      "if (!feld || !zeile) { loescheAuswahl(); return; }" in _js4, True)


# ─── 22. Zeilen über die Kostenspalten wählen ────────────────────────────
# Mit der Maus über Hotel oder Reisen ziehen wählt die Zeilen aus. Vorher ging das
# nur über die Tage in der Matrix — darauf kommt niemand, der ein Feld in mehreren
# Zeilen setzen will.
print(chr(10) + "── 22. Zeilenauswahl in den Kostenspalten ──")
_js5 = io.open(os.path.join(os.path.dirname(__file__), "..", "static",
                            "crew_matrix.js"), encoding="utf-8").read()
check("eigener Zieh-Zustand", "ziehtZeilen" in _js5, True)
check("Auswahl ohne Tage", "function zeilenBereich(" in _js5, True)
check("Ziehen beginnt am Kostenfeld",
      'contains("crew-rate")' in _js5 and "ziehtZeilen = true;" in _js5, True)
# Beim Ziehen liegt der Zeiger schnell neben dem Feld — die Zelle zählt mit, sonst
# reißt die Auswahl ab.
check("Zelle zählt mit", 'closest("td.crew-cost")' in _js5, True)
check("Ziehen endet beim Loslassen",
      "zieht = false; ziehtZeilen = false;" in _js5, True)
# Eine Auswahl ohne Tage darf nichts in die Matrix schreiben: weder das Füllen von
# Zellen noch das Umhängen auf eine Position hat dann einen Tagesbereich.
check("Füllen verlangt Tage", "if (!sel || !sel.from || !window.htmx) return false;"
      in _js5, True)
check("Umhängen verlangt Tage",
      "if (sel && sel.from && (sel.rows.length > 1" in _js5, True)
check("eigener Hinweistext für die Kostenspalten",
      'sel.rows.length + " Felder gewählt' in _js5, True)
# Der Betrag steht jetzt im Tooltip statt in einer zweiten Zeile — er muss beim
# Rechnen mitwandern, sonst nennt er später eine Zahl, die nicht mehr stimmt.
check("Tooltip wird nachgeführt", "function nkTitel(" in _js5, True)
_h = body(run(crew.crew_panel(req())))
check("Betrag ausgeblendet", 'class="crew-nk-sum" id="crew-ho-' in _h
      and 'hidden>' in _h, True)
check("Betrag im Tooltip", "€ je Nacht =" in _h, True)
check("Marken in klebendem Streifen", 'class="crew-mp-bar"' in _h, True)
# Beim Tausch des Panels entsteht der Scroll-Bereich neu und fängt wieder links an.
# Wer weit rechts eine Position zuordnet, landet sonst am Anfang der Zeitachse.
check("Scrollstand wird gemerkt",
      'addEventListener("htmx:beforeSwap"' in _js5 and "merkeScroll()" in _js5, True)
check("und wiederhergestellt", "stelleScrollHer();" in _js5, True)
check("waagerecht und senkrecht",
      "box.scrollLeft = scrollStand.x;" in _js5
      and "box.scrollTop = scrollStand.y;" in _js5, True)


# ─── Ergebnis ────────────────────────────────────────────────────────────────

print("\n" + "=" * 62)
if _fails:
    print(f"FEHLGESCHLAGEN: {len(_fails)}")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("Alle Prüfungen bestanden.")
