"""
Testet verschiedene Varianten der jobs/create-Bestätigung gegen die EJ-API.
Aufruf: python test_jobs_create.py [IdProject]
"""
import json, os, sys
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
sys.path.insert(0, os.path.dirname(__file__))
from easyjob_api import EjLiveClient

EJ_URL  = os.environ["EJ_BASE_URL"]
EJ_USER = os.environ.get("EJ_USERNAME") or input("EJ-User: ").strip()
EJ_PASS = os.environ.get("EJ_PASSWORD") or input("EJ-Pass: ").strip()
ID_PROJECT = int(sys.argv[1]) if len(sys.argv) > 1 else int(input("IdProject: ").strip())

client = EjLiveClient(EJ_URL, EJ_USER, EJ_PASS)

def post(body):
    return client._client._post("/api.json/v2/rental/jobs/create", body=body)

def show(label, resp):
    jid = resp.get("ID") or resp.get("IdJob")
    has_ctx = "ModelContext" in resp
    print(f"  [{label}] ID={jid}  ModelContext={has_ctx}  Success={resp.get('Success')}")
    return jid

base = {"IdProject": ID_PROJECT, "Caption": "Test-Job-DELETE-ME", "IdAddressDelivery": 1}

# ── Schritt 1: Basis-Request ─────────────────────────────────────────────
print(f"\nProjekt {ID_PROJECT} – jobs/create Variantentest")
r1 = post(base)
if show("1: base", r1):
    sys.exit(0)

ctx = r1.get("ModelContext")
if not ctx:
    print("Kein ModelContext – unerwartete Antwort:", r1)
    sys.exit(1)

ctx_msg = ctx["ContextMessage"]
print(f"  Dialog: {ctx_msg['Key']}  Buttons={ctx_msg['Buttons']}")

# ── Varianten der Bestätigung ────────────────────────────────────────────
variants = [
    # (Label, wie wird ContextMessage.Value gesetzt)
    ("2: ctx zurück, Value=None",      None),
    ("3: ctx zurück, Value='Ok'",      "Ok"),
    ("4: ctx zurück, Value='1'",       "1"),
    ("5: ctx zurück, Value=Key",       ctx_msg["Key"]),
    ("6: ctx zurück, Value='Confirm'", "Confirm"),
]

for label, val in variants:
    import copy
    ctx2 = copy.deepcopy(ctx)
    ctx2["ContextMessage"]["Value"] = val
    r = post({**base, "ModelContext": ctx2})
    jid = show(label, r)
    if jid:
        print(f"\n  Funktioniert! Variante: {label}")
        ans = input("  Jetzt loeschen? (j/n): ").strip().lower()
        if ans == "j":
            dr = client._client._post(f"/api.json/Jobs/Delete/?id={jid}")
            print("  Loeschen:", dr)
        sys.exit(0)
    # Neuen Context fuer naechsten Versuch holen
    if "ModelContext" in r:
        ctx = r["ModelContext"]
        ctx_msg = ctx["ContextMessage"]

# ── Fallback: Projekt ohne EventCalendar testen ──────────────────────────
print("\n  Alle Varianten gescheitert.")
print("  Teste mit einem Projekt ohne EventCalendar...")
import pyodbc
cn = pyodbc.connect(
    "DRIVER={SQL Server};SERVER=192.168.2.4\\SQLEXPRESS;"
    "DATABASE=easyjob;UID=sa;PWD=_easyjob6P@ssW0rd_"
)
row = cn.execute(
    "SELECT TOP 1 IdProject, Caption FROM Project "
    "WHERE IdEventCalendar IS NULL OR IdEventCalendar = 0 "
    "ORDER BY IdProject DESC"
).fetchone()
cn.close()
if row:
    print(f"  Projekt ohne EventCalendar: {row[0]} – {row[1]}")
    base2 = {**base, "IdProject": row[0]}
    r2 = post(base2)
    jid2 = show("7: Projekt ohne EventCalendar", r2)
    if jid2:
        print("  -> Dialog tritt NUR bei Projekten MIT EventCalendar auf!")
        print("     Loesung: IdEventCalendar=0 beim Projekt-Create setzen oder Dialog akzeptieren.")
        ans = input("  Jetzt loeschen? (j/n): ").strip().lower()
        if ans == "j":
            client._client._post(f"/api.json/Jobs/Delete/?id={jid2}")
else:
    print("  Kein Projekt ohne EventCalendar gefunden.")
