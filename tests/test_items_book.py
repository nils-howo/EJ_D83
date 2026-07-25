"""
Testet items/book: Artikel in eine bestimmte EJ-Gruppe buchen.

Aufruf: python test_items_book.py [IdJob]
        python test_items_book.py        (fragt interaktiv)

Was wir lernen wollen:
  1. Funktioniert items/book mit IdStockType2JobGroup?
  2. In welcher DB-Tabelle landet der Artikel?
  3. Wie liest man die gebuchten Artikel wieder aus?
  4. Funktioniert loeschen wieder?
"""
import json, os, sys, pyodbc
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from easyjob_client import EasyjobClient

EJ_URL  = os.environ["EJ_BASE_URL"]
EJ_USER = os.environ.get("EJ_USERNAME") or input("EJ-User: ").strip()
EJ_PASS = os.environ.get("EJ_PASSWORD") or input("EJ-Pass: ").strip()
ID_JOB  = int(sys.argv[1]) if len(sys.argv) > 1 else int(input("IdJob (vorhandener Job): ").strip())

DB = (
    "DRIVER={SQL Server};SERVER=192.168.2.4\\SQLEXPRESS;"
    "DATABASE=easyjob;UID=sa;PWD=_easyjob6P@ssW0rd_"
)

client = EasyjobClient(EJ_URL, EJ_USER, EJ_PASS)
cn = pyodbc.connect(DB)
cur = cn.cursor()

# ── 1. Gruppen des Jobs aus DB ────────────────────────────────────────────────
print(f"\n[1] Gruppen von Job {ID_JOB}:")
rows = cur.execute(
    "SELECT IdStockType2JobGroup, Caption, SortOrder FROM StockType2JobGroup "
    "WHERE IdJob=? ORDER BY SortOrder",
    ID_JOB
).fetchall()
if not rows:
    print("  Keine Gruppen – bitte erst Gruppen anlegen (D83-Import).")
    cn.close(); sys.exit(1)

groups = [{"id": r[0], "caption": r[1], "sort": r[2]} for r in rows]
for g in groups:
    print(f"  ID={g['id']}  Sort={g['sort']}  Caption={g['caption']!r}")

target_group = groups[0]
print(f"\n  -> Ziel-Gruppe: {target_group['caption']!r} (ID={target_group['id']})")

# ── 2. Test-Artikel finden ────────────────────────────────────────────────────
print("\n[2] Test-Artikel suchen...")
art_row = cur.execute(
    "SELECT TOP 1 IdStockType, Number, Caption FROM StockType "
    "WHERE Rentable=1 AND Deleted=0 ORDER BY IdStockType"
).fetchone()
if not art_row:
    print("  Kein Artikel gefunden!"); cn.close(); sys.exit(1)

id_art, art_num, art_cap = art_row
print(f"  Artikel: #{art_num}  {art_cap!r}  (IdStockType={id_art})")

# ── 3. Artikel buchen (items/book) ────────────────────────────────────────────
print(f"\n[3] items/book: Artikel {id_art} → Job {ID_JOB}, Gruppe {target_group['id']}...")
resp = client._post("/api.json/items/book", body={
    "IdStockType":          id_art,
    "IdJob":                ID_JOB,
    "Quantity":             2,
    "IdStockType2JobGroup": target_group["id"],
})
print(f"  Response: {resp}")
booked_id = resp.get("ID") if isinstance(resp, dict) else None

# ── 4. DB prüfen: Was wurde angelegt? ────────────────────────────────────────
print("\n[4] DB-Ergebnis nach items/book:")

# StockType2Job – Haupttabelle für Artikelbuchungen
s2j = cur.execute(
    "SELECT TOP 5 IdStockType2Job, IdStockType, IdJob, IdStockType2JobGroup, "
    "       Quantity, PricePerUnit, Caption "
    "FROM StockType2Job WHERE IdJob=? ORDER BY IdStockType2Job DESC",
    ID_JOB
).fetchall()
cols = [d[0] for d in cur.description]
print("  StockType2Job (neueste 5):")
for r in s2j:
    print(f"  {dict(zip(cols, r))}")

# Alternativ: StockType2Job (andere mögliche Tabellen?)
print("\n[5] Andere mögliche Tabellen prüfen...")
for tbl in ["JobItem", "JobMaterial", "RentalItem", "PositionItem"]:
    try:
        n = cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE IdJob=?", ID_JOB).fetchone()[0]
        print(f"  {tbl}: {n} Zeilen")
    except:
        pass  # Tabelle existiert nicht

# ── 5. Wie liest man Artikel eines Jobs aus? ──────────────────────────────────
print("\n[6] API: Artikel des Jobs auslesen...")
for path in [
    f"/api.json/Items/JobItems/?id={ID_JOB}",
    f"/api.json/v2/rental/jobitems/grid?IdJob={ID_JOB}",
    f"/api.json/Jobs/Details/?id={ID_JOB}",
]:
    try:
        r = client._get(path.split("?")[0], dict(p.split("=") for p in (path.split("?")[1:] or [""])[0].split("&") if "=" in p) if "?" in path else {})
        preview = str(r)[:200]
        print(f"  {path.split('/')[-1].split('?')[0]}: {preview}")
        break
    except Exception as e:
        print(f"  {path}: FEHLER – {e}")

# ── 6. Cleanup ────────────────────────────────────────────────────────────────
ans = input(f"\nGebuchten Artikel wieder loeschen? (j/n): ").strip().lower()
if ans == "j" and booked_id:
    try:
        dr = client._post(f"/api.json/Items/Delete/", params={"id": booked_id})
        print(f"  Geloescht: {dr}")
    except Exception as e:
        # Alternativ direkt per DB
        cur.execute("DELETE FROM StockType2Job WHERE IdStockType2Job=?", booked_id)
        cn.commit()
        print(f"  Per DB geloescht (API-Fehler: {e})")

cn.close()
print("\nFertig.")
