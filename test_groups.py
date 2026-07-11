"""
Testet:
1. Was existiert nach jobs/create in StockType2JobGroup(Parent)?
2. Was macht Items/AddGroup?
3. Funktionieren Gruppen ohne IdJobPartOut/In?

Aufruf: python test_groups.py [IdProject]
"""
import json, os, sys, pyodbc
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
sys.path.insert(0, os.path.dirname(__file__))
from easyjob_api import EjLiveClient

EJ_URL  = os.environ["EJ_BASE_URL"]
EJ_USER = os.environ.get("EJ_USERNAME") or input("EJ-User: ").strip()
EJ_PASS = os.environ.get("EJ_PASSWORD") or input("EJ-Pass: ").strip()
ID_PROJECT = int(sys.argv[1]) if len(sys.argv) > 1 else int(input("IdProject: ").strip())

DB = "DRIVER={SQL Server};SERVER=192.168.2.4\\SQLEXPRESS;DATABASE=easyjob;UID=sa;PWD=_easyjob6P@ssW0rd_"

client = EjLiveClient(EJ_URL, EJ_USER, EJ_PASS)

# ── 1. Test-Job anlegen ───────────────────────────────────────────────────
print("\n[1] Test-Job anlegen...")
resp = client.jobs_create(ID_PROJECT, "TEST-API-JOB", "2026-08-01", "2026-08-05", 1)
job_id = int(resp.get("ID") or resp.get("IdJob") or 0)
if not job_id:
    print("  FEHLER:", resp); sys.exit(1)
print(f"  Job-ID: {job_id}")

# ── 2. DB: Was hat EJ automatisch angelegt? ───────────────────────────────
cn = pyodbc.connect(DB)
cur = cn.cursor()

print("\n[2] StockType2JobGroupParent nach jobs/create:")
rows = cur.execute("SELECT * FROM StockType2JobGroupParent WHERE IdJob=?", job_id).fetchall()
cols = [d[0] for d in cur.description]
if rows:
    for r in rows:
        print(" ", dict(zip(cols, r)))
else:
    print("  (leer – keine Hauptgruppe automatisch angelegt)")

print("\n[3] StockType2JobGroup nach jobs/create:")
rows2 = cur.execute("SELECT * FROM StockType2JobGroup WHERE IdJob=?", job_id).fetchall()
cols2 = [d[0] for d in cur.description]
if rows2:
    for r in rows2:
        print(" ", dict(zip(cols2, r)))
else:
    print("  (leer – keine Gruppe automatisch angelegt)")

# ── 3. API: Items/AddGroup testen ─────────────────────────────────────────
print("\n[4] Items/AddGroup via API:")
gr = client._client._post("/api.json/Items/AddGroup/", params={"id": job_id, "caption": "Test-Gruppe-API"})
print("  Response:", gr)
grp_id = gr.get("ID")

print("\n[5] DB nach Items/AddGroup:")
rows3 = cur.execute("SELECT * FROM StockType2JobGroupParent WHERE IdJob=?", job_id).fetchall()
print("  GroupParent:", [dict(zip(cols, r)) for r in rows3])
rows4 = cur.execute("SELECT * FROM StockType2JobGroup WHERE IdJob=?", job_id).fetchall()
cols4 = [d[0] for d in cur.description]
print("  Group:", [dict(zip(cols4, r)) for r in rows4])

# ── 4. IdJobPartOut/In: NULL vs. Wert ─────────────────────────────────────
print("\n[6] IdJobPartOutDefault/IdJobPartInDefault des Jobs:")
row = cur.execute("SELECT IdJobPartOutDefault, IdJobPartInDefault FROM Job WHERE IdJob=?", job_id).fetchone()
print(f"  Out={row[0]}  In={row[1]}")
print("  -> NULL bedeutet: EJ verwendet eigene Defaults (kein Pflichtfeld)")

cn.close()

# ── Cleanup ───────────────────────────────────────────────────────────────
ans = input(f"\nTest-Job {job_id} loeschen? (j/n): ").strip().lower()
if ans == "j":
    dr = client._client._post(f"/api.json/Jobs/Delete/?id={job_id}")
    print("  Geloescht:", dr)
