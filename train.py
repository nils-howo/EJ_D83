"""
Trainingsdata-Builder: liest Easyjob-Angebotsexporte (Excel) und erzeugt mappings.json.

Jede Excel-Datei enthält:
  - GAEB-Abschnitt-Label  (z.B. "01.01.02 4-point truss 300mm, silver")
  - Artikel-Einträge:      Qty | Artikelart / Bezeichnung (zwei Zeilen pro Artikel)

Ergebnis (mappings.json):
  article_resolutions:  "Artikelart Bezeichnung" → Artikelnummer
  section_articles:     "GAEB-Label"             → [Artikelnummer, ...]
  query_boosts:         beliebiger Schlüssel      → Artikelnummer  (manuell ergänzbar)
"""
import json
import re
import sys
from pathlib import Path

import openpyxl
from rapidfuzz import fuzz, process

MAPPINGS_FILE = Path(__file__).parent / "mappings.json"
ARTIKEL_FILE  = Path(__file__).parent / "infos" / "artikel.json"

# ─── Excel parsen ─────────────────────────────────────────────────────────────

_OZ_RE    = re.compile(r"^\d{2}\.\d{2}\.\d{2}[\s\.]")   # z.B. "01.01.02 ..."
_SEC_RE   = re.compile(r"^\d{2}\.\d{2}\s+\w")            # z.B. "01.01 RIGGING"
_SUMME_RE = re.compile(r"^Summe\s+\d{2}\.\d{2}\.\d{2}")  # z.B. "Summe 01.01.02 ..."


def _is_qty(v) -> bool:
    if v is None:
        return False
    try:
        float(str(v).replace(",", "."))
        return True
    except ValueError:
        return False


def parse_excel(path: str | Path) -> list[dict]:
    """
    Gibt eine Liste von Dicts zurück:
      {
        "section":    "01.01 RIGGING",
        "subsection": "01.01.02 4-point truss 300mm, silver",
        "qty":        204.0,
        "artikelart": "Vierholmtraverse",
        "bezeichnung": "Eurotruss HD34 300cm silber",
      }
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]

    records: list[dict] = []
    cur_section    = ""
    cur_subsection = ""
    pending: dict | None = None   # Artikelart-Zeile wartet auf Bezeichnungs-Zeile

    for row in ws.iter_rows(values_only=True):
        col2 = row[2] if len(row) > 2 else None
        col4 = row[4] if len(row) > 4 else None
        col10 = row[10] if len(row) > 10 else None

        c2 = str(col2).strip() if col2 is not None else ""
        c4 = str(col4).strip() if col4 is not None else ""
        c10 = str(col10).strip() if col10 is not None else ""

        # Haupt-Abschnitt  (z.B. "01.01 RIGGING")
        if _SEC_RE.match(c2) and col4 is None:
            cur_section = c2
            cur_subsection = ""
            pending = None
            continue

        # Unter-Abschnitt  (z.B. "01.01.02 4-point truss 300mm, silver")
        if _OZ_RE.match(c4) and not _is_qty(col2):
            cur_subsection = c4
            pending = None
            continue

        # Summen-Zeile → pending verwerfen
        if _SUMME_RE.match(c10):
            pending = None
            continue

        # Artikel-Zeile (Qty + Artikelart)
        if _is_qty(col2) and c4 and c4 != "\xa0":
            if pending:
                # vorherige Artikelzeile ohne Bezeichnung → trotzdem speichern
                records.append(pending)
            pending = {
                "section":    cur_section,
                "subsection": cur_subsection,
                "qty":        float(str(col2).replace(",", ".")),
                "artikelart": c4,
                "bezeichnung": "",
            }
            continue

        # Bezeichnungs-Zeile (folgt direkt nach Artikel-Zeile)
        if pending is not None and c4 and c4 not in ("\xa0", "Bezeichnung"):
            if not _is_qty(col2) and col2 in (None, "\xa0", ""):
                pending["bezeichnung"] = c4
                records.append(pending)
                pending = None
                continue

        # Sonstige Leerzeilen → pending nur verwerfen wenn neue Section beginnt
        # (wird oben bei _SEC_RE / _OZ_RE schon behandelt)

    if pending:
        records.append(pending)

    return records


# ─── Artikel auflösen ─────────────────────────────────────────────────────────

def _load_artikel_index(path: str | Path) -> tuple[list, list[str]]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw if isinstance(raw, list) else raw.get("items", [])
    corpus = []
    for it in items:
        parts = [
            it.get("Bezeichnung", "") or "",
            it.get("Artikelart", "") or "",
            it.get("Hersteller", "") or "",
        ]
        detail = (it.get("Detailbeschreibung", "") or "")[:80]
        if detail:
            parts.append(detail)
        corpus.append(" ".join(p for p in parts if p).strip())
    return items, corpus


def resolve_to_nummer(search_text: str, items: list, corpus: list[str],
                      threshold: float = 65.0) -> str | None:
    hits = process.extract(search_text, corpus, scorer=fuzz.token_sort_ratio, limit=1)
    if not hits:
        return None
    _, score, idx = hits[0]
    if score >= threshold:
        return items[idx].get("Nummer")
    return None


# ─── Mappings laden / speichern ───────────────────────────────────────────────

def load_mappings() -> dict:
    if MAPPINGS_FILE.exists():
        with open(MAPPINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"article_resolutions": {}, "section_articles": {}, "query_boosts": {}}


def save_mappings(data: dict):
    with open(MAPPINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Gespeichert: {MAPPINGS_FILE}")


# ─── Haupt-Logik ──────────────────────────────────────────────────────────────

def train_from_excel(excel_path: str | Path, verbose: bool = True):
    path = Path(excel_path)
    print(f"\nLese: {path.name}")

    records = parse_excel(path)
    print(f"  {len(records)} Artikel-Einträge gefunden")

    items, corpus = _load_artikel_index(ARTIKEL_FILE)
    print(f"  {len(items)} Easyjob-Artikel als Suchreferenz")

    mappings = load_mappings()
    ar = mappings["article_resolutions"]
    sa = mappings["section_articles"]

    new_res   = 0
    no_match  = 0
    already   = 0

    for rec in records:
        key = f"{rec['artikelart']} {rec['bezeichnung']}".strip()
        if not key:
            continue

        if key in ar:
            already += 1
            nummer = ar[key]
        else:
            nummer = resolve_to_nummer(key, items, corpus)
            if nummer:
                ar[key] = nummer
                new_res += 1
                if verbose:
                    print(f"  + [{nummer}] <- '{key}'")
            else:
                no_match += 1
                if verbose:
                    print(f"  ? kein Match für '{key}'")

        if nummer:
            sub = rec["subsection"]
            if sub:
                if sub not in sa:
                    sa[sub] = []
                if nummer not in sa[sub]:
                    sa[sub].append(nummer)

    save_mappings(mappings)
    print(f"\nErgebnis: {new_res} neu aufgelöst, {already} bereits bekannt, {no_match} ohne Match")
    return mappings


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    paths = sys.argv[1:] or list(Path("infos").glob("*.xlsx"))
    if not paths:
        print("Keine Excel-Dateien gefunden.")
        sys.exit(1)
    for p in paths:
        train_from_excel(p)
