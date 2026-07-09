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
from matcher import MatchResult, UnifiedMatcher
from easyjob_api import EjLiveClient

# ─── Pfade ────────────────────────────────────────────────────────────────────

BASE_DIR            = Path(__file__).parent
HAENGEPUNKT_NR      = "1000756.00"
GUI_MAPPINGS_PATH   = BASE_DIR / "mappings_gui.json"
TRAIN_MAPPINGS_PATH = BASE_DIR / "mappings.json"

templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ─── Hilfsfunktion DB-Verbindungsstring ──────────────────────────────────────

def _build_db_conn(server: str, db: str, uid: str, pwd: str) -> str:
    driver = os.environ.get("EJ_DB_DRIVER", "ODBC Driver 18 for SQL Server")
    return f"DRIVER={{{driver}}};SERVER={server};DATABASE={db};UID={uid};PWD={pwd};TrustServerCertificate=yes"


# ─── Per-Session State ────────────────────────────────────────────────────────

class MatchProgress:
    def __init__(self):
        self.done:    int  = 0
        self.total:   int  = 0
        self.running: bool = False
        self.error:   str  = ""


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
        self.x84_bytes:  Optional[bytes]         = None
        self.x83_name:   str                     = ""
        self.x84_name:   str                     = ""
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
        # D83 Import
        self.d83_project:    Optional[GaebProject] = None
        self.d83_name:       str                   = ""
        self.d83_groups:     list                  = []
        self.d83_proj_types: list                  = []
        self.d83_events:     list                  = []
        self.d83_import_mode: str                  = "positions"


# ─── Session-Registry ─────────────────────────────────────────────────────────

_SESSION_TTL = 8 * 3600  # Sekunden — passt zum Cookie max_age

_sessions:      dict[str, UserSession] = {}
_last_seen:     dict[str, float]       = {}


def _cleanup_sessions() -> None:
    """Entfernt Sessions die seit TTL nicht mehr aktiv waren."""
    cutoff = time.monotonic() - _SESSION_TTL
    stale = [sid for sid, t in _last_seen.items() if t < cutoff]
    for sid in stale:
        _sessions.pop(sid, None)
        _last_seen.pop(sid, None)


def get_session(session: dict) -> UserSession:
    """Gibt den UserSession der aktuellen Browser-Session zurück (lazy create)."""
    # Gelegentlich aufräumen (jede ~100. Anfrage)
    if len(_sessions) > 10 and int(time.monotonic()) % 100 == 0:
        _cleanup_sessions()

    sid = session.get("app_sid")
    if not sid or sid not in _sessions:
        sid = str(uuid.uuid4())
        session["app_sid"] = sid
        _sessions[sid] = UserSession()

    _last_seen[sid] = time.monotonic()
    return _sessions[sid]
