"""Shared application state, constants and template config."""
import os
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
    return f"DRIVER={{SQL Server}};SERVER={server};DATABASE={db};UID={uid};PWD={pwd}"


# ─── State ────────────────────────────────────────────────────────────────────

class MatchProgress:
    done:    int  = 0
    total:   int  = 0
    running: bool = False
    error:   str  = ""


class State:
    project:    Optional[GaebProject]   = None
    matcher:    Optional[UnifiedMatcher] = None
    matches:    dict[str, MatchResult]  = {}
    bundles:    dict[str, list]         = {}
    alt_active: dict                    = {}
    ej_client:  Optional[EjLiveClient] = None
    ej_cache:   dict[str, list]        = {}
    x83_bytes:  Optional[bytes]        = None
    x84_bytes:  Optional[bytes]        = None
    x83_name:   str                    = ""
    x84_name:   str                    = ""
    progress:   MatchProgress          = MatchProgress()
    # Einstellungen
    ej_url:             str  = os.environ.get("EJ_BASE_URL", "http://EASYJOB-TEST:8008")
    ej_user:            str  = os.environ.get("EJ_USERNAME", "")
    ej_pass:            str  = ""
    use_train_mappings: bool = True
    use_gui_mappings:   bool = True
    # D83 Import
    d83_project:     Optional[GaebProject] = None
    d83_name:        str                   = ""
    d83_groups:      list                  = []
    d83_proj_types:  list                  = []
    d83_events:      list                  = []
    d83_import_mode:  str                  = "positions"
    ej_db_conn:      str                   = ""
    ej_user_id:      int                   = 0


S = State()
