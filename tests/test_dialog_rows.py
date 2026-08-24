"""Zeilenangabe und „alle Zeilen"-Umschalter im Mapping-Dialog.

Zugeklappte Blätter bekommen serverseitig keine Zellen mehr (das sparte bei LOS2
92 % der Nutzlast) — ihre Zeilenzahl und der Umschalter müssen trotzdem dastehen.
Genau das war einmal weg.

    .venv/Scripts/python.exe tests/test_dialog_rows.py
"""
import html as _h
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient
from routes.import_ import router
import excel_parser as _xl, db

app = FastAPI(); app.include_router(router)
app.add_middleware(SessionMiddleware, secret_key="t"*40, session_cookie="t")
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
lay_of = lambda t: json.loads(_h.unescape(re.search(r"data-layout='(.*?)'", t, re.S).group(1)))
# Blattnamen mit & stehen im HTML als &amp; — sonst findet das Suchmuster sie nicht.
esc = lambda n: re.escape(_h.escape(n, quote=False))

FN = "LOS2_LK_Technik_JPK_CMD.xlsx"
d = open("infos/" + FN, "rb").read()
with db.get_conn() as cn:
    cn.execute("DELETE FROM excel_layouts WHERE fingerprint=?",
               (_xl.probe_workbook(d).fingerprint,))

with TestClient(app) as c:
    r = c.post("/api/import/upload", files={"file": (FN, d, MIME)})
    L = lay_of(r.text)
    aktive = [s["name"] for s in L["sheets"] if s["enabled"]]

    # 1) Jedes aktive Blatt nennt seine Zeilenzahl
    ohne = [n for n in aktive if not re.search(esc(n) + r'.{0,900}?\d+ Zeilen', r.text, re.S)]
    print(f"1) Blätter ohne Zeilenangabe: {ohne or 'keine'}  ({len(aktive)} aktiv)")
    assert not ohne, ohne

    # 2) Ein Pinselstrich (nur ein Blatt offen) -> Zeilenzahlen bleiben
    offen = re.findall(r'data-sheet="([^"]+)"[^>]*\bopen', r.text)
    r2 = c.post("/api/import/excel/repreview",
                data={"layout_json": json.dumps(L), "opened": "|".join(offen)})
    ohne2 = [n for n in aktive if not re.search(esc(n) + r'.{0,900}?\d+ Zeilen', r2.text, re.S)]
    print(f"2) nach Pinselstrich, offen={offen}: ohne Angabe {ohne2 or 'keine'}")
    assert not ohne2, ohne2

    # 3) Umschalter genau bei den Blättern mit mehr Zeilen als die Vorschau
    lang = [sp.layout.name for sp in _xl.probe_workbook(d, None, None, set(aktive)).sheets
            if sp.layout.enabled and sp.counts["rows"] > _xl._PREVIEW_ROWS]
    knoepfe = re.findall(r'class="xl-showall" data-sheet="([^"]+)"', r2.text)
    print(f"3) Umschalter bei {sorted(knoepfe)} · erwartet {sorted(lang)}")
    assert sorted(knoepfe) == sorted(lang), (knoepfe, lang)

    # 4) "alle Zeilen" für ein zugeklapptes Blatt -> es wird geöffnet UND voll geladen
    ziel = lang[0]
    r3 = c.post("/api/import/excel/repreview",
                data={"layout_json": json.dumps(L), "show_all": ziel,
                      "opened": "|".join(set(offen) | {ziel})})
    # data-sheet steht auch am Reiter — den richtigen <details>-Block suchen
    blockteil = ""
    for teil in r3.text.split('<details class="xl-sheet')[1:]:
        if f'data-sheet="{_h.escape(ziel, quote=False)}"' in teil.split(">", 1)[0]:
            blockteil = teil.split("</details>", 1)[0]
            break
    assert blockteil, f"Block für {ziel!r} nicht gefunden"
    n_td = blockteil.count("<td ")
    print(f"4) '{ziel}' mit alle Zeilen: {n_td} Zellen · Zurück-Umschalter "
          f"{'xl-showless' in blockteil}")
    assert n_td > 60 and "xl-showless" in blockteil, (n_td, "xl-showless" in blockteil)

    with db.get_conn() as cn:
        cn.execute("DELETE FROM excel_layouts WHERE fingerprint=?", (L["fingerprint"],))
print("\nZeilenangabe und Umschalter sind auf jedem Blatt da.")
