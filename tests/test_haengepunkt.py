"""Hängepunkt Satz Material pauschal — wann sie gebucht wird.

Zwei Lücken, die vorher zu fehlenden Pauschalen führten:

1. Erkannt wurde nur der GAEB-Kurztext, und `\\bmotor\\b` scheitert an
   Zusammensetzungen („Kettenzugmotor"). Jetzt zählt auch der zugeordnete
   Artikel: ein Motorkettenzug löst die Pauschale aus, selbst wenn die Position
   nur „Punktzug 1t" heißt.

2. Beim manuellen Zuordnen im Artikel-Dialog lief die Regel gar nicht — die
   Pauschale fehlte still. Jetzt wird sie bei jedem Setzen neu abgeglichen:
   dazu, wenn ein Hebezeug gebucht ist, weg beim Wechsel auf etwas anderes,
   und nie doppelt.

Steuerungen und Kabel („Motorsteuerung", „Motorkabel") lösen bewusst NICHT aus.

    .venv/Scripts/python.exe tests/test_haengepunkt.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import state
from state import HAENGEPUNKT_NR
from gaeb_parser import GaebItem, GaebProject
from matcher import (UnifiedMatcher, is_motor_article, is_motor_position,
                     load_articles_db, load_resources_db)
from routes.import_ import router

app = FastAPI(); app.include_router(router)
app.add_middleware(SessionMiddleware, secret_key="t" * 40, session_cookie="t")
M = UnifiedMatcher(load_articles_db(), load_resources_db())
fails = []

MOTOR = "1002393.00"   # Movecat 0500PLUS Motor D8-Plus — Artikelart Motorkettenzug
HAND  = "1000711.00"   # Handkettenzug 1t 8m
TRUSS = "1006453.00"   # Eurotruss schwarz HD33 300cm Traverse


def check(label, got, want):
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {label}: {got}" + ("" if ok else f"  (erwartet {want})"))
    if not ok:
        fails.append(label)


def art(num):
    idx = M._num_to_idx.get(num)
    assert idx is not None, f"Artikel {num} fehlt im Stamm"
    return M._pool[idx]


def item(iid, desc, qty):
    return GaebItem(item_id=iid, rno_part=0, oz=iid, description=desc,
                    long_text="", qty=qty, unit="St", category_path=[])


print("\n── Positionstext ───────────────────────────────────────────────────")
for text, want in [("Motor 1t", True), ("Motoren 500kg", True),
                   ("Kettenzugmotor 1t", True), ("E-Motor 250kg", True),
                   ("Elektrokettenzug 1000kg", True), ("Hebezeug 2t", True),
                   ("Motorsteuerung 8-fach", False), ("Motorkabel 15m", False),
                   ("Punktzug 1t", False)]:
    check(text, is_motor_position(text), want)

print("\n── Artikel ─────────────────────────────────────────────────────────")
check("Motorkettenzug", is_motor_article(art(MOTOR)), True)
check("Handkettenzug",  is_motor_article(art(HAND)),  True)
check("Traverse",       is_motor_article(art(TRUSS)), False)
check("Pauschale selbst", is_motor_article(art(HAENGEPUNKT_NR)), False)


print("\n── Buchung beim manuellen Zuordnen ─────────────────────────────────")


def hp_qty(ss, iid):
    """Menge der Hängepunkt-Pauschale im Bundle der Position (None = nicht gebucht)."""
    for b in ss.bundles.get(iid, []):
        if getattr(b.get("article"), "nummer", None) == HAENGEPUNKT_NR:
            return b["qty"]
    return None


with TestClient(app) as c:
    c.get("/api/import/booking-qty/x")          # Session anlegen
    ss = list(state._sessions.values())[-1]
    ss.matcher = M
    ss.d83_project = GaebProject(
        name="t", label="", phase="", date="", currency="EUR",
        items=[item("p1", "Punktzug 1t", 6),       # Text nennt keinen Motor
               item("p2", "Motor 1t", 4),
               item("p3", "Vierholmtraverse", 3)],
    )

    def setm(iid, num):
        c.post(f"/api/import/set-match/{iid}", data={
            "ej_num": num, "qty": 1,
            "raw_json": '{"Number": "%s", "Caption": "x"}' % num,
        })

    setm("p1", MOTOR)
    check("Text ohne Motor + Motor-Artikel → 6", hp_qty(ss, "p1"), 6.0)
    setm("p1", MOTOR)
    check("zweimal gesetzt → keine Dublette",    hp_qty(ss, "p1"), 6.0)
    check("p1 Bundle-Größe",                     len(ss.bundles.get("p1", [])), 1)

    setm("p3", MOTOR)
    check("Traversen-Position + Motor → 3",      hp_qty(ss, "p3"), 3.0)
    setm("p3", TRUSS)
    check("zurück auf Traverse → weg",           hp_qty(ss, "p3"), None)

    setm("p2", TRUSS)
    check("Text nennt Motor → trotzdem dabei",   hp_qty(ss, "p2"), 4.0)

print()
if fails:
    print(f"FEHLER in {len(fails)} Prüfungen: {', '.join(fails)}")
    sys.exit(1)
print("Alle Prüfungen bestanden.")
