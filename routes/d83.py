"""Reste der früheren D83-Seite: nur noch die von /import genutzten Such-Endpoints.
Die eigentliche D83-Seite wurde entfernt — /d83 leitet auf /import um.

Kundensuche läuft rein über die EJ-API (kein direkter DB-Zugriff):
  /api/d83/address-search  → Firmen inkl. Kontaktpersonen suchen (gruppiert).
  Mit ?contacts=0 werden nur Firmen geliefert (z. B. für Lieferadresse).
"""

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from state import get_session

router = APIRouter()


@router.get("/d83")
async def d83_page(request: Request):
    # D83-Seite entfernt — Funktion ist in /import integriert
    return RedirectResponse("/import", status_code=303)


@router.get("/api/d83/address-search")
async def d83_address_search(request: Request, q: str = "", limit: int = 12,
                             contacts: int = 1):
    """Firmensuche (reiner API-Weg).

    Liefert gruppiert nach Firma: [{id, name, contacts:[{idc, name}]}].
    Mit ``contacts=0`` werden nur Firmen ohne Kontaktpersonen zurückgegeben
    (z. B. für die Lieferadresse).
    """
    ss = get_session(request.session)
    if len(q) < 2 or not ss.ej_client:
        return JSONResponse([])
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            lambda: ss.ej_client.addresses_search(
                q, limit, with_contacts=bool(contacts)
            ),
        )
        return JSONResponse(results or [])
    except Exception as _e:
        logging.error("d83/address-search failed: %s", _e)
        return JSONResponse([])



@router.get("/api/d83/event-search")
async def d83_event_search(request: Request, q: str = "", limit: int = 15):
    ss = get_session(request.session)
    if not ss.ej_client:
        return JSONResponse([])
    try:
        loop    = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, lambda: ss.ej_client.event_calendars_search(q)
        )
        return JSONResponse((results or [])[:limit])
    except Exception as _e:
        logging.error("d83/event-search failed: %s", _e)
        return JSONResponse([])
