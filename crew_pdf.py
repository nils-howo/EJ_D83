"""Die Personalplanung als PDF — die Beilage, die bei fast jeder Ausschreibung mitgeht.

Zwei Varianten aus denselben Daten:

* **Kundenversion** — nur die Besetzung. Keine Sätze, keine Beträge.
* **Kalkulation** — zusätzlich Tagessatz, Spesen, Hotel, Reisen und Zeilensumme.

Gebaut mit ``reportlab``: ein reines Python-Wheel, das im ``python:3.12-slim`` ohne
zusätzliche Systempakete läuft. WeasyPrint bräuchte dort Pango und Cairo im Image.

Querformat, weil die Zeitachse die lange Kante braucht. Passen die Tage nicht auf eine
Seite, werden sie auf mehrere verteilt — die Namensspalte steht auf jeder wieder
davor, sonst wäre die zweite Seite eine Zahlenwüste ohne Zeilenbeschriftung.

Der Briefbogen liegt als A4 hoch vor (``infos/mld_Briefbogen_*.jpg``, 150 dpi). Statt
ihn quer zu zerren, werden die beiden Bänder daraus ausgeschnitten — der schwarze
Logo-Block oben rechts und das gelbe Adressband unten — und in ihrer **echten
Druckgröße** platziert. Das Logo ist damit auf dem Querformat genauso groß wie auf
einem gewöhnlichen Brief.
"""
from __future__ import annotations

import io
import os
from datetime import date, datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as _canvas

from crew_plan import CrewPlan, CrewRow, parse_day

# ── Briefbogen ───────────────────────────────────────────────────────────────
# Die Ausschnitte sind am Bild ausgemessen (siehe Modul-Docstring). 150 dpi heißt:
# ein Bildpunkt ist 72/150 Punkt groß — damit stimmt die Druckgröße.
_BOGEN = os.path.join(os.path.dirname(__file__), "infos",
                      "mld_Briefbogen_neueAdresse_Ansicht 150dpi.jpg")
_DPI_SCALE = 72.0 / 150.0
_KOPF_BOX = (937, 0, 1240, 168)        # schwarzer Block mit Logo, oben rechts
_FUSS_BOX = (99, 1654, 1240, 1754)     # gelbes Adressband, unten rechts

GELB = colors.HexColor("#ffdc00")
SCHWARZ = colors.HexColor("#000000")
GRAU = colors.HexColor("#f2f4f7")
LINIE = colors.HexColor("#c9d1da")
TEXT = colors.HexColor("#1a1a1a")
BLASS = colors.HexColor("#6b7784")

# Farben der Phasen — dieselbe Reihenfolge wie in der Matrix.
PHASENFARBEN = ["#1565c0", "#00897b", "#e65100", "#6a1b9a",
                "#2e7d32", "#c62828", "#455a64"]

_WOCHENTAG = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_MONAT = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember")


def _eur(v: float) -> str:
    return f"{v:,.0f}".replace(",", ".") + " €"


def _kurz(text: str, c: _canvas.Canvas, font: str, groesse: float,
          breite: float) -> str:
    """Text auf die Spaltenbreite kürzen, mit … am Ende."""
    if c.stringWidth(text, font, groesse) <= breite:
        return text
    while text and c.stringWidth(text + "…", font, groesse) > breite:
        text = text[:-1]
    return text + "…"


class _Bogen:
    """Die beiden Bänder des Briefbogens, einmal geladen."""

    def __init__(self, pfad: str = _BOGEN):
        self.kopf = self.fuss = None
        if not os.path.exists(pfad):
            return
        try:
            from PIL import Image
        except ImportError:                 # ohne Pillow eben ohne Briefbogen
            return
        try:
            bild = Image.open(pfad).convert("RGB")
            self.kopf = ImageReader(bild.crop(_KOPF_BOX))
            self.fuss = ImageReader(bild.crop(_FUSS_BOX))
        except Exception:                   # noqa: BLE001 — ein fehlender Bogen darf
            self.kopf = self.fuss = None    # den Export nicht verhindern

    def zeichne(self, c: _canvas.Canvas, breite: float, hoehe: float) -> None:
        if self.kopf is not None:
            x1, y1, x2, y2 = _KOPF_BOX
            w, h = (x2 - x1) * _DPI_SCALE, (y2 - y1) * _DPI_SCALE
            c.drawImage(self.kopf, breite - w, hoehe - h, w, h, mask="auto")
        if self.fuss is not None:
            x1, y1, x2, y2 = _FUSS_BOX
            w, h = (x2 - x1) * _DPI_SCALE, (y2 - y1) * _DPI_SCALE
            c.drawImage(self.fuss, breite - w, 0, w, h, mask="auto")


def _tage(plan: CrewPlan) -> list[dict]:
    out = []
    for k in plan.day_keys():
        d = parse_day(k)
        out.append({"key": k, "d": d, "tag": d.day,
                    "wt": _WOCHENTAG[d.weekday()],
                    "we": d.weekday() >= 5,
                    "monat": d.month, "jahr": d.year})
    return out


def _phase_von(plan: CrewPlan, key: str) -> int:
    for i, ph in enumerate(plan.phases):
        if ph.contains(key):
            return i
    return -1


def _zeilen(plan: CrewPlan) -> list[tuple[str, list[CrewRow]]]:
    """Abschnitte mit ihren Zeilen, in der Reihenfolge der Matrix.

    Zeilen ohne einen einzigen Manntag fallen weg, und ein dadurch leerer Abschnitt
    gleich mit. In der Matrix sind sie nützlich — dort stehen sie als Vorschlag aus
    dem Matching, bis jemand entscheidet. Auf dem Blatt sind sie nur eine Liste von
    Leuten, die nicht kommen.
    """
    out = []
    for key, rows in plan.groups():
        gefuellt = [r for r in rows if plan.manntage(r) > 0]
        if gefuellt:
            out.append((key, gefuellt))
    return out


def build_pdf(plan: CrewPlan, projekt: str = "", *, kalkulation: bool = True,
              titel_zusatz: str = "", pos_titel: dict[str, str] | None = None,
              menu_titel: dict[str, str] | None = None) -> bytes:
    """Die Planung als PDF-Bytes.

    ``pos_titel`` bildet item_id → Beschriftung ab (für die Positionsspalte),
    ``menu_titel`` Abschnitts-Schlüssel → Überschrift. Beides kommt aus dem LV und
    ist dem Planungsmodell selbst nicht bekannt.
    """
    pos_titel = pos_titel or {}
    menu_titel = menu_titel or {}
    puffer = io.BytesIO()
    breite, hoehe = landscape(A4)
    c = _canvas.Canvas(puffer, pagesize=(breite, hoehe))
    c.setTitle(f"Personalplanung {projekt}".strip())
    c.setAuthor("music & light design GmbH")
    bogen = _Bogen()

    rand_l, rand_r = 30.0, 30.0
    kopf_h = (_KOPF_BOX[3] - _KOPF_BOX[1]) * _DPI_SCALE
    fuss_h = (_FUSS_BOX[3] - _FUSS_BOX[1]) * _DPI_SCALE
    oben = hoehe - kopf_h - 16
    unten = fuss_h + 16

    tage = _tage(plan)
    gruppen = _zeilen(plan)

    # Spaltenmaße. Die Kostenspalten entfallen in der Kundenversion — dort ist mehr
    # Platz für die Tage, was bei langen Projekten den Umbruch spart.
    name_w = 132.0
    kosten = ([("MT", 26.0), ("TS", 42.0), ("Spesen", 44.0), ("Hotel", 44.0),
               ("Reisen", 44.0), ("Summe", 56.0)] if kalkulation else [])
    kosten_w = sum(w for _, w in kosten)
    frei = breite - rand_l - rand_r - name_w - kosten_w
    if not tage:
        tag_w, pro_seite = 16.0, 1
    else:
        # Erst zählen, wie viele Tage bei der schmalsten vertretbaren Spalte auf eine
        # Seite gehen, dann die Spalten auf den vorhandenen Platz aufziehen. Andersherum
        # — Breite aus der Tageszahl, Tage aus der Breite — verliert durch
        # Rundungsfehler den letzten Tag und bricht auf eine zweite Seite um.
        # Untergrenze 11 pt: darunter wird eine zweistellige Zahl unleserlich. Ein
        # Monat passt damit auf eine Seite — das ist der Regelfall, und ein Umbruch
        # mitten in der Veranstaltung wäre dafür ein schlechter Tausch.
        pro_seite = max(1, min(len(tage), int(frei // 11.0)))
        tag_w = max(11.0, min(24.0, frei / pro_seite))

    bloecke = [tage[i:i + pro_seite] for i in range(0, len(tage), pro_seite)] or [[]]
    seiten = len(bloecke)

    for nr, block in enumerate(bloecke, start=1):
        _seite(c, plan, projekt, block, gruppen, bogen,
               breite=breite, hoehe=hoehe, oben=oben, unten=unten,
               rand_l=rand_l, name_w=name_w, tag_w=tag_w, kosten=kosten,
               kalkulation=kalkulation, seite=nr, seiten=seiten,
               titel_zusatz=titel_zusatz, pos_titel=pos_titel,
               menu_titel=menu_titel)
        c.showPage()

    c.save()
    return puffer.getvalue()


def _seite(c: _canvas.Canvas, plan: CrewPlan, projekt: str, block: list[dict],
           gruppen, bogen: _Bogen, *, breite, hoehe, oben, unten, rand_l,
           name_w, tag_w, kosten, kalkulation, seite, seiten, titel_zusatz,
           pos_titel, menu_titel) -> None:
    bogen.zeichne(c, breite, hoehe)

    # ── Titelzeile ───────────────────────────────────────────────────────────
    y = oben
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(rand_l, y, "Personalplanung")
    c.setFont("Helvetica", 9.5)
    c.setFillColor(BLASS)
    rechts = breite - 30
    zeitraum = ""
    if plan.date_from and plan.date_to:
        a, b = parse_day(plan.date_from), parse_day(plan.date_to)
        zeitraum = (f"{a.day}. {_MONAT[a.month - 1]} – "
                    f"{b.day}. {_MONAT[b.month - 1]} {b.year}")
    fuss = zeitraum
    if seiten > 1:
        fuss += f"   ·   Seite {seite} von {seiten}"
    c.drawRightString(rechts, y, fuss)

    y -= 15
    c.setFont("Helvetica", 10.5)
    c.setFillColor(TEXT)
    kopfzeile = projekt or "Ohne Projektnamen"
    if titel_zusatz:
        kopfzeile += f" · {titel_zusatz}"
    c.drawString(rand_l, y, _kurz(kopfzeile, c, "Helvetica", 10.5, breite - 340))
    if not kalkulation:
        c.setFont("Helvetica", 8)
        c.setFillColor(BLASS)
        c.drawRightString(rechts, y, "Besetzungsübersicht")

    y -= 12
    c.setStrokeColor(GELB)
    c.setLineWidth(2)
    c.line(rand_l, y, breite - 30, y)
    y -= 4

    if not block:
        c.setFont("Helvetica", 9)
        c.setFillColor(BLASS)
        c.drawString(rand_l, y - 20, "Kein Zeitraum festgelegt.")
        return

    # ── Spaltenpositionen ────────────────────────────────────────────────────
    x_name = rand_l
    x_tage = x_name + name_w
    x_kosten = x_tage + len(block) * tag_w
    zeilen_h = 14.0

    # ── Kopf: Monat, Phase, Tag ──────────────────────────────────────────────
    y_monat = y - 12
    c.setFont("Helvetica-Bold", 7)
    lauf = 0
    while lauf < len(block):
        m, j = block[lauf]["monat"], block[lauf]["jahr"]
        span = 0
        while (lauf + span < len(block) and block[lauf + span]["monat"] == m
               and block[lauf + span]["jahr"] == j):
            span += 1
        c.setFillColor(BLASS)
        c.drawString(x_tage + lauf * tag_w + 2, y_monat + 3,
                     f"{_MONAT[m - 1]} {j}")
        lauf += span

    y_phase = y_monat - 11
    lauf = 0
    while lauf < len(block):
        pi = _phase_von(plan, block[lauf]["key"])
        span = 0
        while (lauf + span < len(block)
               and _phase_von(plan, block[lauf + span]["key"]) == pi):
            span += 1
        if pi >= 0:
            farbe = colors.HexColor(PHASENFARBEN[pi % len(PHASENFARBEN)])
            c.setFillColor(farbe)
            c.rect(x_tage + lauf * tag_w, y_phase, span * tag_w, 10,
                   stroke=0, fill=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 6)
            name = _kurz(plan.phases[pi].name.upper(), c, "Helvetica-Bold", 6,
                         span * tag_w - 4)
            c.drawCentredString(x_tage + (lauf + span / 2) * tag_w,
                                y_phase + 2.8, name)
        lauf += span

    y_tag = y_phase - 13
    for i, d in enumerate(block):
        x = x_tage + i * tag_w
        if d["we"]:
            c.setFillColor(GRAU)
            c.rect(x, y_tag - 1, tag_w, 13, stroke=0, fill=1)
        c.setFillColor(BLASS if d["we"] else TEXT)
        c.setFont("Helvetica", 5.6)
        c.drawCentredString(x + tag_w / 2, y_tag + 7.2, d["wt"])
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(x + tag_w / 2, y_tag + 0.6, str(d["tag"]))

    if kalkulation:
        c.setFont("Helvetica", 6)
        c.setFillColor(BLASS)
        xk = x_kosten
        for name, w in kosten:
            c.drawRightString(xk + w - 3, y_tag + 0.6, name.upper())
            xk += w

    y_kopf_unten = y_tag - 3
    c.setStrokeColor(LINIE)
    c.setLineWidth(0.7)
    c.line(x_name, y_kopf_unten, x_kosten + sum(w for _, w in kosten),
           y_kopf_unten)

    # ── Zeilen ───────────────────────────────────────────────────────────────
    y = y_kopf_unten
    schluss = unten + 26
    for menu_key, rows in gruppen:
        if y - zeilen_h < schluss:
            break
        titel = menu_titel.get(menu_key) or plan.menu_title(menu_key) or "Ohne Abschnitt"
        y -= zeilen_h
        c.setFillColor(colors.HexColor("#eef1f6"))
        c.rect(x_name, y, x_kosten + sum(w for _, w in kosten) - x_name,
               zeilen_h, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#37474f"))
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(x_name + 3, y + 4, _kurz(titel.upper(), c,
                                              "Helvetica-Bold", 7.5, name_w * 2))

        for r in rows:
            if y - zeilen_h < schluss:
                break
            y -= zeilen_h
            c.setStrokeColor(colors.HexColor("#eceff1"))
            c.setLineWidth(0.4)
            c.line(x_name, y, x_kosten + sum(w for _, w in kosten), y)

            c.setFillColor(TEXT)
            c.setFont("Helvetica", 7.5)
            c.drawString(x_name + 3, y + 4,
                         _kurz(r.label, c, "Helvetica", 7.5, name_w - 8))

            for i, d in enumerate(block):
                x = x_tage + i * tag_w
                if d["we"]:
                    c.setFillColor(GRAU)
                    c.rect(x, y, tag_w, zeilen_h, stroke=0, fill=1)
                n = r.cells.get(d["key"])
                if not n:
                    continue
                c.setFillColor(GELB)
                c.rect(x + 1, y + 1.5, tag_w - 2, zeilen_h - 3, stroke=0, fill=1)
                c.setFillColor(TEXT)
                c.setFont("Helvetica-Bold", 7.5)
                c.drawCentredString(x + tag_w / 2, y + 4, str(n))

            if kalkulation:
                mt = plan.manntage(r)
                werte = [str(mt), _eur(r.tagessatz), _eur(plan.row_spesen(r)),
                         _eur(plan.row_hotel(r)), _eur(plan.row_rk(r)),
                         _eur(plan.row_total(r))]
                xk = x_kosten
                c.setFillColor(TEXT)
                for (name, w), wert in zip(kosten, werte):
                    c.setFont("Helvetica-Bold" if name == "Summe" else "Helvetica", 7)
                    c.drawRightString(xk + w - 3, y + 4, wert)
                    xk += w

    # ── Summenzeile ──────────────────────────────────────────────────────────
    y -= zeilen_h
    c.setFillColor(colors.HexColor("#f7f8fc"))
    c.rect(x_name, y, x_kosten + sum(w for _, w in kosten) - x_name, zeilen_h,
           stroke=0, fill=1)
    c.setStrokeColor(LINIE)
    c.setLineWidth(0.8)
    c.line(x_name, y + zeilen_h, x_kosten + sum(w for _, w in kosten),
           y + zeilen_h)
    c.setFillColor(colors.HexColor("#37474f"))
    c.setFont("Helvetica-Bold", 7)
    c.drawString(x_name + 3, y + 4, "PERSONEN / TAG")
    for i, d in enumerate(block):
        n = plan.day_total(d["key"])
        if not n:
            continue
        c.drawCentredString(x_tage + i * tag_w + tag_w / 2, y + 4, str(n))

    if kalkulation:
        t = plan.totals()
        werte = [str(t["manntage"]), "", _eur(t["spesen"]), _eur(t["hotel"]),
                 _eur(t["rk"]), _eur(t["summe"])]
        xk = x_kosten
        for (name, w), wert in zip(kosten, werte):
            c.setFont("Helvetica-Bold", 7)
            c.drawRightString(xk + w - 3, y + 4, wert)
            xk += w

    # ── Fußnote ──────────────────────────────────────────────────────────────
    c.setFont("Helvetica", 6.5)
    c.setFillColor(BLASS)
    stand = datetime.now().strftime("%d.%m.%Y")
    hinweis = f"Stand {stand}"
    if not kalkulation:
        hinweis += "   ·   Besetzung ohne Preise"
    c.drawString(rand_l, unten - 10, hinweis)
