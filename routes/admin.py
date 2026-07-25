"""Admin-Seite: Sync-Status, DB-Statistiken, manuelle Sync-Trigger."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from state import get_session, templates
import db

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    ss = get_session(request.session)
    if not ss.is_admin:
        return RedirectResponse("/import", status_code=303)
    stats        = db.get_db_stats()
    history      = db.get_sync_history(limit=30)
    changes      = db.get_recent_changes(hours=72)
    gui_mappings = db.get_gui_mappings(limit=500)
    train_mappings = db.get_train_mappings(limit=500)
    return templates.TemplateResponse(request, "admin.html", {
        "stats":          stats,
        "history":        history,
        "changes":        changes,
        "gui_mappings":   gui_mappings,
        "train_mappings": train_mappings,
        "is_admin":       ss.is_admin,
    })
