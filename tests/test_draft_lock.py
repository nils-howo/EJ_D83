"""Regressionstest: ein übernommener Entwurf darf nicht aus dem Cache zurückkommen.

Wer die Importseite verlässt, gibt die Sperre frei — der Entwurf steht danach anderen
offen. Die Arbeitskopie blieb aber in der Sitzung liegen: kommt man später zurück,
sähe man einen Stand, den inzwischen jemand anderes überholt hat, und würde beim
nächsten Speichern darüberschreiben.

    .venv/Scripts/python.exe tests/test_draft_lock.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="drafts_"), "test.db")
os.environ["DB_PATH"] = _TMP_DB

import db as _db                                          # noqa: E402

_fails: list[str] = []


def check(name, ist, soll):
    ok = ist == soll
    print(("  ✓ " if ok else "  ✗ ") + name + ": " + repr(ist)
          + ("" if ok else "  ERWARTET " + repr(soll)))
    if not ok:
        _fails.append(name)


_db.init_db()
with _db.get_conn() as cn:
    cn.execute("INSERT INTO projects (id, name, status) VALUES (3, 'Entwurf', 'draft')")


# ─── 1. Die Sperre selbst ────────────────────────────────────────────────────
print("── 1. Sperre ──")
check("erster bekommt sie", _db.acquire_draft_lock(3, "anna")[0], True)
# Derselbe Benutzer kommt immer wieder rein — sonst sperrte ein gewöhnliches
# Neuladen der Seite den Bearbeiter aus.
check("derselbe kommt wieder rein", _db.acquire_draft_lock(3, "anna")[0], True)
check("ein anderer nicht", _db.acquire_draft_lock(3, "bernd")[0], False)
_db.release_draft_lock(3, "anna")
check("nach dem Freigeben frei", _db.acquire_draft_lock(3, "bernd")[0], True)
check("und jetzt ist anna draußen", _db.acquire_draft_lock(3, "anna")[0], False)


# ─── 2. Arbeitskopie verwerfen ───────────────────────────────────────────────
print(chr(10) + "── 2. Arbeitskopie ──")
import routes.import_ as imp                              # noqa: E402
from crew_plan import CrewPlan                            # noqa: E402


class _S:
    """Sitzung mit dem, was eine Arbeitskopie ausmacht."""
    def __init__(self):
        self.draft_id = 3
        self.ej_user = "anna"
        self.d83_project = object()
        self.d83_name = "LV.x83"
        self.d83_groups = [{"name": "Ton"}]
        self.d83_draft_setup = {"id_address": 5}
        self.matches = {"p1": object()}
        self.bundles = {"p1": [1]}
        self.x83_bytes = b"xxx"
        self.excel_bytes = None
        self.import_filename = "LV.x83"
        self.crew = CrewPlan(date_from="2026-03-09", date_to="2026-03-12")
        self.crew_schedule = {"Aufbau": ("2026-03-09", "2026-03-10")}
        self.d83_local_jobs = [{"lid": 2}]
        self.d83_group_jobs = {"Ton": 2}
        self.d83_next_lid = 3
        self.d83_standard_job_name = "Technik"
        self.d83_alt_active = {"a": "alt"}
        self.d83_booking_qtys = {"p1": {"qty": 2}}


ss = _S()
imp._verwerfe_arbeitskopie(ss)
check("Entwurf losgelassen", ss.draft_id, None)
check("LV weg", ss.d83_project, None)
check("Matching weg", (ss.matches, ss.bundles), ({}, {}))
check("Quelldatei weg", ss.x83_bytes, None)
check("Personalplanung weg", ss.crew, None)
check("Gruppen und Jobs weg",
      (ss.d83_groups, ss.d83_group_jobs, ss.d83_local_jobs), ([], {}, []))
check("Entwurfs-Vorbelegung weg", ss.d83_draft_setup, {})
# Was der Entwurf sonst noch mitbrachte, muss ebenfalls fort: sonst steht beim
# nächsten Import die Alt-Auswahl des fremden Projekts noch da.
check("Auswahl weg", (ss.d83_alt_active, ss.d83_booking_qtys), ({}, {}))


# ─── 3. Die Seite verwirft nur bei fremder Sperre ────────────────────────────
# Ein gewöhnliches Neuladen darf nichts löschen — der Beacon beim Verlassen gibt die
# Sperre frei, und beim Zurückkommen holt die Seite sie sich wieder.
print(chr(10) + "── 3. Wann verworfen wird ──")

_db.release_draft_lock(3, "bernd")
check("frei: anna bekommt sie zurück", _db.acquire_draft_lock(3, "anna")[0], True)
check("und darf ihre Kopie behalten", _db.acquire_draft_lock(3, "anna")[0], True)
# Erst wenn wirklich jemand anderes daran sitzt, wird verworfen.
_db.release_draft_lock(3, "anna")
_db.acquire_draft_lock(3, "bernd")
check("fremd: anna kommt nicht mehr rein", _db.acquire_draft_lock(3, "anna")[0], False)

_quelle = open(os.path.join(os.path.dirname(__file__), "..", "routes", "import_.py"),
               encoding="utf-8").read()
check("die Seite prüft die Sperre",
      "_ok, _fremd = _db.acquire_draft_lock(ss.draft_id" in _quelle, True)
check("und verwirft dann die Kopie",
      "_verwerfe_arbeitskopie(ss)" in _quelle, True)


print(chr(10) + "=" * 62)
if _fails:
    print(f"FEHLGESCHLAGEN: {len(_fails)}")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("Alle Prüfungen bestanden.")
