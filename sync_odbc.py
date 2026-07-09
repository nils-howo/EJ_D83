"""Nightly sync: EasyJob SQL Server → lokale SQLite DB via pyodbc."""
import logging
import os

import pyodbc

import db

# ── SQL-Abfragen ──────────────────────────────────────────────────────────────

_PERSONAL_SQL = """
SELECT
    rt.CaptionDE          AS Ressourcenart,
    rf.Caption            AS Funktion,
    rf.IdResourceFunction,
    Tagessatz             = ISNULL(satz.DayPayment, rf.DayPayment),
    satz.Satzname
FROM dbo.ResourceFunction rf
    JOIN dbo.ResourceType rt ON rt.IdResourceType = rf.IdResourceType
    OUTER APPLY (
        SELECT TOP (1)
               r2f.DayPayment,
               rr.Caption AS Satzname
        FROM dbo.ResourceRate2Function r2f
            JOIN dbo.ResourceRate rr ON rr.IdResourceRate = r2f.IdResourceRate
        WHERE r2f.IdResourceFunction = rf.IdResourceFunction
          AND ISNULL(rr.Inactive, 0) = 0
        ORDER BY rr.SortOrder, r2f.IdResourceRate2Function
    ) AS satz
WHERE ISNULL(rf.Inactive, 0) = 0
ORDER BY rt.CaptionDE, rf.Caption
"""

_ARTIKEL_SQL = """
SELECT
    st.Number                                       AS Nummer,
    st.Caption                                      AS Bezeichnung,
    ISNULL(cat.Caption,   '')                       AS Warengruppe,
    ISNULL(pcat.Caption,  '')                       AS Mutterwarengruppe,
    ISNULL(st.Comment,    '')                       AS Kommentar,
    -- Custom1/2/3: Benutzerfelder aus StockTypeExtension (primäre Sprache = Deutsch).
    -- Zuordnung: Custom1=Artikelart, Custom2=Hersteller, Custom3=Detailbeschreibung
    -- → ggf. anpassen sobald Mapping bestätigt.
    ISNULL(stx.Custom1,   '')                       AS Artikelart,
    ISNULL(stx.Custom2,   '')                       AS Hersteller,
    ISNULL(stx.Custom3,   '')                       AS Detailbeschreibung,
    ISNULL(preis.Rental,  0)                        AS Mietpreis,
    ISNULL(u.Caption,     '')                       AS Einheit,
    ISNULL(st.Inventory,  0)                        AS Mietinventar
FROM dbo.StockType st
    -- Benutzerfelder (Deutsch = primäre Sprache)
    LEFT JOIN dbo.StockTypeExtension stx
        ON stx.IdStockType = st.IdStockType
    -- Warengruppe
    LEFT JOIN dbo.StockTypeCategory cat
        ON cat.IdStockTypeCategory = st.IdStockTypeCategory
    -- Mutterwarengruppe
    LEFT JOIN dbo.StockTypeCategoryParent pcat
        ON pcat.IdStockTypeCategoryParent = cat.IdStockTypeCategoryParent
    -- Einheit
    LEFT JOIN dbo.Unit u
        ON u.IdUnit = st.IdUnit
    -- Primärer Mietpreis
    OUTER APPLY (
        SELECT TOP (1) stp.Rental
        FROM dbo.StockTypePrice stp
        WHERE stp.IdStockType = st.IdStockType
        ORDER BY stp.IdStockTypePriceGroup
    ) AS preis
WHERE st.Condition = 0                         -- 0 = aktiv, NULL = deaktiviert
  AND ISNULL(st.IsMarketingStockType, 0) = 0
ORDER BY cat.Caption, st.Number
"""


# ── Verbindung ────────────────────────────────────────────────────────────────

def _ej_conn() -> pyodbc.Connection:
    driver   = os.environ.get("EJ_DB_DRIVER", "ODBC Driver 18 for SQL Server")
    server   = os.environ.get("EJ_DB_SERVER", "")
    database = os.environ.get("EJ_DB_NAME",   "")
    uid      = os.environ.get("EJ_DB_UID",    "")
    pwd      = os.environ.get("EJ_DB_PWD",    "")
    conn_str = (
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
        f"UID={uid};PWD={pwd};TrustServerCertificate=yes"
    )
    return pyodbc.connect(conn_str, timeout=15)


# ── Personal ──────────────────────────────────────────────────────────────────

def sync_personal() -> tuple[int, int]:
    """Synct Personal von EJ SQL Server → SQLite. Gibt (neu, aktualisiert) zurück."""
    conn = _ej_conn()
    try:
        cursor = conn.cursor()
        cursor.execute(_PERSONAL_SQL)
        cols = [d[0] for d in cursor.description]
        rows = []
        for row in cursor.fetchall():
            r = dict(zip(cols, row))
            rows.append({
                "id":           int(r["IdResourceFunction"]),
                "funktion":     (r.get("Funktion") or "").strip(),
                "ressourcenart": r.get("Ressourcenart") or "",
                "tagessatz":    float(r.get("Tagessatz") or 0),
                "satzname":     r.get("Satzname") or "",
            })
    finally:
        conn.close()

    if not rows:
        logging.warning("sync_personal: Keine Zeilen von EJ erhalten")
        return 0, 0

    new_c, upd_c = db.upsert_personal_tracked(rows)
    logging.info("Personal-Sync: %d Gesamt, %d neu, %d aktualisiert", len(rows), new_c, upd_c)
    return new_c, upd_c


# ── Artikel ───────────────────────────────────────────────────────────────────

def sync_articles() -> tuple[int, int]:
    """Synct Artikel von EJ SQL Server → SQLite. Gibt (neu, aktualisiert) zurück."""
    conn = _ej_conn()
    try:
        cursor = conn.cursor()
        cursor.execute(_ARTIKEL_SQL)
        cols = [d[0] for d in cursor.description]
        rows = []
        for row in cursor.fetchall():
            r = dict(zip(cols, row))
            rows.append({
                "nummer":            str(r.get("Nummer") or "").strip(),
                "bezeichnung":       r.get("Bezeichnung") or "",
                "mutterwarengruppe": r.get("Mutterwarengruppe") or "",
                "warengruppe":       r.get("Warengruppe") or "",
                "kommentar":         r.get("Kommentar") or "",
                "artikelart":        r.get("Artikelart") or "",
                "hersteller":        r.get("Hersteller") or "",
                "detail":            r.get("Detailbeschreibung") or "",
                "mietpreis":         float(r.get("Mietpreis") or 0),
                "einheit":           r.get("Einheit") or "",
                "mietinventar":      int(r.get("Mietinventar") or 0),
            })
        rows = [r for r in rows if r["nummer"]]
    finally:
        conn.close()

    if not rows:
        logging.warning("sync_articles: Keine Zeilen von EJ erhalten")
        return 0, 0

    new_c, upd_c = db.upsert_articles_tracked(rows)
    logging.info("Artikel-Sync: %d Gesamt, %d neu, %d aktualisiert", len(rows), new_c, upd_c)
    return new_c, upd_c


# ── Vollständiger Sync ────────────────────────────────────────────────────────

def run_full_sync() -> dict:
    """Führt kompletten ODBC-Sync durch. Gibt Stats-Dict zurück."""
    stats: dict = {}

    try:
        p_new, p_upd = sync_personal()
        stats["personal_new"]     = p_new
        stats["personal_updated"] = p_upd
    except Exception as exc:
        logging.error("Personal-Sync fehlgeschlagen: %s", exc)
        stats["personal_error"] = str(exc)

    try:
        a_new, a_upd = sync_articles()
        stats["articles_new"]     = a_new
        stats["articles_updated"] = a_upd
    except Exception as exc:
        logging.error("Artikel-Sync fehlgeschlagen: %s", exc)
        stats["articles_error"] = str(exc)

    return stats
