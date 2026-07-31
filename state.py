"""Shared application state, constants and template config."""
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi.templating import Jinja2Templates

load_dotenv()

from gaeb_parser import GaebProject
from matcher import MatchResult, UnifiedMatcher, resolve_time_factor
from easyjob_api import EjLiveClient

# ─── Pfade ────────────────────────────────────────────────────────────────────

BASE_DIR            = Path(__file__).parent
HAENGEPUNKT_NR      = "1000756.00"
GUI_MAPPINGS_PATH   = BASE_DIR / "mappings_gui.json"
TRAIN_MAPPINGS_PATH = BASE_DIR / "mappings.json"

templates = Jinja2Templates(directory=BASE_DIR / "templates")

import re as _re
from markupsafe import Markup as _Markup, escape as _escape

_URL_RE = _re.compile(r'(https?://[^\s<>"]+[^\s<>.,;:!?"\')\]}])')

def _autolink(text: str) -> _Markup:
    """HTML-escaped Text mit klickbaren URLs."""
    escaped = str(_escape(text))
    linked  = _URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
        escaped,
    )
    return _Markup(linked)

templates.env.filters["autolink"] = _autolink
templates.env.globals["resolve_time_factor"] = resolve_time_factor


# ─── Hilfsfunktion DB-Verbindungsstring ──────────────────────────────────────

def _build_db_conn(server: str, db: str, uid: str, pwd: str) -> str:
    driver = os.environ.get("EJ_DB_DRIVER", "ODBC Driver 18 for SQL Server")
    return f"DRIVER={{{driver}}};SERVER={server};DATABASE={db};UID={uid};PWD={pwd};TrustServerCertificate=yes"


def is_admin_login(ej_user: str) -> bool:
    """Prüft, ob der Login-Name in der Admin-Liste (Env ADMIN_LOGINS) steht.
    Kommasepariert, Groß-/Kleinschreibung egal. Leere Liste = niemand ist Admin."""
    admins = {
        name.strip().lower()
        for name in os.environ.get("ADMIN_LOGINS", "").split(",")
        if name.strip()
    }
    return bool(ej_user) and ej_user.strip().lower() in admins


# ─── Per-Session State ────────────────────────────────────────────────────────

class MatchProgress:
    def __init__(self):
        self.done:    int  = 0
        self.total:   int  = 0
        self.running: bool = False
        self.error:   str  = ""


class CreateProgress:
    def __init__(self):
        self.running:    bool        = False
        self.step:       str         = ""
        self.done:       int         = 0
        self.total:      int         = 0
        self.started_at: float       = 0.0
        self.log:        list | None = None   # None = läuft noch; list = fertig


class UserSession:
    """Vollständiger State pro Browser-Session — kein globales Singleton."""
    def __init__(self):
        # Matching
        self.project:    Optional[GaebProject]   = None
        self.matcher:    Optional[UnifiedMatcher] = None
        self.matches:    dict[str, MatchResult]  = {}
        self.bundles:    dict[str, list]         = {}
        self.alt_active: dict                    = {}
        self.x83_bytes:  Optional[bytes]         = None
        self.x83_name:   str                     = ""
        self.progress:   MatchProgress           = MatchProgress()
        # Einstellungen
        self.ej_url:             str  = os.environ.get("EJ_BASE_URL", "http://EASYJOB-TEST:8008")
        self.ej_user:            str  = ""
        self.ej_pass:            str  = ""
        self.use_train_mappings: bool = True
        self.use_gui_mappings:   bool = True
        # EJ Verbindung
        self.ej_client:  Optional[EjLiveClient] = None
        self.ej_cache:   dict[str, list]        = {}
        self.ej_db_conn: str                    = ""
        self.ej_user_id: int                    = 0
        self.is_admin:   bool                   = False
        # D83 Import
        self.d83_project:    Optional[GaebProject] = None
        self.d83_name:       str                   = ""
        self.d83_groups:     list                  = []
        self.d83_proj_types: list                  = []
        self.d83_events:     list                  = []
        self.d83_import_mode: str                  = "positions"
        # D83 EJ Projekt-State (gesetzt nach Projekt-Anlage)
        # Lokale Job-Definitionen (vor EJ-Anlage)
        self.d83_local_jobs:       list = []   # extra jobs: [{"lid": 2, "name": "Licht"}, ...]
        self.d83_next_lid:         int  = 2
        self.d83_group_jobs:       dict = {}   # group_name → lid (fehlend/1 = Standard-Job)
        self.d83_standard_job_name: str = ""   # leer = "Standard"; nach Umbenennung z.B. "Technik"
        self.d83_alt_active:       dict = {}   # alt_key → "primary"|"alt"|"both"
        self.d83_booking_qtys:     dict = {}   # item_id → {"qty": float, "lfm_converted": bool}
        self.einsatztage:          float = 2.0  # Berechnungstage für Preis-Progression (Job.CommitmentDays), Dezimal erlaubt
        self.import_filename: str = ""  # zuletzt hochgeladene Datei auf /import
        self.draft_id:   Optional[int] = None  # geladener Entwurf (projects.status='draft'); None = frischer Import
        self.d83_draft_setup: dict = {}         # setup-Felder aus dem Entwurf (Seitenleiste vorbefüllen)
        self.create_progress: CreateProgress = CreateProgress()


# ─── Session-Registry ─────────────────────────────────────────────────────────

_SESSION_TTL = 15 * 3600  # Sekunden — passt zum Cookie max_age (Weiterarbeiten am Folgetag)
_SESSION_MAX = 200        # harte Obergrenze gegen unbegrenztes Session-Wachstum

_sessions:      dict[str, UserSession] = {}
_last_seen:     dict[str, float]       = {}


def _cleanup_sessions() -> None:
    """Entfernt Sessions, die seit TTL nicht mehr aktiv waren, und begrenzt die
    Gesamtzahl (älteste zuerst) — verhindert unbegrenztes Wachstum."""
    cutoff = time.monotonic() - _SESSION_TTL
    for sid in [s for s, t in list(_last_seen.items()) if t < cutoff]:
        _sessions.pop(sid, None)
        _last_seen.pop(sid, None)
    while len(_sessions) > _SESSION_MAX and _last_seen:
        oldest = min(_last_seen, key=_last_seen.get)
        _sessions.pop(oldest, None)
        _last_seen.pop(oldest, None)


def drop_session(session: dict) -> None:
    """Löscht die serverseitige Session (Logout) — entfernt Credentials und
    Matcher-/Projekt-State sofort aus dem RAM."""
    sid = session.get("app_sid")
    if sid:
        _sessions.pop(sid, None)
        _last_seen.pop(sid, None)


def get_session(session: dict) -> UserSession:
    """Gibt den UserSession der aktuellen Browser-Session zurück (lazy create)."""
    _cleanup_sessions()   # deterministisch bei jedem Zugriff (Registry ist klein)

    sid = session.get("app_sid")
    if not sid or sid not in _sessions:
        sid = str(uuid.uuid4())
        session["app_sid"] = sid
        _sessions[sid] = UserSession()

    _last_seen[sid] = time.monotonic()
    return _sessions[sid]
