"""Regressionstest für die Buchung der Personalplanung nach Easyjob.

Prüft die eine Sache, die beim Zusammenführen von Matching und Matrix schiefgehen
kann und die man in Easyjob erst merkt, wenn das Angebot schon draußen ist: **dieselbe
Leistung zweimal**. Bisher entstand je Personalposition eine RFA aus dem Matching;
kommt die Matrix dazu, gäbe es beides.

Gebucht wird gegen eine Attrappe statt gegen den SQL-Server: geprüft wird, welche
Zeilen entstehen, nicht ob der Treiber sie schreibt.

    .venv/Scripts/python.exe tests/test_crew_booking.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="crewbook_"), "test.db")
os.environ["DB_PATH"] = _TMP_DB

from crew_plan import CrewPlan, bookings          # noqa: E402

_fails: list[str] = []


def check(name, ist, soll):
    ok = ist == soll
    print(("  ✓ " if ok else "  ✗ ") + name + ": " + repr(ist)
          + ("" if ok else "  ERWARTET " + repr(soll)))
    if not ok:
        _fails.append(name)


# ─── Aufbau: zwei Positionen, eine davon geplant ─────────────────────────────
# p1 „Tontechniker" steht in der Matrix, p2 „Lichttechniker" nicht. Beide haben im
# Matching eine Ressource — p1 darf trotzdem nur einmal gebucht werden.
print("── 1. Was die Planung bucht ──")

plan = CrewPlan(date_from="2026-03-09", date_to="2026-03-12")
plan.positions = ["p1", "p2"]
plan.spesen_satz, plan.hotel_satz = 32.0, 150.0
r1 = plan.add_row("Ton-Operator", 501)
r1.tagessatz, r1.eigenkosten = 600.0, 400.0
for d, n in [("2026-03-09", 2), ("2026-03-10", 2), ("2026-03-11", 1)]:
    plan.set_cell(r1.id, d, n)
plan.assign_days(r1.id, "2026-03-09", "2026-03-11", "p1")

lines = bookings(plan)
check("nur geplante Positionen", sorted({b.item_id for b in lines}), ["p1"])
check("p2 bleibt Sache des Matchings", any(b.item_id == "p2" for b in lines), False)

# ─── Die Auswahl, die der Import trifft ──────────────────────────────────────
# Genau diese Menge entscheidet, was aus dem Matching herausfällt. Maßgeblich ist,
# worauf die Matrix Manntage legt — nicht, welche Positionen in ihr auftauchen: eine
# Position ohne eingetragene Tage bleibt Sache des Matchings, sonst fiele sie still
# weg.
print(chr(10) + "── 2. Auswahl für den Import ──")

crew_items = {b.item_id for b in lines}
check("p1 fällt aus dem Matching", "p1" in crew_items, True)
check("p2 nicht", "p2" in crew_items, False)

leer = CrewPlan(date_from="2026-03-09", date_to="2026-03-12")
leer.positions = ["p1"]
leer.add_row("Ohne Tage", 502)
check("Position ohne Manntage fällt nicht heraus",
      {b.item_id for b in bookings(leer)}, set())

# Nur Tageskosten lösen das Matching ab. Lenkt ein Abschnitt seine Nebenkosten auf
# eine Position, die selbst keine Manntage trägt, behält die ihr eigenes Personal —
# sonst bliebe es ungebucht, und das merkt niemand.
nk = CrewPlan(date_from="2026-03-09", date_to="2026-03-12")
nk.positions = ["p1", "p2"]
nk.set_pos_mode("p1", "menu")
nk.set_menu_pos("p1", "p1")
nk.set_nk_pos("p1", "p2")
zr = nk.add_row("Ton", 501, group_key="p1")
zr.tagessatz, zr.hotel_naechte = 600.0, 2
nk.set_cell(zr.id, "2026-03-09", 1)
nk.assign_days(zr.id, "2026-03-09", "2026-03-09", "p1")
_z = bookings(nk)
check("Nebenkosten landen auf p2",
      {b.item_id for b in _z if b.kind != "tage"}, {"p2"})
check("Tageskosten auf p1", {b.item_id for b in _z if b.kind == "tage"}, {"p1"})
check("nur p1 löst das Matching ab",
      {b.item_id for b in _z if b.kind == "tage"}, {"p1"})

# ─── Termine ─────────────────────────────────────────────────────────────────
# Der bisherige Import setzte für alles den Projektstart und den Tag darauf. Damit
# klumpt die ganze Crew am ersten Tag, und die Personaldisposition in Easyjob ist
# wertlos. Aus der Matrix kommen echte Termine.
print(chr(10) + "── 3. Termine ──")

tage = [b for b in lines if b.kind == "tage"]
# Zwei Leute an zwei Tagen sind EINE Zeile mit Anzahl 2 — Easyjob führt dafür
# Quantity und rechnet TotalPrice = Quantity × Tage × Satz.
check("Kopfzahl statt Mehrfachzeilen",
      [(b.day_from, b.day_to, b.count, b.days) for b in tage],
      [("2026-03-09", "2026-03-10", 2, 2.0),
       ("2026-03-11", "2026-03-11", 1, 1.0)])
check("mit Uhrzeit", [(b.start_dt().hour, b.end_dt().hour) for b in tage],
      [(8, 18), (8, 18)])
check("nicht alles am Projektstart",
      len({(b.day_from, b.day_to) for b in tage}), 2)

# ─── Nebenkosten auf eigene Ressourcen ───────────────────────────────────────
print(chr(10) + "── 4. Nebenkosten ──")

r1.hotel_naechte, r1.rk_anzahl = 2, 1
lines2 = bookings(plan)
arten = {}
for b in lines2:
    arten.setdefault(b.kind, set()).add(b.resource_id)
check("Tageskosten auf der Person", arten.get("tage"), {501})
check("Spesen, Hotel und Reisen je eigene Ressource",
      (arten.get("spesen"), arten.get("hotel"), arten.get("reise")),
      ({123}, {37}, {126}))
check("nicht als Zuschlag auf den Tagessatz",
      {b.day_pay for b in lines2 if b.kind == "tage"}, {600.0})

# Die Summe der Buchungen ist die Summe, die die Matrix anzeigt. Weicht sie ab, fällt
# das erst beim Abgleich der Angebotssumme auf — und dann sucht sie niemand mehr.
check("Summe wie in der Matrix",
      round(sum(b.total for b in lines2), 2), round(plan.row_total(r1), 2))

# Nebenkosten liegen auf einem eigenen Tag zwei Tage vor dem Einsatz, nicht über der
# ganzen Spanne — sonst legen sie sich in der Personaldisposition quer über alles.
_nk = [b for b in lines2 if b.kind != "tage"]
check("Nebenkosten vor dem Einsatz",
      {b.day_from for b in _nk}, {"2026-03-07"})
check("und nur an diesem einen Tag", {b.day_to for b in _nk}, {"2026-03-07"})

# ─── Eigenkosten ────────────────────────────────────────────────────────────
# TotalCosts ist die Gegenrechnung zum Weiterbelasten. Ohne sie steht in Easyjob ein
# Deckungsbeitrag, der dem Umsatz entspricht.
check("Eigenkosten mitgegeben",
      {b.fixed_cost for b in lines2 if b.kind == "tage"}, {400.0})
check("Nebenkosten ohne Eigenkosten",
      {b.fixed_cost for b in lines2 if b.kind != "tage"}, {0.0})


# ─── Matrix und Buchung sagen dasselbe ───────────────────────────────────────
# Der Einheitspreis einer Position entsteht in Easyjob aus dem, was auf ihre Gruppe
# gebucht wird. Weicht das von dem ab, was die Matrix je Position anzeigt, merkt es
# niemand — bis jemand die Zahlen nebeneinanderlegt. Deshalb hier gegen viele
# Zufallsplanungen geprüft, nicht nur an einem Beispiel.
print(chr(10) + "── 5. Matrix je Position == Buchung je Position ──")

import random                                             # noqa: E402

_rnd = random.Random(7)
_schief = []
for _lauf in range(200):
    q = CrewPlan(date_from="2026-03-01", date_to="2026-03-20")
    _tage = q.day_keys()
    q.positions = ["p1", "p2", "p3"]
    q.spesen_satz = _rnd.choice([32.0, 62.0])
    q.hotel_satz = _rnd.choice([0.0, 120.0, 150.0])
    for i in range(_rnd.randint(1, 5)):
        z = q.add_row(f"Zeile {i}", 500 + i)
        z.tagessatz = _rnd.choice([0.0, 500.0, 600.0, 1020.0])
        z.eigenkosten = z.tagessatz * 0.6
        z.hotel_naechte = _rnd.randint(0, 8)
        z.rk_anzahl = _rnd.randint(0, 3)
        if _rnd.random() < 0.3:
            z.hotel_satz = _rnd.choice([0.0, 210.0])
        if _rnd.random() < 0.3:
            z.rk_satz = _rnd.choice([0.0, 480.0])
        for d in _tage:
            if _rnd.random() < 0.5:
                q.set_cell(z.id, d, _rnd.randint(1, 4))
        # Ein Teil der Tage bleibt ohne Position — die dürfen nichts abbekommen.
        for d in _tage:
            if z.cells.get(d) and _rnd.random() < 0.7:
                q.assign_days(z.id, d, d, _rnd.choice(["p1", "p2", "p3"]))
    _stats = q.position_stats()
    _gebucht = {}
    for b in bookings(q):
        _gebucht[b.item_id] = round(_gebucht.get(b.item_id, 0.0) + b.total, 2)
    for _item in set(_stats) | set(_gebucht):
        _soll = round(_stats.get(_item, {}).get("cost", 0.0), 2)
        _ist = round(_gebucht.get(_item, 0.0), 2)
        if abs(_soll - _ist) > 0.02:
            _schief.append((_lauf, _item, _soll, _ist))

check("200 Zufallsplanungen ohne Abweichung", _schief[:3], [])


print(chr(10) + "=" * 62)
if _fails:
    print(f"FEHLGESCHLAGEN: {len(_fails)}")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("Alle Prüfungen bestanden.")
