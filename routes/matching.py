"""Reste des früheren Matching-Workflows: nur noch die von /import genutzten
Endpoints (EJ-Artikelsuche im Dialog, Mapping-Quellen-Toggles).
Die Matching-Seite wurde entfernt — / leitet auf /import um."""
import asyncio
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from state import get_session, templates

router = APIRouter()


@router.get("/")
async def index(request: Request):
    # Matching-Seite entfernt — Einstieg ist jetzt /import
    return RedirectResponse("/import", status_code=303)


@router.post("/api/settings/mappings", response_class=HTMLResponse)
async def save_mapping_toggles(
    request:   Request,
    use_train: str = Form(""),
    use_gui:   str = Form(""),
):
    ss = get_session(request.session)
    if not ss.is_admin:
        return HTMLResponse('<span class="error-msg">Nur für Admins</span>', status_code=403)
    ss.use_train_mappings = (use_train == "1")
    ss.use_gui_mappings   = (use_gui   == "1")
    labels = []
    if ss.use_train_mappings: labels.append("Training")
    if ss.use_gui_mappings:   labels.append("GUI")
    text = "Aktiv: " + ", ".join(labels) if labels else "Alle deaktiviert"
    return f'<span class="save-ok">✓ {text}</span>'


@router.get("/api/ej/search/{item_id}", response_class=HTMLResponse)
async def ej_search(item_id: str, request: Request, q: str = ""):
    """EJ-Artikelsuche für den Zuordnungs-Dialog auf der Import-Seite."""
    ss = get_session(request.session)
    if not ss.ej_client or not q.strip():
        return templates.TemplateResponse(request, "partials/ej_results.html", {
            "results": [], "item_id": item_id
        })
    ck = q.strip().lower()
    if ck not in ss.ej_cache:
        loop = asyncio.get_event_loop()
        ss.ej_cache[ck] = await loop.run_in_executor(
            None, lambda: ss.ej_client.search(q, limit=100)
        )
    raw = ss.ej_cache.get(ck, [])
    results = []
    for r in raw:
        num = str(r.get("Number", ""))
        inv = ""
        if ss.matcher:
            lidx = ss.matcher._num_to_idx.get(num)
            if lidx is not None:
                inv = str(ss.matcher._pool[lidx].mietinventar)
        results.append({
            "nummer":      num,
            "bezeichnung": r.get("Caption", ""),
            "kategorie":   r.get("Category", ""),
            "inv":         inv,
            "_raw_json":   json.dumps(r),
        })
    return templates.TemplateResponse(request, "partials/ej_results.html", {
        "results": results, "item_id": item_id, "limit": 100
    })
