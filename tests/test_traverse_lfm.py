"""Laufmeter→Stück-Umrechnung und Übernahme auf gleiche Positionen.

Zwei Regeln, die vorher falsch bzw. gar nicht liefen:

1. Geteilt wird nur, wenn der GEBUCHTE ARTIKEL wirklich ein gerades
   Traversenstück bekannter Länge ist. Früher hing die Umrechnung allein an der
   Einheit der GAEB-Position und der 3-m-Standardlänge — jede Position in „m"
   wurde durch 3 geteilt, auch wenn ein 1-m-Stück, eine Ecke oder ein Kabel
   gebucht war.

2. Beim Zuordnen eines Artikels ziehen die gleichlautenden Positionen WEITER
   UNTEN mit: eigene Menge, eigene Traversen-Umrechnung, Zusatzartikel im
   gleichen Verhältnis zur Hauptmenge. Von Hand gesetzte Positionen bleiben
   unangetastet.

    .venv/Scripts/python.exe tests/test_traverse_lfm.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from gaeb_parser import GaebItem, GaebProject
from matcher import (MatchResult, UnifiedMatcher, article_piece_length_m,
                     load_articles_db, load_resources_db)
from routes.import_ import _apply_match_to_twins, _booking_entry, _twin_items

M = UnifiedMatcher(load_articles_db(), load_resources_db())
fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {label}: {got}" + ("" if ok else f"  (erwartet {want})"))
    if not ok:
        fails.append(label)


def art(num):
    idx = M._num_to_idx.get(num)
    assert idx is not None, f"Artikel {num} fehlt im Stamm"
    return M._pool[idx]


def item(iid, desc, qty, unit):
    return GaebItem(item_id=iid, rno_part=0, oz=iid, description=desc,
                    long_text="", qty=qty, unit=unit, category_path=[])


TRUSS_3M = art("1006453.00")   # Eurotruss schwarz HD33 300cm Traverse
TRUSS_1M = art("1006449.00")   # Eurotruss schwarz HD33 100cm Traverse
ECKE     = art("1006454.00")   # Eurotruss schwarz HD33-L90 Ecke 2Weg 90°
KABEL    = art("1004187.00")   # kein Traversenstück
PIPE_3M  = art("1000749.00")   # Pipe Alu silber 50mm / 300cm
HB_3M    = art("1004172.00")   # HB-Rohr silber 50mm / 300cm
HB_VERB  = art("1004230.00")   # HB-Rohr Verbinder kurz — kein Längenstück

print("\n── Stücklänge aus dem Artikel ──────────────────────────────────────")
check("3-m-Traverse",  article_piece_length_m(TRUSS_3M), 3.0)
check("1-m-Traverse",  article_piece_length_m(TRUSS_1M), 1.0)
check("Ecke",          article_piece_length_m(ECKE),     None)
check("Kabel",         article_piece_length_m(KABEL),    None)
# Pipes und HB-Rohre heißen nicht "Traverse" und haben eigene Artikelarten
# (Aluminiumrohr / Aluminium-Doppelrohr) — sie zählen trotzdem als Längenstück.
check("3-m-Pipe",      article_piece_length_m(PIPE_3M),  3.0)
check("3-m-HB-Rohr",   article_piece_length_m(HB_3M),    3.0)
check("HB-Verbinder",  article_piece_length_m(HB_VERB),  None)

print("\n── Buchungsmenge für 12 m ──────────────────────────────────────────")
pos12 = item("1", "Vierholmtraverse", 12, "m")
check("3-m-Stück → 4 Stück",   _booking_entry(TRUSS_3M, pos12)["qty"], 4.0)
check("1-m-Stück → 12 Stück",  _booking_entry(TRUSS_1M, pos12)["qty"], 12.0)
check("Ecke → unverändert",    _booking_entry(ECKE,     pos12)["qty"], 12.0)
check("Kabel → unverändert",   _booking_entry(KABEL,    pos12)["qty"], 12.0)
check("Ecke ohne lfm-Hinweis", _booking_entry(ECKE, pos12)["lfm_converted"], False)

print("\n── Stück-Einheit bleibt unangetastet ───────────────────────────────")
check("5 St → 5", _booking_entry(TRUSS_3M, item("2", "Vierholmtraverse", 5, "St"))["qty"], 5.0)


print("\n── Übernahme auf gleiche Positionen weiter unten ───────────────────")


class _SS:
    pass


ss = _SS()
ss.d83_project = GaebProject(
    name="t", label="", phase="", date="", currency="EUR",
    items=[
        item("a", "Vierholmtraverse HD34",  9, "m"),   # Quelle → 3 Stück
        item("b", "PPT Laptop",             2, "St"),  # anderer Text
        item("c", "vierholmtraverse hd34 ", 6, "m"),   # gleicher Text, andere Schreibweise
        item("d", "Vierholmtraverse HD34",  4, "St"),  # gleicher Text, Stück-Einheit
        item("e", "Vierholmtraverse HD34",  3, "m"),   # gleicher Text, aber manuell gesetzt
    ],
)
ss.matches = {"e": MatchResult(matched=TRUSS_1M, score=99.0, method="manual", confident=True)}
ss.bundles = {"a": [{"article": KABEL, "qty": 3.0}]}   # 1 Zusatzartikel je Traversenstück
ss.d83_booking_qtys = {"a": _booking_entry(TRUSS_3M, ss.d83_project.items[0])}

check("Zwillinge gefunden", [i.item_id for i in _twin_items(ss, "a")], ["c", "d"])
check("Quelle 9 m → 3 Stück", ss.d83_booking_qtys["a"]["qty"], 3.0)

check("geänderte Positionen", _apply_match_to_twins(ss, "a", TRUSS_3M), 2)
check("c bekommt Artikel",  ss.matches["c"].matched.nummer, TRUSS_3M.nummer)
check("c 6 m → 2 Stück",    ss.d83_booking_qtys["c"]["qty"], 2.0)
check("c Zusatzartikel 2×", [b["qty"] for b in ss.bundles["c"]], [2.0])
check("d 4 St bleibt 4",    ss.d83_booking_qtys["d"]["qty"], 4.0)
check("d Zusatzartikel 4×", [b["qty"] for b in ss.bundles["d"]], [4.0])
check("b unberührt",        ss.matches.get("b"), None)
check("e bleibt manuell",   ss.matches["e"].matched.nummer, TRUSS_1M.nummer)

print()
if fails:
    print(f"FEHLER in {len(fails)} Prüfungen: {', '.join(fails)}")
    sys.exit(1)
print("Alle Prüfungen bestanden.")
