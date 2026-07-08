"""SQLite-Datenbankschicht: Artikel, Personal, Mappings (Train + GUI)."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

# DB-Pfad: per Env-Variable konfigurierbar, Standard neben den Skripten
_DEFAULT_DB = Path(__file__).parent / "data" / "gaeb.db"
DB_PATH = Path(os.environ.get("DB_PATH", str(_DEFAULT_DB)))


def _ensure_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    _ensure_dir()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # gleichzeitige Lese-/Schreibzugriffe
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    nummer            TEXT PRIMARY KEY,
    bezeichnung       TEXT NOT NULL,
    mutterwarengruppe TEXT,
    warengruppe       TEXT,
    kommentar         TEXT,
    artikelart        TEXT,
    hersteller        TEXT,
    detail            TEXT,
    mietpreis         REAL    DEFAULT 0,
    einheit           TEXT,
    mietinventar      INTEGER DEFAULT 0,
    synced_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS personal (
    id            INTEGER PRIMARY KEY,
    funktion      TEXT NOT NULL,
    ressourcenart TEXT,
    tagessatz     REAL DEFAULT 0,
    satzname      TEXT,
    synced_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Trainings-Mappings (aus mappings.json — selten geändert)
CREATE TABLE IF NOT EXISTS mappings_train (
    description TEXT PRIMARY KEY,
    nummer      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mappings_train_extras (
    description TEXT    NOT NULL,
    nummer      TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (description, nummer)
);
CREATE TABLE IF NOT EXISTS mappings_train_sections (
    section_key TEXT    NOT NULL,
    nummer      TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (section_key, nummer)
);

-- GUI-Mappings (aus mappings_gui.json — laufend geschrieben)
CREATE TABLE IF NOT EXISTS mappings_gui (
    description TEXT PRIMARY KEY,
    nummer      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mappings_gui_extras (
    description TEXT    NOT NULL,
    nummer      TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (description, nummer)
);

CREATE INDEX IF NOT EXISTS idx_art_warengruppe  ON articles(warengruppe);
CREATE INDEX IF NOT EXISTS idx_art_bezeichnung  ON articles(bezeichnung);
CREATE INDEX IF NOT EXISTS idx_art_hersteller   ON articles(hersteller);
"""


def init_db() -> None:
    """Erstellt alle Tabellen falls noch nicht vorhanden."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)


# ── Zähler (für Migrationscheck) ──────────────────────────────────────────────

def _count(table: str) -> int:
    with get_conn() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def article_count()  -> int: return _count("articles")
def personal_count() -> int: return _count("personal")
def gui_mapping_count() -> int: return _count("mappings_gui")
def train_mapping_count() -> int: return _count("mappings_train")


# ── Artikel ───────────────────────────────────────────────────────────────────

def upsert_articles(rows: list[dict]) -> int:
    """INSERT OR REPLACE für alle Artikel. Gibt Anzahl verarbeiteter Zeilen zurück."""
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO articles
                (nummer, bezeichnung, mutterwarengruppe, warengruppe,
                 kommentar, artikelart, hersteller, detail,
                 mietpreis, einheit, mietinventar, synced_at)
            VALUES
                (:nummer, :bezeichnung, :mutterwarengruppe, :warengruppe,
                 :kommentar, :artikelart, :hersteller, :detail,
                 :mietpreis, :einheit, :mietinventar, CURRENT_TIMESTAMP)
            ON CONFLICT(nummer) DO UPDATE SET
                bezeichnung       = excluded.bezeichnung,
                mutterwarengruppe = excluded.mutterwarengruppe,
                warengruppe       = excluded.warengruppe,
                kommentar         = excluded.kommentar,
                artikelart        = excluded.artikelart,
                hersteller        = excluded.hersteller,
                detail            = excluded.detail,
                mietpreis         = excluded.mietpreis,
                einheit           = excluded.einheit,
                mietinventar      = excluded.mietinventar,
                synced_at         = CURRENT_TIMESTAMP
        """, rows)
        return len(rows)


def load_articles_db() -> list[dict]:
    """Gibt alle Artikel als Liste von Dicts zurück."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM articles").fetchall()]


# ── Personal ──────────────────────────────────────────────────────────────────

def upsert_personal(rows: list[dict]) -> int:
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO personal (id, funktion, ressourcenart, tagessatz, satzname, synced_at)
            VALUES (:id, :funktion, :ressourcenart, :tagessatz, :satzname, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                funktion      = excluded.funktion,
                ressourcenart = excluded.ressourcenart,
                tagessatz     = excluded.tagessatz,
                satzname      = excluded.satzname,
                synced_at     = CURRENT_TIMESTAMP
        """, rows)
        return len(rows)


def load_personal_db() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM personal").fetchall()]


# ── GUI-Mappings (laufend geschrieben) ────────────────────────────────────────

def save_gui_bundle(description: str, numbers: list[str]) -> None:
    """Speichert Primary + Extras für eine Beschreibung. Leere Liste = löschen."""
    with get_conn() as conn:
        conn.execute("DELETE FROM mappings_gui        WHERE description=?", (description,))
        conn.execute("DELETE FROM mappings_gui_extras WHERE description=?", (description,))
        if numbers:
            conn.execute(
                "INSERT INTO mappings_gui (description, nummer) VALUES (?, ?)",
                (description, numbers[0]),
            )
            if len(numbers) > 1:
                conn.executemany(
                    "INSERT INTO mappings_gui_extras (description, nummer, sort_order) "
                    "VALUES (?, ?, ?)",
                    [(description, n, i) for i, n in enumerate(numbers[1:])],
                )


def load_gui_mappings() -> tuple[dict[str, str], dict[str, list[str]]]:
    """Gibt (primary_map, extras_map) aus den GUI-Mappings zurück."""
    with get_conn() as conn:
        primary = {
            r["description"]: r["nummer"]
            for r in conn.execute("SELECT description, nummer FROM mappings_gui")
        }
        extras: dict[str, list[str]] = {}
        for r in conn.execute(
            "SELECT description, nummer FROM mappings_gui_extras "
            "ORDER BY description, sort_order"
        ):
            extras.setdefault(r["description"], []).append(r["nummer"])
    return primary, extras


# ── Train-Mappings (selten geändert) ─────────────────────────────────────────

def load_train_mappings() -> tuple[dict[str, str], dict[str, list[str]], dict[str, list[str]]]:
    """Gibt (primary_map, extras_map, sections_map) aus den Train-Mappings zurück."""
    with get_conn() as conn:
        primary = {
            r["description"]: r["nummer"]
            for r in conn.execute("SELECT description, nummer FROM mappings_train")
        }
        extras: dict[str, list[str]] = {}
        for r in conn.execute(
            "SELECT description, nummer FROM mappings_train_extras "
            "ORDER BY description, sort_order"
        ):
            extras.setdefault(r["description"], []).append(r["nummer"])
        sections: dict[str, list[str]] = {}
        for r in conn.execute(
            "SELECT section_key, nummer FROM mappings_train_sections "
            "ORDER BY section_key, sort_order"
        ):
            sections.setdefault(r["section_key"], []).append(r["nummer"])
    return primary, extras, sections


# ── Migration von JSON → DB (einmalig beim ersten Start) ──────────────────────

def migrate_from_json(
    artikel_path:       Path | None = None,
    personal_path:      Path | None = None,
    train_path:         Path | None = None,
    gui_path:           Path | None = None,
    force:              bool = False,
) -> dict[str, int]:
    """
    Liest vorhandene JSON-Dateien und befüllt DB-Tabellen.
    force=True: Tabellen vorher leeren (für Neuimport nach kaputten Daten).
    Ohne force: bereits befüllte Tabellen werden NICHT überschrieben.
    """
    base     = Path(__file__).parent
    data_dir = DB_PATH.parent   # data/ Volume in Docker, data/ lokal

    def _find(*candidates: str) -> Path | None:
        """Gibt erste existierende Datei zurück (data_dir zuerst, dann base)."""
        for rel in candidates:
            for root in (data_dir, base):
                p = root / rel
                if p.exists():
                    return p
        return None

    if force:
        with get_conn() as conn:
            conn.execute("DELETE FROM articles")
            conn.execute("DELETE FROM personal")
            conn.execute("DELETE FROM mappings_train")
            conn.execute("DELETE FROM mappings_train_extras")
            conn.execute("DELETE FROM mappings_train_sections")

    stats: dict[str, int] = {}

    # Artikel
    ap = artikel_path or _find("infos/artikel.json")
    if ap and (force or article_count() == 0):
        with open(ap, encoding="utf-8") as f:
            raw = json.load(f)
        items = raw if isinstance(raw, list) else raw.get("items", [])
        rows = [
            {
                "nummer":            it.get("Nummer", ""),
                "bezeichnung":       it.get("Bezeichnung", ""),
                "mutterwarengruppe": it.get("Mutterwarengruppe", ""),
                "warengruppe":       it.get("Warengruppe", ""),
                "kommentar":         it.get("Kommentar") or "",
                "artikelart":        it.get("Artikelart", ""),
                "hersteller":        it.get("Hersteller", ""),
                "detail":            it.get("Detailbeschreibung") or "",
                "mietpreis":         float(it.get("Mietpreis") or 0),
                "einheit":           it.get("Einheit") or "",
                "mietinventar":      int(it.get("Mietinventar") or 0),
            }
            for it in items if it.get("Nummer")
        ]
        stats["articles"] = upsert_articles(rows)

    # Personal
    pp = personal_path or _find("infos/personal.json")
    if pp and (force or personal_count() == 0):
        with open(pp, encoding="utf-8") as f:
            raw = json.load(f)
        rows_r = raw if isinstance(raw, list) else raw.get("rows", [])
        rows_p = [
            {
                "id":           int(r.get("IdResourceFunction", 0)),
                "funktion":     (r.get("Funktion") or "").strip(),
                "ressourcenart": r.get("Ressourcenart", ""),
                "tagessatz":    float(r.get("Tagessatz") or 0),
                "satzname":     r.get("Satzname") or "",
            }
            for r in rows_r if (r.get("Funktion") or "").strip()
        ]
        stats["personal"] = upsert_personal(rows_p)

    # Train-Mappings
    tp = train_path or _find("mappings.json")
    if tp and (force or train_mapping_count() == 0):
        with open(tp, encoding="utf-8") as f:
            m = json.load(f)
        primary  = m.get("article_resolutions", {})
        extras   = m.get("bundle_extras", {})
        sections = m.get("section_articles", {})
        with get_conn() as conn:
            if primary:
                conn.executemany(
                    "INSERT OR IGNORE INTO mappings_train (description, nummer) VALUES (?,?)",
                    primary.items(),
                )
            for desc, nums in extras.items():
                conn.executemany(
                    "INSERT OR IGNORE INTO mappings_train_extras "
                    "(description, nummer, sort_order) VALUES (?,?,?)",
                    [(desc, n, i) for i, n in enumerate(nums)],
                )
            for key, nums in sections.items():
                conn.executemany(
                    "INSERT OR IGNORE INTO mappings_train_sections "
                    "(section_key, nummer, sort_order) VALUES (?,?,?)",
                    [(key, n, i) for i, n in enumerate(nums)],
                )
        stats["train_mappings"] = len(primary)

    # GUI-Mappings (nie force-löschen — enthält gelernte Zuordnungen)
    gp = gui_path or _find("mappings_gui.json")
    if gp and gui_mapping_count() == 0:
        with open(gp, encoding="utf-8") as f:
            m = json.load(f)
        primary = m.get("article_resolutions", {})
        extras  = m.get("bundle_extras", {})
        with get_conn() as conn:
            if primary:
                conn.executemany(
                    "INSERT OR IGNORE INTO mappings_gui (description, nummer) VALUES (?,?)",
                    primary.items(),
                )
            for desc, nums in extras.items():
                conn.executemany(
                    "INSERT OR IGNORE INTO mappings_gui_extras "
                    "(description, nummer, sort_order) VALUES (?,?,?)",
                    [(desc, n, i) for i, n in enumerate(nums)],
                )
        stats["gui_mappings"] = len(primary)

    return stats
