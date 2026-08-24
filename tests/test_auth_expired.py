"""Abgelaufene Session: Navigation -> 303 auf /login, fetch/htmx -> 401 + HX-Redirect."""
import sys

# Konsole auf UTF-8: sonst stirbt schon ein "→" im print an cp1252 und der Test
# bricht mitten drin ab, ohne dass eine Prüfung fehlgeschlagen wäre.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
from starlette.testclient import TestClient
import server

c = TestClient(server.app, follow_redirects=False)

cases = [
    ("Navigation (Browser)",        {"sec-fetch-mode": "navigate"},              303),
    ("htmx-Request",                {"hx-request": "true"},                     401),
    ("fetch (same-origin)",         {"sec-fetch-mode": "same-origin"},           401),
    ("fetch (cors)",                {"sec-fetch-mode": "cors"},                  401),
    ("XHR alt",                     {"x-requested-with": "XMLHttpRequest"},      401),
    ("ohne Header (curl)",          {},                                          303),
]
for name, hdr, want in cases:
    r = c.get("/import", headers=hdr)
    loc = r.headers.get("location") or r.headers.get("hx-redirect") or ""
    html = "<form" in r.text.lower()
    print(f"  {name:24s} -> {r.status_code}  Ziel={loc or '-':8s}  Login-HTML im Body: {html}")
    assert r.status_code == want, (name, r.status_code, want)
    assert loc == "/login", (name, loc)
    if want == 401:
        assert not html, f"{name}: Login-Formular darf nicht im Body stehen"

# POST auf eine API-Route (das war der Fall im Screenshot)
r = c.post("/api/import/excel/repreview", data={"layout_json": "{}"},
           headers={"sec-fetch-mode": "same-origin"})
print(f"  {'POST /excel/repreview':24s} -> {r.status_code}  "
      f"Ziel={r.headers.get('hx-redirect')}  Body leer: {not r.text.strip()}")
assert r.status_code == 401 and r.headers.get("hx-redirect") == "/login"
assert not r.text.strip(), "kein HTML im Body, sonst wird es eingerendert"

# /login selbst und /static bleiben erreichbar
assert c.get("/login").status_code == 200
assert c.get("/static/style.css").status_code == 200
print("\n  /login und /static bleiben offen")
print("\nAbgelaufene Session leitet sauber weiter, kein Login-HTML in Ziel-Elementen.")
