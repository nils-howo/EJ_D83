"""Hinweise im Mapping-Dialog: Formeln in der Preisspalte, Zeilen-Umschalter.

Die Warnung soll erscheinen, bevor exportiert wird — eine berechnete Preisspalte
(LOS1 GOE: E = C + D) darf nicht überschrieben werden. Summenzeilen-Formeln
(LOS2) dürfen dagegen keinen Fehlalarm auslösen.

    .venv/Scripts/python.exe tests/test_dialog_hints.py
"""
import sys

# Konsole auf UTF-8: sonst stirbt schon ein "→" im print an cp1252 und der Test
# bricht mitten drin ab, ohne dass eine Prüfung fehlgeschlagen wäre.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import html as _h
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient
from routes.import_ import router
import excel_parser as _xl, db

app = FastAPI(); app.include_router(router)
app.add_middleware(SessionMiddleware, secret_key="t"*40, session_cookie="t")
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
lay_of = lambda t: json.loads(_h.unescape(re.search(r"data-layout='(.*?)'", t, re.S).group(1)))

def clean(fn):
    d = open("infos/" + fn, "rb").read()
    with db.get_conn() as cn:
        cn.execute("DELETE FROM excel_layouts WHERE fingerprint=?",
                   (_xl.probe_workbook(d).fingerprint,))
    return d

# ── GOE: Preisspalte E ist eine Formel -> Warnung
FN = "LOS1_GOE Ausschreibung_Technik 2026_Preisblatt_V2.1.xlsx"
data = clean(FN)
with TestClient(app) as c:
    t0 = time.perf_counter()
    r = c.post("/api/import/upload", files={"file": (FN, data, MIME)})
    t_up = time.perf_counter() - t0
    warn = re.search(r"In Spalte (\w+)\s*\(Einzelpreis\) stehen in <b>(\d+)</b> von\s*(\d+)", r.text, re.S)
    print(f"GOE  Upload {t_up*1000:.0f} ms")
    assert warn, "Formelwarnung fehlt:\n" + r.text[:600]
    print(f"  Warnung: Spalte {warn.group(1)}, {warn.group(2)} von {warn.group(3)} Positionszeilen")
    assert warn.group(1) == "E" and int(warn.group(2)) > 400

    # Preisspalte auf C (1.PA Material, Eingabespalte) umfärben -> Warnung muss weg
    L = lay_of(r.text)
    sh = next(s for s in L["sheets"] if s["name"] == "Preisblatt Technik")
    sh["roles"]["price"] = 3
    r2 = c.post("/api/import/excel/repreview", data={"layout_json": json.dumps(L)})
    still = "Einzelpreis) stehen in" in r2.text
    print(f"  nach Umfärben auf Spalte C: Warnung noch da? {still}")
    assert not still, "Warnung müsste verschwinden"

    # Zeilen-Umschalter
    m = re.search(r'Vorschau:\s*(\d+)\s*von\s*(\d+)\s*Zeilen', r2.text, re.S)
    print(f"  Vorschau-Hinweis: {' '.join(m.group(0).split()) if m else 'keiner'}")
    assert m and 'class="xl-showall"' in r2.text, "Umschalter fehlt"
    t0 = time.perf_counter()
    r3 = c.post("/api/import/excel/repreview",
                data={"layout_json": json.dumps(L), "show_all": "Preisblatt Technik"})
    dt = time.perf_counter() - t0
    rows_before = r2.text.count('class="xl-rowhead"')
    rows_after  = r3.text.count('class="xl-rowhead"')
    print(f"  alle Zeilen: {rows_before} -> {rows_after} Zeilen in {dt*1000:.0f} ms, "
          f"{len(r3.text)//1024} KB")
    assert rows_after > rows_before * 3, (rows_before, rows_after)
    assert 'class="xl-showless"' in r3.text, "Zurück-Umschalter fehlt"
    assert 'data-showall="Preisblatt Technik"' in r3.text, "Zustand nicht im Datenattribut"
    with db.get_conn() as cn:
        cn.execute("DELETE FROM excel_layouts WHERE fingerprint=?", (L["fingerprint"],))

# ── LOS2: Summen-Formeln in der Preisspalte dürfen NICHT warnen
FN2 = "LOS2_LK_Technik_JPK_CMD.xlsx"
d2 = clean(FN2)
with TestClient(app) as c:
    r = c.post("/api/import/upload", files={"file": (FN2, d2, MIME)})
    print(f"\nLOS2 (Summen-Formeln in Spalte G): Warnung? "
          f"{'Einzelpreis) stehen in' in r.text}")
    assert "Einzelpreis) stehen in" not in r.text, "Fehlalarm bei Summenzeilen"
    # Und die Blätter, bei denen nichts abgeschnitten wird, dürfen keinen Hinweis zeigen
    n_hint = r.text.count("alle Zeilen anzeigen")
    print(f"  Blätter mit echtem Abschneiden: {n_hint} (von 10 aktiven)")
    assert n_hint == 1, n_hint          # nur 'Sneak Previews'
    with db.get_conn() as cn:
        cn.execute("DELETE FROM excel_layouts WHERE fingerprint=?",
                   (lay_of(r.text)["fingerprint"],))

print("\nFormelwarnung und Zeilen-Umschalter ok.")
