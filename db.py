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
    ej_id             INTEGER DEFAULT 0,
    id_time_factor    INTEGER DEFAULT 0,
    synced_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Berechnungsgrundlagen (EJ: TimeFactor / TimeFactorItem) — Preis-Progressionskurven
-- je nach Einsatztagen. Referenzdaten, ändern sich selten, werden komplett ersetzt.
CREATE TABLE IF NOT EXISTS time_factors (
    id          INTEGER PRIMARY KEY,
    caption     TEXT,
    description TEXT
);
CREATE TABLE IF NOT EXISTS time_factor_items (
    id                        INTEGER PRIMARY KEY,
    id_time_factor            INTEGER NOT NULL,
    commitment_days           REAL,
    step_next_commitment_days REAL,
    factor                    REAL
);
CREATE INDEX IF NOT EXISTS idx_tfi_time_factor ON time_factor_items(id_time_factor);

CREATE TABLE IF NOT EXISTS personal (
    id            INTEGER PRIMARY KEY,
    funktion      TEXT NOT NULL,
    ressourcenart TEXT,
    tagessatz     REAL DEFAULT 0,
    eigenkosten   REAL DEFAULT 0,
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

-- GUI-Ressourcen-Mappings (gelernt wenn Benutzer Ressource manuell zuordnet)
CREATE TABLE IF NOT EXISTS mappings_gui_resources (
    description TEXT    PRIMARY KEY,
    resource_id INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_art_warengruppe  ON articles(warengruppe);
CREATE INDEX IF NOT EXISTS idx_art_bezeichnung  ON articles(bezeichnung);
CREATE INDEX IF NOT EXISTS idx_art_hersteller   ON articles(hersteller);

CREATE TABLE IF NOT EXISTS sync_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type         TEXT    NOT NULL,
    started_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at      TIMESTAMP,
    articles_new     INTEGER DEFAULT 0,
    articles_updated INTEGER DEFAULT 0,
    personal_new     INTEGER DEFAULT 0,
    personal_updated INTEGER DEFAULT 0,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    ej_project_id     INTEGER,
    ej_project_number TEXT,
    ej_job_ids        TEXT,
    gaeb_name         TEXT,
    item_count        INTEGER DEFAULT 0,
    booking_count     INTEGER DEFAULT 0,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    gaeb_bytes        BLOB
);

CREATE TABLE IF NOT EXISTS project_bookings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER NOT NULL REFERENCES projects(id),
    item_id          TEXT    NOT NULL,
    kind             TEXT    DEFAULT 'article',   -- 'article' | 'resource'
    oz               TEXT,
    description      TEXT,
    art_num          TEXT,
    ej_stock_type_id INTEGER,   -- Artikel: IdStockType · Ressource: IdResourceFunction
    ej_s2j_id        INTEGER,
    ej_group_id      INTEGER,
    qty              REAL    DEFAULT 1,           -- Artikel: Stückzahl · Ressource: Tage
    unit_price       REAL    DEFAULT 0            -- Artikel: EP · Ressource: Tagessatz
);
"""


def init_db() -> None:
    """Erstellt alle Tabellen falls noch nicht vorhanden. Migriert fehlende Spalten."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        # Migration: Spalten die in älteren DBs fehlen könnten
        for sql in [
            "ALTER TABLE projects ADD COLUMN gaeb_bytes BLOB",
            "ALTER TABLE project_bookings ADD COLUMN unit_price REAL DEFAULT 0",
            "ALTER TABLE articles ADD COLUMN ej_id INTEGER DEFAULT 0",
            "ALTER TABLE personal ADD COLUMN eigenkosten REAL DEFAULT 0",
            "ALTER TABLE articles ADD COLUMN id_time_factor INTEGER DEFAULT 0",
            "ALTER TABLE projects ADD COLUMN ej_job_ids TEXT",
            "ALTER TABLE projects ADD COLUMN ej_project_number TEXT",
            "ALTER TABLE project_bookings ADD COLUMN kind TEXT DEFAULT 'article'",
        ]:
            try:
                conn.execute(sql)
            except Exception:
                pass  # Spalte existiert bereits


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
                 mietpreis, einheit, mietinventar, ej_id, id_time_factor, synced_at)
            VALUES
                (:nummer, :bezeichnung, :mutterwarengruppe, :warengruppe,
                 :kommentar, :artikelart, :hersteller, :detail,
                 :mietpreis, :einheit, :mietinventar, :ej_id, :id_time_factor, CURRENT_TIMESTAMP)
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
                ej_id             = excluded.ej_id,
                id_time_factor    = excluded.id_time_factor,
                synced_at         = CURRENT_TIMESTAMP
        """, rows)
        return len(rows)


def load_articles_db() -> list[dict]:
    """Gibt alle Artikel als Liste von Dicts zurück."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM articles").fetchall()]


def load_articles_by_ej_ids(ej_ids) -> list[dict]:
    """Lädt nur die Artikel mit den angegebenen EJ-IdStockType (ej_id) — deutlich
    schneller als alle Artikel zu laden, wenn nur ein paar gebraucht werden."""
    ids = sorted({int(i) for i in ej_ids if i})
    if not ids:
        return []
    ph = ",".join("?" for _ in ids)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM articles WHERE ej_id IN ({ph})", ids).fetchall()]


# ── Berechnungsgrundlagen (TimeFactor / TimeFactorItem) ───────────────────────
# Kleine, selten geänderte Referenzdaten (EJ: 5 Kurven, ~30 Stufen) — werden bei
# jedem Sync komplett ersetzt statt einzeln abgeglichen zu werden.

def upsert_time_factors(rows: list[dict]) -> int:
    """Ersetzt alle Berechnungsgrundlagen-Kurven (Caption/Description)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM time_factors")
        conn.executemany(
            "INSERT INTO time_factors (id, caption, description) "
            "VALUES (:id, :caption, :description)",
            rows,
        )
        return len(rows)


def upsert_time_factor_items(rows: list[dict]) -> int:
    """Ersetzt alle Kurven-Stufen (CommitmentDays → Factor je Kurve)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM time_factor_items")
        conn.executemany(
            "INSERT INTO time_factor_items "
            "(id, id_time_factor, commitment_days, step_next_commitment_days, factor) "
            "VALUES (:id, :id_time_factor, :commitment_days, :step_next_commitment_days, :factor)",
            rows,
        )
        return len(rows)


def load_time_factor_curves_db() -> dict[int, list[dict]]:
    """Gibt {id_time_factor: [Stufen aufsteigend nach commitment_days]} zurück."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id_time_factor, commitment_days, step_next_commitment_days, factor "
            "FROM time_factor_items ORDER BY id_time_factor, commitment_days"
        ).fetchall()
    curves: dict[int, list[dict]] = {}
    for r in rows:
        curves.setdefault(r["id_time_factor"], []).append({
            "commitment_days":           r["commitment_days"],
            "step_next_commitment_days": r["step_next_commitment_days"],
            "factor":                    r["factor"],
        })
    return curves


# ── Personal ──────────────────────────────────────────────────────────────────

def upsert_personal(rows: list[dict]) -> int:
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO personal (id, funktion, ressourcenart, tagessatz, eigenkosten, satzname, synced_at)
            VALUES (:id, :funktion, :ressourcenart, :tagessatz, :eigenkosten, :satzname, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                funktion      = excluded.funktion,
                ressourcenart = excluded.ressourcenart,
                tagessatz     = excluded.tagessatz,
                eigenkosten   = excluded.eigenkosten,
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


def save_gui_resource_mapping(description: str, resource_id: int) -> None:
    """Speichert GAEB-Beschreibung → Ressource-ID. resource_id=0 = löschen."""
    with get_conn() as conn:
        conn.execute("DELETE FROM mappings_gui_resources WHERE description=?", (description,))
        if resource_id:
            conn.execute(
                "INSERT INTO mappings_gui_resources (description, resource_id) VALUES (?, ?)",
                (description, resource_id),
            )


def load_gui_resource_mappings() -> dict[str, int]:
    """Gibt {description: resource_id} aus den Ressourcen-GUI-Mappings zurück."""
    with get_conn() as conn:
        return {
            r["description"]: r["resource_id"]
            for r in conn.execute("SELECT description, resource_id FROM mappings_gui_resources")
        }


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


# ── Sync-Log ──────────────────────────────────────────────────────────────────

def log_sync(run_type: str, articles_new: int = 0, articles_updated: int = 0,
             personal_new: int = 0, personal_updated: int = 0, notes: str = "") -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO sync_log
                (run_type, finished_at, articles_new, articles_updated,
                 personal_new, personal_updated, notes)
            VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)
        """, (run_type, articles_new, articles_updated, personal_new, personal_updated, notes))


def get_sync_history(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, run_type, finished_at,
                   articles_new, articles_updated,
                   personal_new, personal_updated, notes
            FROM sync_log ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_db_stats() -> dict:
    with get_conn() as conn:
        art   = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        pers  = conn.execute("SELECT COUNT(*) FROM personal").fetchone()[0]
        gui   = conn.execute("SELECT COUNT(*) FROM mappings_gui").fetchone()[0]
        train = conn.execute("SELECT COUNT(*) FROM mappings_train").fetchone()[0]
        last_art = conn.execute(
            "SELECT MAX(synced_at) FROM articles"
        ).fetchone()[0]
    return dict(articles=art, personal=pers, gui_mappings=gui,
                train_mappings=train, last_article_sync=last_art)


def get_gui_mappings(limit: int = 500) -> list[dict]:
    """Gibt alle GUI-Korrekturen zurück (description → nummer + Artikelname + extras)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT m.description, m.nummer, COALESCE(a.bezeichnung, '') AS bezeichnung
            FROM mappings_gui m
            LEFT JOIN articles a ON a.nummer = m.nummer
            ORDER BY m.description LIMIT ?
        """, (limit,)).fetchall()
        extras = {}
        for r in conn.execute(
            "SELECT description, nummer FROM mappings_gui_extras ORDER BY sort_order"
        ):
            extras.setdefault(r["description"], []).append(r["nummer"])
    result = []
    for r in rows:
        result.append({"description": r["description"], "nummer": r["nummer"],
                       "bezeichnung": r["bezeichnung"],
                       "extras": extras.get(r["description"], [])})
    return result


def get_train_mappings(limit: int = 500) -> list[dict]:
    """Gibt gelernte Train-Mappings zurück."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT m.description, m.nummer, COALESCE(a.bezeichnung, '') AS bezeichnung
            FROM mappings_train m
            LEFT JOIN articles a ON a.nummer = m.nummer
            ORDER BY m.description LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── Import-Projekte ───────────────────────────────────────────────────────────

def save_project(
    name: str,
    ej_project_id: int,
    gaeb_name: str,
    item_count: int,
    booking_count: int,
    gaeb_bytes: bytes | None = None,
    ej_job_ids: str = "",
    ej_project_number: str = "",
) -> int:
    """Speichert ein angelegtes Projekt. Gibt die neue lokale ID zurück.

    ej_job_ids: kommaseparierte Liste der in EJ angelegten Job-IDs — der D84-Export
    aggregiert ausschließlich über diese Jobs (wichtig im Bestehend-Projekt-Modus,
    damit fremde Jobs des Projekts nicht mitgerechnet werden).
    ej_project_number: die menschenlesbare EJ-Projektnummer (z.B. „26-0994"); wird
    beim Anlegen einmalig per API geholt, damit die Projekte-Seite keine Abfrage braucht.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects
                (name, ej_project_id, ej_project_number, ej_job_ids,
                 gaeb_name, item_count, booking_count, gaeb_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, ej_project_id, ej_project_number, ej_job_ids,
             gaeb_name, item_count, booking_count, gaeb_bytes),
        )
        return cur.lastrowid


def set_project_number(project_id: int, ej_project_number: str) -> None:
    """Trägt die EJ-Projektnummer nachträglich ein (Backfill für Altprojekte)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET ej_project_number = ? WHERE id = ?",
            (ej_project_number, project_id),
        )


def add_project_booking(
    project_id: int,
    item_id: str,
    oz: str,
    description: str,
    art_num: str,
    ej_stock_type_id: int,
    ej_s2j_id: int,
    ej_group_id: int,
    qty: float,
    unit_price: float = 0.0,
    kind: str = "article",
) -> None:
    """Fügt eine Buchungszeile für ein Projekt hinzu.

    kind='article' (Standard) oder 'resource'. Bei Ressourcen ist
    ej_stock_type_id die IdResourceFunction, qty = Tage, unit_price = Tagessatz.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO project_bookings
                (project_id, item_id, kind, oz, description, art_num,
                 ej_stock_type_id, ej_s2j_id, ej_group_id, qty, unit_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, item_id, kind, oz, description, art_num,
             ej_stock_type_id, ej_s2j_id, ej_group_id, qty, unit_price),
        )


def list_projects(limit: int = 100) -> list[dict]:
    """Gibt alle Projekte zurück, neueste zuerst."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, name, ej_project_id, ej_project_number, gaeb_name,
                   item_count, booking_count, created_at,
                   CASE WHEN gaeb_bytes IS NOT NULL THEN 1 ELSE 0 END AS has_gaeb
            FROM projects ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_project_gaeb(project_id: int) -> tuple[bytes | None, str | None]:
    """Gibt (gaeb_bytes, gaeb_name) für ein Projekt zurück."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT gaeb_bytes, gaeb_name FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    if not row:
        return None, None
    return row["gaeb_bytes"], row["gaeb_name"]


def get_project(project_id: int) -> dict | None:
    """Gibt alle Metadaten eines Projekts zurück (ohne gaeb_bytes)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, ej_project_id, ej_project_number, ej_job_ids, gaeb_name, "
            "item_count, booking_count, created_at "
            "FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    return dict(row) if row else None


def get_project_bookings(project_id: int) -> list[dict]:
    """Gibt alle Buchungszeilen für ein Projekt zurück."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, project_id, item_id, kind, oz, description, art_num,
                   ej_stock_type_id, ej_s2j_id, ej_group_id, qty, unit_price
            FROM project_bookings WHERE project_id = ? ORDER BY id
            """,
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_project(project_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM project_bookings WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def get_recent_changes(hours: int = 48) -> list[dict]:
    """Artikel die in den letzten N Stunden upserted wurden."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT nummer, bezeichnung, warengruppe, mietpreis, synced_at
            FROM articles
            WHERE synced_at >= datetime('now', ? || ' hours')
            ORDER BY synced_at DESC LIMIT 200
        """, (f"-{hours}",)).fetchall()
    return [dict(r) for r in rows]


def _art_sig(r: dict) -> tuple:
    """Vergleichbarer Fingerabdruck aller relevanten Artikel-Felder."""
    return (
        int(r.get("ej_id") or 0),
        r.get("bezeichnung", ""),
        r.get("warengruppe", ""),
        r.get("mutterwarengruppe", ""),
        r.get("artikelart", ""),
        r.get("hersteller", ""),
        r.get("kommentar", ""),
        r.get("detail", ""),
        float(r.get("mietpreis") or 0),
        r.get("einheit", ""),
        int(r.get("mietinventar") or 0),
        int(r.get("id_time_factor") or 0),
    )


def upsert_articles_tracked(rows: list[dict]) -> tuple[int, int]:
    """Upsert mit vollständigem Change-Tracking. Gibt (neu, aktualisiert) zurück."""
    existing: dict[str, tuple] = {}
    with get_conn() as conn:
        for r in conn.execute(
            "SELECT nummer, ej_id, bezeichnung, warengruppe, mutterwarengruppe, "
            "artikelart, hersteller, kommentar, detail, mietpreis, einheit, mietinventar, "
            "id_time_factor "
            "FROM articles"
        ):
            existing[r["nummer"]] = _art_sig(dict(r))

    new_rows:     list[dict] = []
    changed_rows: list[dict] = []
    unchanged:    list[dict] = []

    for r in rows:
        prev = existing.get(r["nummer"])
        if prev is None:
            new_rows.append(r)
        elif prev != _art_sig(r):
            changed_rows.append(r)
        else:
            unchanged.append(r)

    # Neue + geänderte Artikel mit aktuellem synced_at schreiben
    if new_rows or changed_rows:
        upsert_articles(new_rows + changed_rows)

    return len(new_rows), len(changed_rows)


def _pers_sig(r: dict) -> tuple:
    return (
        (r.get("funktion") or "").strip(),
        r.get("ressourcenart", ""),
        float(r.get("tagessatz") or 0),
        float(r.get("eigenkosten") or 0),
        r.get("satzname", ""),
    )


def upsert_personal_tracked(rows: list[dict]) -> tuple[int, int]:
    """Upsert mit vollständigem Change-Tracking. Gibt (neu, aktualisiert) zurück."""
    existing: dict[int, tuple] = {}
    with get_conn() as conn:
        for r in conn.execute(
            "SELECT id, funktion, ressourcenart, tagessatz, eigenkosten, satzname FROM personal"
        ):
            existing[r["id"]] = _pers_sig(dict(r))

    new_rows:     list[dict] = []
    changed_rows: list[dict] = []

    for r in rows:
        rid  = int(r.get("id", 0))
        prev = existing.get(rid)
        if prev is None:
            new_rows.append(r)
        elif prev != _pers_sig(r):
            changed_rows.append(r)

    if new_rows or changed_rows:
        upsert_personal(new_rows + changed_rows)

    return len(new_rows), len(changed_rows)
