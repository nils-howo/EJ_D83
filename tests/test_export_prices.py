"""Regressionstest der Einheitspreis-Berechnung für den Export.

Kernannahme: eine Easyjob-Gruppe = eine LV-Position (Import-Modus „Positionen").
Im Modus „Gruppen" teilen sich mehrere Positionen eine Gruppe — dann muss die
Gruppensumme verteilt werden, sonst ist die Angebotssumme ein Vielfaches zu hoch.

    .venv/Scripts/python.exe tests/test_export_prices.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

from routes.projects import _export_ep, _shared_group_note


def costs(group_by_item, art, pers, bookings):
    ipg, ic = {}, {}
    for b in bookings:
        gid, iid = b["ej_group_id"], b["item_id"]
        if gid:
            ipg.setdefault(gid, [])
            if iid not in ipg[gid]: ipg[gid].append(iid)
        ic[iid] = ic.get(iid, 0.0) + b["qty"] * b["unit_price"]
    return {"group_by_item": group_by_item, "oz_to_group": {},
            "group_art_cost": art, "group_pers_cost": pers,
            "items_per_group": ipg, "item_cost": ic}

# ── A) Modus "Positionen": eine Gruppe = eine Position (1:1)
c = costs({"p1": 100}, {100: 900.0}, {100: 100.0},
          [{"ej_group_id": 100, "item_id": "p1", "qty": 10, "unit_price": 90}])
ep = _export_ep(c, "p1", "1.1", 10.0)
print(f"A) 1:1 — Gruppenkosten 1000, Menge 10 -> EP {ep}, Gesamt {ep*10:.2f}")
assert ep == 100.0 and ep * 10 == 1000.0, ep

# ── B) Modus "Gruppen": drei Positionen teilen eine Gruppe (1000 EUR)
#     Gebuchte Kosten beim Import: p1 600, p2 300, p3 100
bk = [
    {"ej_group_id": 200, "item_id": "p1", "qty": 10, "unit_price": 60},   # 600
    {"ej_group_id": 200, "item_id": "p2", "qty":  5, "unit_price": 60},   # 300
    {"ej_group_id": 200, "item_id": "p3", "qty":  2, "unit_price": 50},   # 100
]
c = costs({"p1": 200, "p2": 200, "p3": 200}, {200: 900.0}, {200: 100.0}, bk)
total = 0.0
for iid, qty, erwartet_anteil in [("p1", 10.0, 0.6), ("p2", 5.0, 0.3), ("p3", 2.0, 0.1)]:
    e = _export_ep(c, iid, "", qty)
    gp = e * qty
    total += gp
    print(f"B) {iid}: Menge {qty:4} -> EP {e:8.3f} -> Gesamt {gp:8.2f} "
          f"(Anteil {gp/1000:.0%}, erwartet {erwartet_anteil:.0%})")
    assert abs(gp - 1000 * erwartet_anteil) < 0.05, (iid, gp)
print(f"   Summe = {total:.2f}  (Gruppe kostet 1000.00)")
assert abs(total - 1000.0) < 0.05, total

# ── C) Geteilte Gruppe ohne belastbare Preise -> gleichmäßig
bk0 = [{"ej_group_id": 300, "item_id": i, "qty": 1, "unit_price": 0} for i in ("a", "b")]
c = costs({"a": 300, "b": 300}, {300: 500.0}, {}, bk0)
ea = _export_ep(c, "a", "", 1.0); eb = _export_ep(c, "b", "", 1.0)
print(f"C) ohne Preise: EP a={ea} b={eb}, Summe {ea+eb:.2f} (Gruppe 500.00)")
assert ea == eb == 250.0, (ea, eb)

print("\nEP-Logik ok: Summe der Positionen = Summe der Gruppen, in beiden Modi.")
