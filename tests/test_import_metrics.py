"""Regressionstest für die Kennzahlen über der Positionsliste.

Anlass: in „Pers.-Kosten" steckten auch Fahrzeuge. Im Easyjob-Stamm liegen Personal,
Arbeitsmittel und Fahrzeuge in derselben Tabelle — unterschieden werden sie nur durch
`ressourcenart`, und die Kennzahl zählte alles zusammen. Wer die Zahl gegen eine
Personalkalkulation hält, sucht die Differenz vergeblich.

    .venv/Scripts/python.exe tests/test_import_metrics.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="metrics_"), "test.db")
os.environ["DB_PATH"] = _TMP_DB

import db as _db                                          # noqa: E402

_db.init_db()

from gaeb_parser import GaebItem, GaebProject             # noqa: E402
from matcher import MatchResult, Resource                 # noqa: E402
import routes.import_ as imp                              # noqa: E402

_fails: list[str] = []


def check(name, ist, soll):
    ok = ist == soll
    print(("  ✓ " if ok else "  ✗ ") + name + ": " + repr(ist)
          + ("" if ok else "  ERWARTET " + repr(soll)))
    if not ok:
        _fails.append(name)


def _res(rid, name, art, satz):
    return Resource(id=rid, funktion=name, ressourcenart=art, tagessatz=satz,
                    eigenkosten=satz * 0.6, satzname="", gaeb_synonyms=[])


def _item(iid, oz, besch):
    return GaebItem(item_id=iid, rno_part=0, oz=oz, description=besch,
                    long_text="", qty=1.0, unit="St", category_path=[])


class _S:
    """Nur die Felder, die `_calc_import_metrics` liest."""
    def __init__(self):
        self.d83_project = GaebProject(
            name="Test", label="", phase="", date="", currency="EUR",
            items=[_item("p1", "01.01", "Tontechniker"),
                   _item("p2", "01.02", "LKW 7,5 t"),
                   _item("p3", "01.03", "Spesensatz")])
        self.d83_groups = []          # leer = alle Positionen aktiv
        self.matches = {}
        self.bundles = {}
        self.d83_booking_qtys = {}
        self.einsatztage = 5.0
        self.crew = None


# ─── 1. Fahrzeuge zählen nicht zum Personal ──────────────────────────────────
print("── 1. Trennung ──")

ss = _S()
ss.matches = {
    "p1": MatchResult(matched=_res(11, "Tontechniker", "Personal", 600.0),
                      score=99.0, method="manual", confident=True),
    "p2": MatchResult(matched=_res(22, "LKW 7,5 t", "Fahrzeug", 250.0),
                      score=99.0, method="manual", confident=True),
    "p3": MatchResult(matched=_res(33, "Spesensatz Inland", "Arbeitsmittel", 32.0),
                      score=99.0, method="manual", confident=True),
}
ss.d83_booking_qtys = {k: {"qty": 2.0} for k in ("p1", "p2", "p3")}

ss.d83_project.items.append(_item("p4", "01.04", "Leergut-Handling"))
ss.matches["p4"] = MatchResult(
    matched=_res(44, "Leergut- und Vollguthandling", "Arbeitsmittel", 6500.0),
    score=99.0, method="manual", confident=True)
ss.d83_booking_qtys["p4"] = {"qty": 1.0}

m = imp._calc_import_metrics(ss)
check("Personal ohne den LKW", m["cost_pers"], 2 * 600.0 + 2 * 32.0)
check("LKW als Transport", m["cost_trans"], 2 * 250.0)
check("Handling als Sonstiges", m["cost_sonst"], 6500.0)
# Spesen bleiben beim Personal: sie hängen an den Leuten.
check("Spesen bleiben Personal", m["cost_pers"] > 2 * 600.0, True)
# Gezählt werden weiterhin alle drei — die Zahl heißt jetzt „Ressourcen".
check("alle gezählt", m["res_count"], 4)

# „Arbeitsmittel" ist ein gemischter Topf: die Spesensätze stehen dort neben
# Leergut-Handling und Storno-Pauschale. Nur was an den Leuten hängt, zählt zum
# Personal.
_f = imp._kostentopf
check("Fahrzeug", _f(_res(1, "LKW 7,5 t", "Fahrzeug", 250.0)), "transport")
check("Person", _f(_res(2, "Tontechniker", "Personal", 600.0)), "personal")
check("Spesensatz", _f(_res(3, "Spesensatz Schweiz", "Arbeitsmittel", 62.0)),
      "personal")
check("Reisekosten", _f(_res(4, "Reisekosten Pauschal", "Arbeitsmittel", 250.0)),
      "personal")
check("Handling ist keins",
      _f(_res(5, "Leergut- und Vollguthandling", "Arbeitsmittel", 6500.0)),
      "sonstiges")
check("Storno ist keins",
      _f(_res(6, "Ausfallpauschale bei Storno", "Arbeitsmittel", 0.0)), "sonstiges")
check("Rüstkosten sind keins",
      _f(_res(7, "Rüstkosten pauschal", "Arbeitsmittel", 500.0)), "sonstiges")
# Umbenannt im Stamm: die Ressourcen, auf die die Personalplanung selbst bucht,
# bleiben trotzdem beim Personal.
from crew_plan import DEFAULT_RK_ID                       # noqa: E402
check("Buchungsziel der Planung bleibt Personal",
      _f(_res(DEFAULT_RK_ID, "Umbenannt", "Arbeitsmittel", 250.0)), "personal")
# Unbekannte Arten lieber beim Personal als still im Nichts.
check("unbekannte Art", _f(_res(8, "Irgendwas", "", 10.0)), "personal")


# ─── 2. Bundles zählen genauso ───────────────────────────────────────────────
print(chr(10) + "── 2. Bundles ──")

ss2 = _S()
ss2.matches = {"p1": MatchResult(matched=_res(11, "Tontechniker", "Personal", 600.0),
                                 score=99.0, method="manual", confident=True)}
ss2.d83_booking_qtys = {"p1": {"qty": 1.0}}
ss2.bundles = {"p1": [{"resource": _res(22, "LKW", "Fahrzeug", 250.0), "qty": 3.0}]}
m2 = imp._calc_import_metrics(ss2)
check("Bundle-Fahrzeug geht auf Transport", m2["cost_trans"], 3 * 250.0)
check("Bundle-Personal bleibt Personal", m2["cost_pers"], 600.0)


# ─── 3. Personalplanung schlägt das Matching ─────────────────────────────────
# Was die Matrix bucht, kommt aus der Matrix — sonst zeigt der Voranschlag eine
# Summe, die der Import gar nicht bucht.
print(chr(10) + "── 3. Personalplanung ──")

from crew_plan import CrewPlan                            # noqa: E402

ss3 = _S()
ss3.matches = {"p1": MatchResult(matched=_res(11, "Tontechniker", "Personal", 600.0),
                                 score=99.0, method="manual", confident=True)}
ss3.d83_booking_qtys = {"p1": {"qty": 9.0}}
plan = CrewPlan(date_from="2026-03-09", date_to="2026-03-12")
plan.positions = ["p1"]
r = plan.add_row("Ton-Operator", 11)
r.tagessatz = 600.0
plan.set_cell(r.id, "2026-03-09", 2)
plan.assign_days(r.id, "2026-03-09", "2026-03-09", "p1")
ss3.crew = plan
m3 = imp._calc_import_metrics(ss3)
check("Kosten aus der Matrix, nicht aus der Menge",
      round(m3["cost_pers"], 2), round(plan.position_stats()["p1"]["cost"], 2))
check("nicht 9 Tage aus dem Match", m3["cost_pers"] == 9 * 600.0, False)


print(chr(10) + "=" * 62)
if _fails:
    print(f"FEHLGESCHLAGEN: {len(_fails)}")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("Alle Prüfungen bestanden.")
