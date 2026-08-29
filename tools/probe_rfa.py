"""Nachsehen, wie Easyjob selbst eine Mehrfachbesetzung ablegt.

Vor dem Buchen aus der Personalplanung ist eine Frage offen: sechs Manntage —
steht das in Easyjob als *eine* Zeile mit ``DaysInAction = 6`` über drei Tage, oder
als *zwei* Zeilen mit je 3? Preislich ist beides gleich, in der Personaldisposition
nicht. Statt zu raten wird hier gelesen, was Disponenten über die Oberfläche
angelegt haben — deren Zeilen sind die Vorlage.

Rein lesend: nur SELECT, kein INSERT, kein UPDATE. Ausgegeben werden Zählungen und
anonyme Beispielzeilen, keine Kunden- oder Personendaten.

    .venv/Scripts/python.exe tools/probe_rfa.py

Zugang kommt aus ``.env``, genau wie beim Anmelden am Werkzeug selbst
(``EJ_DB_SERVER``, ``EJ_DB_NAME``, ``EJ_DB_UID``, ``EJ_DB_PWD``) — die Vorgabe dort
ist das Testsystem. Ein abweichender Connection-String kann als Argument mitgegeben
werden; ausgegeben wird er nie.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv        # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from sync_odbc import _ej_conn        # noqa: E402


# Spanne = Kalendertage von DateStart bis DateEnd. Ist DaysInAction größer, steckt
# in der Zeile mehr als eine Person; ist es gleich, ist es eine Person je Zeile.
_VERTEILUNG = """
SELECT
    kategorie = CASE
        WHEN rfa.DaysInAction > DATEDIFF(day, rfa.DateStart, rfa.DateEnd) + 1
            THEN 'mehr Tage als Kalendertage (Kopfzahl in EINER Zeile)'
        WHEN rfa.DaysInAction = DATEDIFF(day, rfa.DateStart, rfa.DateEnd) + 1
            THEN 'genau die Kalendertage (eine Person je Zeile)'
        ELSE 'weniger als die Kalendertage (Teilzeit oder Lücken)'
    END,
    COUNT(*) AS Zeilen
FROM dbo.ResourceFunctionAllocation rfa
WHERE rfa.DateStart IS NOT NULL AND rfa.DateEnd IS NOT NULL
  AND rfa.DaysInAction > 0
  AND rfa.DateStart >= DATEADD(year, -2, GETDATE())
GROUP BY CASE
        WHEN rfa.DaysInAction > DATEDIFF(day, rfa.DateStart, rfa.DateEnd) + 1
            THEN 'mehr Tage als Kalendertage (Kopfzahl in EINER Zeile)'
        WHEN rfa.DaysInAction = DATEDIFF(day, rfa.DateStart, rfa.DateEnd) + 1
            THEN 'genau die Kalendertage (eine Person je Zeile)'
        ELSE 'weniger als die Kalendertage (Teilzeit oder Lücken)'
    END
ORDER BY Zeilen DESC
"""

# Dieselbe Funktion mehrfach im selben Job mit derselben Spanne: das ist die
# Handschrift von „eine Zeile je Person".
_DOPPELTE = """
SELECT TOP (12)
    rfa.IdJob, rfa.IdResourceFunction, rfa.DateStart, rfa.DateEnd,
    COUNT(*) AS GleicheZeilen, SUM(rfa.DaysInAction) AS SummeTage
FROM dbo.ResourceFunctionAllocation rfa
WHERE rfa.DateStart IS NOT NULL AND rfa.DateEnd IS NOT NULL
  AND rfa.DaysInAction > 0
  AND rfa.DateStart >= DATEADD(year, -2, GETDATE())
GROUP BY rfa.IdJob, rfa.IdResourceFunction, rfa.DateStart, rfa.DateEnd
HAVING COUNT(*) > 1
ORDER BY COUNT(*) DESC
"""

# Beispiele für Zeilen mit Kopfzahl — sie zeigen, ob dabei ein Feld mitläuft, das
# wir bisher nicht schreiben (Quantity etwa).
_BEISPIELE = """
SELECT TOP (10)
    rfa.IdResourceFunction, rfa.DateStart, rfa.DateEnd, rfa.DaysInAction,
    rfa.Quantity, rfa.QuantityInvoice, rfa.HoursInAction, rfa.ScheduledByEvent,
    rfa.IdStockType2JobGroup
FROM dbo.ResourceFunctionAllocation rfa
WHERE rfa.DateStart IS NOT NULL AND rfa.DateEnd IS NOT NULL
  AND rfa.DaysInAction > DATEDIFF(day, rfa.DateStart, rfa.DateEnd) + 1
  AND rfa.DateStart >= DATEADD(year, -2, GETDATE())
ORDER BY rfa.IdResourceFunctionAllocation DESC
"""

# Gibt es die Nebenkosten-Ressourcen, mit denen die Planung rechnet?
_RESSOURCEN = """
SELECT rf.IdResourceFunction, rf.Caption, rf.DayPayment,
       ISNULL(rf.Inactive, 0) AS Inaktiv
FROM dbo.ResourceFunction rf
WHERE rf.IdResourceFunction IN (?, ?, ?)
ORDER BY rf.IdResourceFunction
"""


# Die Zeilen mit Kopfzahl könnten unsere eigenen Importe sein: die setzen
# DateEnd = DateStart + 1 Tag und Mitternacht als Uhrzeit. Trifft das zu, sind sie
# kein Vorbild, sondern genau das, was hier abgelöst wird.
_HERKUNFT = """
SELECT
    form = CASE
        WHEN DATEDIFF(day, rfa.DateStart, rfa.DateEnd) = 1
             AND CAST(rfa.DateStart AS time) = '00:00'
            THEN 'Start+1 Tag, Mitternacht (Handschrift unseres Imports)'
        ELSE 'andere Form'
    END,
    COUNT(*) AS Zeilen
FROM dbo.ResourceFunctionAllocation rfa
WHERE rfa.DateStart IS NOT NULL AND rfa.DateEnd IS NOT NULL
  AND rfa.DaysInAction > DATEDIFF(day, rfa.DateStart, rfa.DateEnd) + 1
  AND rfa.DateStart >= DATEADD(year, -2, GETDATE())
GROUP BY CASE
        WHEN DATEDIFF(day, rfa.DateStart, rfa.DateEnd) = 1
             AND CAST(rfa.DateStart AS time) = '00:00'
            THEN 'Start+1 Tag, Mitternacht (Handschrift unseres Imports)'
        ELSE 'andere Form'
    END
ORDER BY Zeilen DESC
"""

# Und wie sehen die von Hand angelegten Zeilen aus: mehrtägig oder je Tag eine, mit
# welchen Uhrzeiten?
_HANDARBEIT = """
SELECT TOP (8)
    Kalendertage = DATEDIFF(day, rfa.DateStart, rfa.DateEnd) + 1,
    Beginn = CAST(CAST(rfa.DateStart AS time) AS varchar(5)),
    Ende   = CAST(CAST(rfa.DateEnd   AS time) AS varchar(5)),
    COUNT(*) AS Zeilen
FROM dbo.ResourceFunctionAllocation rfa
WHERE rfa.DateStart IS NOT NULL AND rfa.DateEnd IS NOT NULL
  AND rfa.DaysInAction = DATEDIFF(day, rfa.DateStart, rfa.DateEnd) + 1
  AND rfa.DateStart >= DATEADD(year, -2, GETDATE())
GROUP BY DATEDIFF(day, rfa.DateStart, rfa.DateEnd) + 1,
         CAST(CAST(rfa.DateStart AS time) AS varchar(5)),
         CAST(CAST(rfa.DateEnd   AS time) AS varchar(5))
ORDER BY COUNT(*) DESC
"""


def _tabelle(cur, sql, *args) -> None:
    cur.execute(sql, *args) if args else cur.execute(sql)
    spalten = [d[0] for d in cur.description]
    zeilen = cur.fetchall()
    if not zeilen:
        print("    (keine Treffer)")
        return
    breit = [max(len(str(s)), *(len(str(z[i])) for z in zeilen))
             for i, s in enumerate(spalten)]
    print("    " + "  ".join(s.ljust(breit[i]) for i, s in enumerate(spalten)))
    for z in zeilen:
        print("    " + "  ".join(str(z[i]).ljust(breit[i])
                                 for i in range(len(spalten))))


def main() -> int:
    from crew_plan import DEFAULT_HOTEL_ID, DEFAULT_RK_ID, DEFAULT_SPESEN_ID

    conn_str = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if conn_str:
            import pyodbc
            cn = pyodbc.connect(conn_str, timeout=15)
        else:
            cn = _ej_conn()
    except Exception as e:                      # noqa: BLE001
        print("Keine Verbindung:", e)
        print("Erwartet werden EJ_DB_SERVER, EJ_DB_NAME, EJ_DB_UID und EJ_DB_PWD "
              "in .env — dieselben, mit denen sich das Werkzeug anmeldet.")
        return 1

    with cn:
        cur = cn.cursor()
        cur.execute("SELECT @@SERVERNAME, DB_NAME()")
        _srv, _dbn = cur.fetchone()
        print(f"Verbunden mit {_srv} / {_dbn}")

        print("\n1. Wie liegen die Einsätze der letzten zwei Jahre?")
        _tabelle(cur, _VERTEILUNG)

        print("\n2. Dieselbe Funktion mehrfach im selben Job und derselben Spanne")
        print("   (viele Treffer sprechen für eine Zeile je Person)")
        _tabelle(cur, _DOPPELTE)

        print("\n3. Beispiele mit Kopfzahl in einer Zeile")
        _tabelle(cur, _BEISPIELE)

        print("\n3b. Woher die Zeilen mit Kopfzahl stammen")
        _tabelle(cur, _HERKUNFT)

        print("\n3c. Form der von Hand angelegten Zeilen")
        _tabelle(cur, _HANDARBEIT)

        print("\n4. Ressourcen für die Nebenkosten")
        _tabelle(cur, _RESSOURCEN, (DEFAULT_SPESEN_ID, DEFAULT_RK_ID,
                                    DEFAULT_HOTEL_ID))
    return 0


if __name__ == "__main__":
    sys.exit(main())
