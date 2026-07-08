"""Article + Resource matching: GAEB positions → Easyjob articles / Personal / Transport."""
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from rapidfuzz import fuzz, process

# ── Englisch → Deutsch Übersetzungstabelle für Event-Tech-Begriffe ────────────
# Längste Phrases zuerst (verhindert Teilmatches bei "chain hoist" vor "hoist")
EN_DE_EVENT_TECH: list[tuple[str, str]] = [
    # ── Herstellerspezifische Traversen-Codes ─────────────────────────────────
    # Prolyte: H=Heavy-duty, S=Super, X=leichter | Größe: 30→290mm, 36→360mm, 40→400mm
    # Typ: V=Viereck(4-Holm), D=Dreieck(3-Holm), L=Leiter/Ladder(2-Holm=HB-Rohr)
    ("prolyte h40v",       "vierholmtraverse hd44"),
    ("prolyte h30v",       "vierholmtraverse hd34"),
    ("prolyte h30d",       "dreiholmtraverse hd33"),
    ("prolyte s36v",       "vierholmtraverse hd44"),
    ("prolyte x40v",       "vierholmtraverse hd44"),
    ("prolyte x30v",       "vierholmtraverse hd34"),
    ("prolyte x30d",       "dreiholmtraverse hd33"),
    ("prolyte h30l",       "hb-rohr"),
    # Prolyte Kurzform (ohne Markenname — häufig in GAEB-Positionen)
    ("h40v",               "vierholmtraverse hd44"),
    ("h30v",               "vierholmtraverse hd34"),
    ("h30d",               "dreiholmtraverse hd33"),
    ("s36v",               "vierholmtraverse hd44"),
    ("x30d",               "dreiholmtraverse hd33"),
    ("h30l",               "hb-rohr"),
    # Global Truss: F[Größenziffer][Holmanzahl] — 3→290mm, 4→400mm
    ("global truss f44",   "vierholmtraverse hd44"),
    ("global truss f34",   "vierholmtraverse hd34"),
    ("global truss f33",   "dreiholmtraverse hd33"),
    ("global truss f32",   "hb-rohr"),
    ("global f44",         "vierholmtraverse hd44"),
    ("global f34",         "vierholmtraverse hd34"),
    ("global f33",         "dreiholmtraverse hd33"),
    ("global f32",         "hb-rohr"),
    # Global Truss Kurzform
    ("f44",                "vierholmtraverse hd44"),
    ("f34",                "vierholmtraverse hd34"),
    ("f33",                "dreiholmtraverse hd33"),
    ("f32",                "hb-rohr"),
    # ── Allgemeine Traversen-Typen ────────────────────────────────────────────
    ("electric chain hoist", "elektrokettenzug motorkettenzug"),
    ("chain hoist",          "kettenzug motorkettenzug"),
    ("4-point truss",        "vierholmtraverse"),
    ("3-point truss",        "dreiholmtraverse"),
    ("2-point truss",        "hb-rohr"),
    ("box truss",            "kastentraverse vierholmtraverse"),
    ("ladder truss",         "hb-rohr leitertraverse"),
    ("t-corner",             "t-stück 3-weg ecke"),
    ("t-piece",              "t-stück 3-weg"),
    ("3-way",                "3-weg"),
    ("truss",                "traverse"),
    ("hoist",                "kettenzug hebezeug"),
    ("clamp",                "klemme"),
    ("shackle",              "schäkel"),
    ("rigging plate",        "hängepunkt"),
    ("follow spot",          "followspot verfolger"),
    ("moving light",         "moving head scheinwerfer"),
    ("moving head",          "moving head"),
    ("wash light",           "wash scheinwerfer"),
    ("beam light",           "beam scheinwerfer"),
    ("spotlight",            "scheinwerfer"),
    ("dimmer",               "dimmer"),
    ("led wall",             "videowand led videowand"),
    ("led screen",           "videowand led videowand"),
    ("led panel",            "led panel videomodul"),
    ("led module",           "led modul"),
    ("pixel pitch",          "pixel pitch"),
    ("media server",         "medienserver"),
    ("projector",            "projektor beamer"),
    # Kontext-spezifische Screen-Übersetzungen VOR dem generischen "screen"
    ("lcd screen",           "lcd display bildschirm"),
    ("uhd screen",           "uhd display bildschirm"),
    ("touch screen",         "touchscreen display bildschirm"),
    ("screen lcd",           "display bildschirm lcd"),
    ("screen uhd",           "display bildschirm uhd"),
    ("touchscreen",          "touchscreen display"),
    ("monitor",              "display bildschirm monitor"),
    # Generisch: "screen" = Projektionsleinwand ODER Display
    ("screen",               "leinwand bildwand display bildschirm"),
    ("loudspeaker",          "lautsprecher"),
    ("subwoofer",            "subwoofer bass"),
    ("amplifier",            "endstufe verstärker"),
    ("mixing console",       "mischpult"),
    ("microphone",           "mikrofon"),
    ("wireless",             "funk drahtlos"),
    ("speaker",              "lautsprecher"),
    ("mixer",                "mischpult"),
    ("media player",         "player"),   # "MediaPlayer" → "Player" (Mini-PC-Zuspieler)
    ("mediaplayer",          "player"),
    ("distribution",         "verteiler"),
    ("cabling",              "verkabelung"),
    ("laptops",              "notebooks"),  # Plural vor Singular (verhindert Teilmatch)
    ("laptop",               "notebook"),
    ("pipe",                 "rohr"),
    ("power",                "strom energie"),
    ("silver",               "silber"),
    ("black",                "schwarz"),
    ("white",                "weiß"),
    ("technician",           "techniker"),
    ("electrician",          "elektriker"),
    ("truck",                "lkw transporter"),
]


def translate_en_de(text: str) -> str:
    """Ersetzt englische Event-Tech-Begriffe durch deutsche Entsprechungen.
    Nutzt Wortgrenzen um Kaskadenersetzungen zu verhindern (z.B. 'screen' in 'touchscreen').
    """
    result = text
    for en_phrase, de_equiv in EN_DE_EVENT_TECH:
        pattern = r'\b' + re.escape(en_phrase) + r'\b'
        result = re.sub(pattern, de_equiv, result, flags=re.IGNORECASE)
    return result


def auto_learn_bundle(gaeb_description: str, numbers: list[str],
                      mappings_path: Path | None = None) -> None:
    """Speichert Bundle-Zustand in der DB. Leere Liste = Mapping löschen.

    mappings_path wird ignoriert (bleibt aus Kompatibilitätsgründen erhalten).
    """
    if not gaeb_description.strip():
        return
    from db import save_gui_bundle
    save_gui_bundle(gaeb_description, numbers)

# LED-Wand: "LED" + Maßangabe in mm (z.B. "LED Panel 500x500mm", "500x500mm LED Wand")
_LED_WALL_RE = re.compile(
    r'(led.{0,40}\d+\s*[xX×]\s*\d+\s*mm|\d+\s*[xX×]\s*\d+\s*mm.{0,40}led)',
    re.IGNORECASE,
)
# LED-Wand: Pixel-Pitch in mm (z.B. "2.6mm", "3,9mm") → eindeutiges Signal für LED-Wand-Modul
_PIXEL_PITCH_RE = re.compile(r'\b\d+[\.,]\d+\s*mm\b', re.IGNORECASE)

# ── Traverse-Erkennung ────────────────────────────────────────────────────────
_TRAVERSE_WORD_RE  = re.compile(
    r'traverse|truss|hb-rohr'
    r'|t[-\s]corner|t[-\s]ecke|3[-\s]weg|3[-\s]way|t[-\s]st[üu]ck'
    r'|\bHD[34][1-4]\b'                          # Eurotruss direkt: HD34, HD33 etc.
    r'|\b[HSXB]-?\d{2,3}-?[VDL]\b'              # Prolyte: H30V, S36V, X30D etc.
    r'|\bF[345][234]\b',                          # Global Truss: F34, F44 etc.
    re.IGNORECASE,
)
# Direkte Eurotruss HD-Nummer erkennen (HDxy → x=Größe, y=Holme)
_HDXY_RE = re.compile(r'\bHD\s*([34])([1-4])\b', re.IGNORECASE)

# ── Hersteller-übergreifende Traversen-Codes ──────────────────────────────────
# Prolyte: [HSXB][Größe][Typ] z.B. H30V, S36V, X30D, H30L
_PROLYTE_RE = re.compile(
    r'\b[HSXB]\s*-?\s*(30|36|40|52|66)\s*-?\s*([VDLvdl])\b',
    re.IGNORECASE,
)
# Global Truss: F[Größenziffer][Holmzahl] z.B. F34, F33, F32, F44
_GLOBAL_TRUSS_RE = re.compile(r'\bF\s*([345])([234])\b', re.IGNORECASE)

_PROLYTE_SIZE_TO_MM: dict[int, int] = {30: 290, 36: 360, 40: 400, 52: 520}
_PROLYTE_TYPE_TO_POINTS: dict[str, int] = {'v': 4, 'd': 3, 'l': 2}
_GLOBAL_SIZE_TO_MM: dict[int, int] = {3: 290, 4: 400, 5: 520}
_TRAVERSE_POINT_RE = re.compile(
    r'(\d)\s*[-–]?\s*[Pp]unkt'       # 4-Punkt (DE)
    r'|(\d)\s*[Hh]olm'               # 4-Holm (DE)
    r'|(\d)\s*[-–]?\s*[Pp]oint'      # 4-point (EN)
    r'|(\d)\s*[-–]?\s*[Cc]hord',     # 4-chord (EN)
    re.IGNORECASE,
)
_TRAVERSE_SIZE_RE  = re.compile(r'(\d{2,3})\s*mm', re.IGNORECASE)
_TRAVERSE_LEN_RE   = re.compile(
    r'[Ll][\xE4a]nge\s*(\d+[,.]?\d*)\s*m\b'   # Länge 3m
    r'|(\d+[,.]?\d*)\s*m\b',                    # 3m  (ohne "cm" davor)
    re.IGNORECASE,
)
_TRAVERSE_CM_RE    = re.compile(r'(?<!\d)(\d{2,3})\s*cm\b', re.IGNORECASE)  # 300cm → 3.0m
_TRAVERSE_COLOR_RE = re.compile(r'schwarz|silber|black|silver', re.IGNORECASE)
# HB-Rohr / Zweiholm-Erkennung direkt aus dem Bezeichnungstext
_HB_ROHR_RE = re.compile(
    r'\bhb[-\s]?rohr\b|\bzweiholm\b|\bleitertraverse\b|\bladder\s*truss\b',
    re.IGNORECASE,
)
# Englische Farben → Deutsche Entsprechungen für Artikel-Matching
_COLOR_NORMALIZE = {"black": "schwarz", "silver": "silber"}
# T-Corner / T-Stück / 3-Weg Erkennung
_TCORNER_RE = re.compile(
    r't[\s-]corner|t[\s-]ecke|t[\s-]st[üu]ck|3[\s-]weg|3[\s-]way|t[\s-]piece',
    re.IGNORECASE,
)
# Metallrohre (Alu/Stahl/Kupfer) → kein Traverse-Scoring; das ist Rohrleitungsmaterial
_IS_PIPE_RE = re.compile(
    r'\b(alu(?:minium)?|stahl|steel|kupfer|copper|iron|eisen)\s*[-\s]*(rohr|pipe|tube|rohre)\b',
    re.IGNORECASE,
)
# Generische Rohr-Anfrage (ohne Metallspezifizierung, z.B. "pipe d48mm")
_GENERIC_PIPE_RE = re.compile(
    r'\b(pipe|tube|rohr)\b.*\bd\d{2,3}\b|\bd\d{2,3}\b.*\b(pipe|tube|rohr)\b',
    re.IGNORECASE,
)

# Boxcorner-Anfrage: Query sucht explizit ein Eck-/Verbindungselement
_BOXCORNER_RE = re.compile(
    r'\bbox[\s-]?corner\b|\beckelement\b|\bcorner[\s-]?block\b|\bkonnektor[\s-]?ecke\b',
    re.IGNORECASE,
)

# Touch-Display-Anfrage: Query ist eindeutig ein Display-Gerät
_DISPLAY_QUERY_RE = re.compile(
    r'\b(touchscreen|touch[\s-]?display|touchmonitor)\b',
    re.IGNORECASE,
)
# Generische Display-Wörter in der ANFRAGE (nicht nur im Kategoriepfad)
_DISPLAY_WORD_RE = re.compile(
    r'\b(screen|display|monitor|lcd|uhd|bildschirm|kiosk)\b',
    re.IGNORECASE,
)
# Display-Größe in Zoll (z.B. 55", 65 Zoll, 43")
_DISPLAY_INCH_RE = re.compile(
    r'\b(\d{2,3})\s*(?:"|\'|zoll|inch)\b',
    re.IGNORECASE,
)
# Zoll aus Artikel-Bezeichnung (auch ohne Anführungszeichen aber mit typischen Zollzahlen)
_ART_INCH_RE = re.compile(
    r'\b(\d{2,3})["\']',
)
# "Touch"-Anforderung in Display-Query
_TOUCH_RE = re.compile(r'\btouch\b|\btouchscreen\b|\bpcap\b|\biR-touch\b', re.IGNORECASE)


def _parse_display_inch(text: str) -> Optional[int]:
    """Parse inch from query text; only 24–110" (to avoid matching voltages etc.)."""
    m = _DISPLAY_INCH_RE.search(text) or _ART_INCH_RE.search(text)
    if m:
        val = int(m.group(1))
        if 24 <= val <= 110:
            return val
    return None


def _parse_art_inch(text: str) -> Optional[int]:
    """Parse inch from article Bezeichnung; min 10" so 12/15" displays are captured."""
    m = _ART_INCH_RE.search(text)
    if m:
        val = int(m.group(1))
        if 10 <= val <= 200:
            return val
    return None

# ── Motor / Hebezeug ──────────────────────────────────────────────────────────
_MOTOR_DETECT_RE = re.compile(
    r'\bmotor(?:en)?\b'
    r'|\bkettenzug(?:e)?\b|\bkettenz[üu]ge\b'
    r'|\bhebezeug(?:e)?\b'
    r'|\bchain[\s-]?hoist\b'
    r'|\belektro[\s-]?ketten[\s-]?zug\b'
    r'|\bliftmotor\b|\bhoist\b',
    re.IGNORECASE,
)
_MOTOR_CAPACITY_RE = re.compile(
    r'(\d+[,.]?\d*)\s*(t\b|to\b|ton(?:ne)?\b|kg\b|kn\b)',
    re.IGNORECASE,
)
_MOTOR_HUB_RE = re.compile(r'(\d+)\s*m\s+hub\b', re.IGNORECASE)
_MOTOR_NAME_CAP_RE = re.compile(r'\b0*(\d{3,4})PLUS', re.IGNORECASE)


def parse_motor_capacity_kg(description: str) -> Optional[int]:
    """Tragkraft in kg aus GAEB-Beschreibung. None wenn nicht erkennbar."""
    best: Optional[int] = None
    for m in _MOTOR_CAPACITY_RE.finditer(description):
        val = float(m.group(1).replace(",", "."))
        unit = m.group(2).lower().rstrip(".")
        kg = int(val * 1000) if unit in ("t", "to", "ton", "tonne") \
             else int(val * 102) if unit == "kn" \
             else int(val)
        if kg >= 50:  # Kleinere Zahlen sind Hub-Länge, nicht Tragkraft
            if best is None or kg > best:
                best = kg
    return best


def motor_art_capacity_kg(bezeichnung: str) -> Optional[int]:
    """Tragkraft aus Movecat-Artikelname: '0160PLUS' → 160."""
    m = _MOTOR_NAME_CAP_RE.search(bezeichnung)
    return int(m.group(1)) if m else None


def motor_art_hub_m(bezeichnung: str) -> Optional[int]:
    """Hub-Länge aus Artikelname: '18m Hub' → 18."""
    m = _MOTOR_HUB_RE.search(bezeichnung)
    return int(m.group(1)) if m else None


def is_motor_position(description: str) -> bool:
    """Erkennt GAEB-Positionen die Hebezeuge/Motoren anfragen."""
    return bool(_MOTOR_DETECT_RE.search(description))

# Eurotruss HD-Nomenklatur: HDxy → x=Größenklasse, y=Holmzahl
# Größe in mm → Eurotruss-Größenziffer
_SIZE_TO_HD = {290: "3", 300: "3", 360: "4", 390: "4", 400: "4"}  # 360=Prolyte S36V → nächste HD-Klasse HD44
# Holmzahl → Eurotruss HD-Serienziffer (3- und 4-Holm)
# 2-Holm = HB-Rohr (kein HD-Prefix in Artikeldatenbank)
_POINTS_TO_HD = {1: "1", 3: "3", 4: "4"}

# Standard-Stücklänge für Laufmeter-Berechnung
TRAVERSE_STANDARD_LENGTH_M = 3.0
# Laufmeter-Einheiten die eine lfm→Stück-Umrechnung auslösen
TRAVERSE_LFM_UNITS = {"lfm", "m", "lm", "lfm.", "lm.", "rm", "rm."}


@dataclass
class TraverseInfo:
    points: Optional[int]       # Holmzahl (1-4)
    size_mm: Optional[int]      # Querschnitt in mm
    length_m: Optional[float]   # Stücklänge in m (falls angegeben)
    color: Optional[str]        # "schwarz" / "silber" / None
    hd_series: str = field(init=False)  # z.B. "HD34"

    def __post_init__(self):
        if self.points == 2:
            # 2-Holm = HB-Rohr (kein Eurotruss HD-Artikel, eigenes Produkt)
            self.hd_series = "HB"
        else:
            size_d = _SIZE_TO_HD.get(self.size_mm or 0, "3")
            pts_d  = _POINTS_TO_HD.get(self.points or 0, "")
            self.hd_series = f"HD{size_d}{pts_d}" if pts_d else f"HD{size_d}"

    @property
    def is_hb_rohr(self) -> bool:
        return self.points == 2

    @property
    def search_query(self) -> str:
        if self.is_hb_rohr:
            parts = ["HB-Rohr"]
        else:
            parts = [self.hd_series]
        if self.color:
            parts.append(self.color)
        if self.length_m:
            parts.append(f"{int(self.length_m * 100)}cm")
        return " ".join(parts)


def parse_traverse_info(description: str) -> Optional[TraverseInfo]:
    """Erkennt Traverse-Beschreibungen und extrahiert Holmzahl, Größe, Länge, Farbe."""
    if not _TRAVERSE_WORD_RE.search(description):
        return None

    # Holmzahl (DE: Punkt/Holm, EN: point/chord)
    m = _TRAVERSE_POINT_RE.search(description)
    points = int(next(g for g in m.groups() if g is not None)) if m else None

    # Größe in mm
    sizes = [int(x) for x in _TRAVERSE_SIZE_RE.findall(description)]
    # nimm Wert der am nächsten an 290-400mm liegt (ignoriert z.B. "4-Punkt")
    size_mm = next((s for s in sizes if 200 <= s <= 500), None)

    # Direkte HD-Nummer (z.B. "HD34" → size=300mm, points=4) als Fallback
    hd_m = _HDXY_RE.search(description)
    if hd_m:
        if size_mm is None:
            size_mm = {3: 300, 4: 400}.get(int(hd_m.group(1)))
        if points is None:
            points = int(hd_m.group(2))

    # Prolyte-Nomenklatur: H30V, H-30D, S36V, X30D etc.
    prolyte_m = _PROLYTE_RE.search(description)
    if prolyte_m:
        size_num = int(prolyte_m.group(1))
        type_char = prolyte_m.group(2).lower()
        if size_mm is None:
            size_mm = _PROLYTE_SIZE_TO_MM.get(size_num)
        if points is None:
            points = _PROLYTE_TYPE_TO_POINTS.get(type_char)

    # Global Truss: F34, F33, F32, F44 etc.
    global_m = _GLOBAL_TRUSS_RE.search(description)
    if global_m:
        size_digit = int(global_m.group(1))
        chord = int(global_m.group(2))
        if size_mm is None:
            size_mm = _GLOBAL_SIZE_TO_MM.get(size_digit)
        if points is None:
            points = chord

    # HB-Rohr direkt am Namen erkennen → 2-Holm auch ohne "2-Punkt"-Angabe
    if _HB_ROHR_RE.search(description) and points is None:
        points = 2

    # Stücklänge: erst in Metern suchen, dann in Zentimetern
    lm = _TRAVERSE_LEN_RE.search(description)
    if lm:
        raw = (lm.group(1) or lm.group(2) or "").replace(",", ".")
        try:
            length_m = float(raw)
        except ValueError:
            length_m = None
    else:
        length_m = None

    # cm-Länge als Fallback (z.B. "300cm" → 3.0m, nicht < 50cm Zubehör)
    if length_m is None:
        cm_lm = _TRAVERSE_CM_RE.search(description)
        if cm_lm:
            cm_val = int(cm_lm.group(1))
            if 50 <= cm_val <= 600:
                length_m = cm_val / 100

    # Farbe
    cm = _TRAVERSE_COLOR_RE.search(description)
    color = cm.group(0).lower() if cm else None

    return TraverseInfo(points=points, size_mm=size_mm,
                        length_m=length_m, color=color)


def traverse_piece_count(total_qty: float, unit: str,
                          piece_length_m: float = TRAVERSE_STANDARD_LENGTH_M) -> Optional[int]:
    """Berechnet Stückzahl aus Laufmetern. Gibt None zurück wenn Einheit kein Laufmeter ist."""
    if unit.lower().strip(".") in {u.strip(".") for u in TRAVERSE_LFM_UNITS}:
        return math.ceil(total_qty / piece_length_m)
    return None

GAEB_SYNONYM_TAG = "[GAEB-Synonyme:"
GAEB_SYNONYM_END = "]"
HIGH_SCORE = 85
LOW_SCORE = 55

# ── Artikel-Filter ────────────────────────────────────────────────────────────
EXCLUDE_WARENGRUPPEN = {"Cases"}
EXCLUDE_PREFIXES = {"ZZZ", "X_", "Y_", "Zubehör"}
EXCLUDE_CONTAINS = {"Netzteil"}          # Substring in Bezeichnung → ausschließen

# Warengruppen mit niedrigerer Priorität (Score-Abzug)
PENALIZE_WARENGRUPPEN = {"Kabel + Zubehör", "Kabel"}
WARENGRUPPE_PENALTY = 18                 # Punkte Abzug

# ── Kategorie-Boost-Regeln ────────────────────────────────────────────────────
# Schlüsselwörter im GAEB-Kategorie-Pfad → Boost für Ressourcentyp oder Artikelgruppe
# Format: (path_keywords, resource_types_boost, article_warengruppe_keywords, resource_penalty)
CATEGORY_BOOSTS: list[tuple[set[str], set[str], set[str], int]] = [
    # GAEB-Kategorie-Keywords  → bevorzugte Ressourcentypen, bevorzugte Artikel-Gruppen, Artikel-Abzug
    ({"personal", "crew", "techniker", "staff", "personal/crew"},
     {"Personal"}, set(), 25),

    ({"fahrzeug", "transport", "lkw", "truck", "spedition"},
     {"Fahrzeug"}, set(), 20),

    ({"arbeitsmittel", "handling", "entladung", "beladeung"},
     {"Arbeitsmittel"}, set(), 15),

    # Technische Kategorien → Artikel bevorzugen, Ressourcen abwerten
    ({"rigging", "traverse", "truss", "motor", "chain hoist"},
     set(), {"Rigging", "Traverse", "Motor", "Truss"}, 0),

    ({"licht", "lighting", "beleuchtung", "dimmer", "follow", "spot"},
     set(), {"Licht", "Beleuchtung", "Moving"}, 0),

    ({"ton", "audio", "sound", "beschallung", "mikrofon"},
     set(), {"Ton", "Audio", "Beschallung", "Mikrofon"}, 0),

    ({"video", "bildwand", "led wall", "projektion"},
     set(), {"Bildwand", "Projektion"}, 0),  # kein "Video" – Substring "video" würde "Videomischer" matchen

    # Displays / Touchscreens / Monitore
    # Touch-Display-Kategorie: nur wenn explizit "touch" im Pfad → Touchdisplay boosten
    ({"touchscreen", "touch screen", "touchdisplay", "touch display", "touch-display"},
     set(), {"Touchdisplay", "Terminal", "Touchscreen"}, 0),
    # Allgemeine Display-Kategorie: Monitor/Bildschirm ohne Touch-Präferenz
    # Absichtlich KEIN "Touchdisplay" hier → Touch-Displays sollen nicht für normale Queries kommen
    ({"display", "monitor", "bildschirm", "screen", "lcd", "uhd", "kiosk"},
     set(), {"Monitor", "Bildschirm"}, 0),

    ({"strom", "power", "energie", "verkabelung", "cabling"},
     set(), {"Strom", "Energie", "CEE", "Schuko"}, 5),
]

RESOURCE_PENALTY_TECHNICAL = 12
RESOURCE_BOOST = 20
ARTICLE_BOOST_KEYWORD = 15

# Kalkulationsposition
KALKPOS_MARKERS = ["kalkulationsposition", "kalkpos", "nur kalkulation"]

# LED-Wand: bevorzugte Warengruppen-Keywords (Boost), abgewertete Warengruppen
# Mehrwörtige Treffer vermeiden zu breite Einzelwort-Matches (z.B. "Wand" → Wandeinbauleuchte)
LED_WALL_BOOST_SUBSTRINGS = {"videowand", "led wall", "led-wall", "led tile",
                              "led modul", "led-modul", "led panel", "pixel pitch",
                              "led video", "ledwall"}
LED_WALL_PENALIZE_KEYWORDS = {"Monitor", "Display", "TV", "Bildschirm", "Wandeinbau"}
LED_WALL_BOOST = 22

# ── Verkabelungs-Pauschal ─────────────────────────────────────────────────────
# Wenn eine GAEB-Position Cabling/Verkabelung fragt → passenden Pauschal-Artikel boosten
VERKABELUNG_QUERY_KEYWORDS = {
    "cabling", "verkabelung", "kabel", "cable", "wiring",
    "distribution", "power distribution",
}
# GAEB-Subkategorie-Keywords → Schlüsselwort in Artikel-Bezeichnung
VERKABELUNG_SUBCAT: list[tuple[set[str], str]] = [
    ({"audio", "ton", "sound", "multicore"},           "AUDIO"),
    ({"licht", "light", "lighting", "dmx"},            "LICHT"),
    ({"rigging", "truss", "traverse", "motor"},        "RIGGING"),
    ({"strom", "power", "energie", "cee", "electric"}, "STROM"),
    ({"video", "led", "display", "bildwand"},          "VIDEO"),
]
VERKABELUNG_BOOST = 38


# ─── Datenmodelle ─────────────────────────────────────────────────────────────

@dataclass
class Article:
    nummer: str
    bezeichnung: str
    warengruppe: str
    mutterwarengruppe: str
    artikelart: str
    hersteller: str
    detail: str
    kommentar: str
    mietpreis: float
    einheit: str
    mietinventar: int   # physische Lagerbestandsmenge (0 = nicht im eigenen Lager)
    gaeb_synonyms: list[str]

    @property
    def search_text(self) -> str:
        parts = [self.artikelart, self.hersteller]
        if self.detail:
            parts.append(self.detail[:120])
        return " ".join(p for p in parts if p).strip()

    @property
    def display_id(self) -> str:
        return self.nummer

    @property
    def display_name(self) -> str:
        return self.bezeichnung

    @property
    def display_group(self) -> str:
        return self.warengruppe

    @property
    def display_price(self) -> str:
        return f"{self.mietpreis:.2f} €/Tag" if self.mietpreis else "—"

    @property
    def display_type(self) -> str:
        return "Artikel"


@dataclass
class Resource:
    """Personal, Fahrzeug oder Arbeitsmittel aus personal.json."""
    id: int
    funktion: str
    ressourcenart: str   # 'Personal', 'Fahrzeug', 'Arbeitsmittel'
    tagessatz: float
    satzname: str
    gaeb_synonyms: list[str]

    @property
    def search_text(self) -> str:
        return f"{self.funktion} {self.ressourcenart}".strip()

    @property
    def display_id(self) -> str:
        return f"RES-{self.id}"

    @property
    def display_name(self) -> str:
        return self.funktion

    @property
    def display_group(self) -> str:
        return self.ressourcenart

    @property
    def display_price(self) -> str:
        return f"{self.tagessatz:.2f} €/Tag" if self.tagessatz else "—"

    @property
    def display_type(self) -> str:
        icons = {"Personal": "👤", "Fahrzeug": "🚚", "Arbeitsmittel": "🔧"}
        return icons.get(self.ressourcenart, "📦") + " " + self.ressourcenart


Matchable = Union[Article, Resource]


@dataclass
class MatchResult:
    matched: Optional[Matchable]   # Article oder Resource
    score: float
    method: str                    # "synonym", "fuzzy", "claude"
    confident: bool
    breakdown: dict = field(default_factory=dict)  # Score-Erklärung für GUI

    @property
    def article(self) -> Optional[Article]:
        return self.matched if isinstance(self.matched, Article) else None

    @property
    def resource(self) -> Optional[Resource]:
        return self.matched if isinstance(self.matched, Resource) else None

    @property
    def color(self) -> str:
        if self.score >= HIGH_SCORE:
            return "#1a7a1a"
        if self.score >= LOW_SCORE:
            return "#b36b00"
        return "#cc2222"


# ─── Laden ────────────────────────────────────────────────────────────────────

def _parse_synonyms(kommentar: str) -> list[str]:
    if not kommentar or GAEB_SYNONYM_TAG not in kommentar:
        return []
    start = kommentar.index(GAEB_SYNONYM_TAG) + len(GAEB_SYNONYM_TAG)
    end = kommentar.find(GAEB_SYNONYM_END, start)
    if end == -1:
        return []
    return [s.strip() for s in kommentar[start:end].split(";") if s.strip()]


def _is_excluded(it: dict) -> bool:
    bezeichnung = it.get("Bezeichnung", "") or ""
    warengruppe = it.get("Warengruppe", "") or ""
    bez_upper = bezeichnung.upper()
    if any(bez_upper.startswith(p.upper()) for p in EXCLUDE_PREFIXES):
        return True
    if warengruppe in EXCLUDE_WARENGRUPPEN:
        return True
    if any(ex.lower() in bezeichnung.lower() for ex in EXCLUDE_CONTAINS):
        return True
    return False


def is_kalkulations_position(description: str) -> bool:
    """Erkennt reine Kalkulationspositionen (Pauschal-Platzhalter ohne echten Artikel)."""
    dl = description.lower()
    return any(m in dl for m in KALKPOS_MARKERS)


_LED_WALL_PHRASES = {"led wall", "led screen", "led-wall", "ledwall", "pixel pitch"}

def is_led_wall(description: str) -> bool:
    """LED + Maßangabe in mm oder LED-Wand-Phrase → LED-Wand-Modus."""
    dl = description.lower()
    # LED + Pixel-Pitch (z.B. "LED HDR 2.6mm", "LED 3,9mm pitch") → eindeutig LED-Wand
    if 'led' in dl and _PIXEL_PITCH_RE.search(description):
        return True
    return bool(_LED_WALL_RE.search(description)) or any(p in dl for p in _LED_WALL_PHRASES)


def is_verkabelung_position(query: str) -> bool:
    """Erkennt GAEB-Positionen die Cabling/Verkabelung anfragen."""
    dl = query.lower()
    return any(k in dl for k in VERKABELUNG_QUERY_KEYWORDS)


def is_t_corner(query: str) -> bool:
    """Erkennt T-Corner/T-Stück/3-Weg-Eckverbinder-Positionen."""
    return bool(_TCORNER_RE.search(query))


def _category_adjustments(category_path: list[str]) -> tuple[set[str], set[str], int]:
    """Gibt zurück: (bevorzugte Resource-Typen, bevorzugte Artikel-Gruppen-Keywords, Ressourcen-Abzug)."""
    path_lower = " ".join(category_path).lower()
    preferred_res: set[str] = set()
    preferred_art: set[str] = set()
    res_penalty = 0

    for path_keys, res_types, art_groups, penalty in CATEGORY_BOOSTS:
        if any(k in path_lower for k in path_keys):
            preferred_res |= res_types
            preferred_art |= art_groups
            res_penalty = max(res_penalty, penalty)

    # Wenn es klar eine technische Kategorie ist (Artikel bevorzugt), Ressourcen abwerten
    if preferred_art and not preferred_res:
        res_penalty = RESOURCE_PENALTY_TECHNICAL

    return preferred_res, preferred_art, res_penalty


def load_articles_db() -> list[Article]:
    """Lädt Artikel aus der SQLite-DB."""
    from db import load_articles_db as _db_load
    items = _db_load()
    articles = []
    for it in items:
        if _is_excluded(it):
            continue
        kommentar = it.get("kommentar") or ""
        articles.append(Article(
            nummer=it.get("nummer", ""),
            bezeichnung=it.get("bezeichnung", ""),
            warengruppe=it.get("warengruppe", ""),
            mutterwarengruppe=it.get("mutterwarengruppe", ""),
            artikelart=it.get("artikelart", ""),
            hersteller=it.get("hersteller", ""),
            detail=it.get("detail", "") or "",
            kommentar=kommentar,
            mietpreis=float(it.get("mietpreis") or 0),
            einheit=it.get("einheit", "") or "",
            mietinventar=int(it.get("mietinventar") or 0),
            gaeb_synonyms=_parse_synonyms(kommentar),
        ))
    return articles


def load_resources_db() -> list[Resource]:
    """Lädt Personal/Ressourcen aus der SQLite-DB."""
    from db import load_personal_db as _db_load
    rows = _db_load()
    return [
        Resource(
            id=int(r.get("id", 0)),
            funktion=(r.get("funktion") or "").strip(),
            ressourcenart=r.get("ressourcenart", ""),
            tagessatz=float(r.get("tagessatz") or 0),
            satzname=r.get("satzname") or "",
            gaeb_synonyms=[],
        )
        for r in rows if (r.get("funktion") or "").strip()
    ]


def load_articles(json_path: str | Path) -> list[Article]:
    """Lädt artikel.json, filtert ZZZ-Artikel und ausgeschlossene Warengruppen."""
    with open(json_path, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw if isinstance(raw, list) else raw.get("items", [])
    articles = []
    skipped = 0
    for it in items:
        if _is_excluded(it):
            skipped += 1
            continue
        kommentar = it.get("Kommentar") or ""
        articles.append(Article(
            nummer=it.get("Nummer", ""),
            bezeichnung=it.get("Bezeichnung", ""),
            warengruppe=it.get("Warengruppe", ""),
            mutterwarengruppe=it.get("Mutterwarengruppe", ""),
            artikelart=it.get("Artikelart", ""),
            hersteller=it.get("Hersteller", ""),
            detail=it.get("Detailbeschreibung", "") or "",
            kommentar=kommentar,
            mietpreis=float(it.get("Mietpreis") or 0),
            einheit=it.get("Einheit", "") or "",
            mietinventar=int(it.get("Mietinventar") or 0),
            gaeb_synonyms=_parse_synonyms(kommentar),
        ))
    return articles


def load_resources(json_path: str | Path) -> list[Resource]:
    """Lädt personal.json (Personal, Fahrzeuge, Arbeitsmittel)."""
    with open(json_path, encoding="utf-8") as f:
        raw = json.load(f)
    rows = raw if isinstance(raw, list) else raw.get("rows", [])
    resources = []
    for r in rows:
        funktion = r.get("Funktion") or ""
        if not funktion.strip():
            continue
        resources.append(Resource(
            id=int(r.get("IdResourceFunction", 0)),
            funktion=funktion.strip(),
            ressourcenart=r.get("Ressourcenart", ""),
            tagessatz=float(r.get("Tagessatz") or 0),
            satzname=r.get("Satzname") or "",
            gaeb_synonyms=[],
        ))
    return resources


# ─── Synonyme aktualisieren ───────────────────────────────────────────────────

def encode_synonym_tag(synonyms: list[str]) -> str:
    return f"{GAEB_SYNONYM_TAG} {'; '.join(synonyms)}{GAEB_SYNONYM_END}"


def update_article_kommentar(article: Article, new_synonym: str) -> str:
    """Gibt den aktualisierten Kommentar-String zurück (neues Synonym eingetragen)."""
    synonyms = list(article.gaeb_synonyms)
    if new_synonym not in synonyms:
        synonyms.append(new_synonym)
    tag = encode_synonym_tag(synonyms)
    base = article.kommentar
    if GAEB_SYNONYM_TAG in base:
        start = base.index(GAEB_SYNONYM_TAG)
        end = base.find(GAEB_SYNONYM_END, start)
        if end != -1:
            base = (base[:start] + base[end + 1:]).strip()
    return f"{base}\n{tag}".strip() if base else tag


# ─── Scoring ──────────────────────────────────────────────────────────────────

def _combined_score(query: str, candidate: str) -> float:
    s1 = fuzz.token_sort_ratio(query, candidate)
    s2 = fuzz.partial_token_set_ratio(query, candidate)
    s3 = fuzz.WRatio(query, candidate)
    return (s1 * 0.45) + (s2 * 0.35) + (s3 * 0.20)


# ─── Matcher ──────────────────────────────────────────────────────────────────

class UnifiedMatcher:
    """Sucht über Artikel UND Ressourcen (Personal, Fahrzeuge, Arbeitsmittel)."""

    def __init__(self, articles: list[Article], resources: list[Resource],
                 mappings_path: str | Path | None = None,
                 gui_mappings_path: str | Path | None = None):
        self.articles = articles
        self.resources = resources
        self._pool: list[Matchable] = [*articles, *resources]
        self._corpus = [m.search_text for m in self._pool]
        self._bezeichnung = [m.display_name for m in self._pool]

        # Schnell-Index: Artikelnummer → Pool-Index
        self._num_to_idx: dict[str, int] = {
            a.nummer: i for i, a in enumerate(articles)
        }

        # Training- und GUI-Mappings aus DB laden
        self._res_keys:    list[str] = []
        self._res_nums:    list[str] = []
        self._res_sources: list[str] = []   # "train" | "gui"
        self._section_map:    dict[str, list[str]] = {}
        self._bundle_extras:  dict[str, list[str]] = {}  # description → [extra_nums]

        from db import load_train_mappings, load_gui_mappings

        def _apply(primary: dict[str, str], extras: dict[str, list[str]],
                   source: str) -> None:
            for key, num in primary.items():
                if num in self._num_to_idx:
                    self._res_keys.append(key)
                    self._res_nums.append(num)
                    self._res_sources.append(source)
            for key, nums in extras.items():
                valid = [n for n in nums if n in self._num_to_idx]
                if valid:
                    self._bundle_extras[key] = valid

        train_primary, train_extras, train_sections = load_train_mappings()
        self._section_map = train_sections
        _apply(train_primary, train_extras, "train")

        gui_primary, gui_extras = load_gui_mappings()
        _apply(gui_primary, gui_extras, "gui")

    @property
    def _article_indices(self) -> list[int]:
        return list(range(len(self.articles)))

    def add_learned_bundle(self, gaeb_description: str, numbers: list[str]) -> None:
        """Aktualisiert In-Memory-Index für Primary + Extras. Leere Liste = vergessen."""
        # Alte Einträge für diese Beschreibung entfernen
        triplets = [
            (k, n, s) for k, n, s in
            zip(self._res_keys, self._res_nums, self._res_sources)
            if k != gaeb_description
        ]
        if numbers and numbers[0] in self._num_to_idx:
            triplets.append((gaeb_description, numbers[0], "gui"))
        if triplets:
            ks, ns, ss = zip(*triplets)
            self._res_keys    = list(ks)
            self._res_nums    = list(ns)
            self._res_sources = list(ss)
        else:
            self._res_keys = self._res_nums = self._res_sources = []

        extras = [n for n in numbers[1:] if n in self._num_to_idx]
        if extras:
            self._bundle_extras[gaeb_description] = extras
        else:
            self._bundle_extras.pop(gaeb_description, None)

    def get_bundle_extras(self, query: str) -> list[str]:
        """Gibt gelernte Zusatzartikel-Nummern für eine GAEB-Beschreibung zurück."""
        if not self._bundle_extras:
            return []
        keys = list(self._bundle_extras.keys())
        query_de = translate_en_de(query)
        best_score, best_extras = 0.0, []
        for q in dict.fromkeys([query, query_de]):
            hits = process.extract(q, keys, scorer=fuzz.token_sort_ratio, limit=1)
            if hits and hits[0][1] > best_score:
                best_score = hits[0][1]
                best_extras = self._bundle_extras[keys[hits[0][2]]]
        return best_extras if best_score >= 85 else []

    def match(self, query: str, limit: int = 5,
              category_path: list[str] | None = None,
              qty: float = 0.0, unit: str = "",
              long_text: str = "") -> list[tuple[MatchResult, Matchable]]:
        if not query.strip():
            return []

        preferred_res, preferred_art, res_penalty = (
            _category_adjustments(category_path) if category_path
            else (set(), set(), 0)
        )

        # Englische Begriffe übersetzen → besseres Matching gegen deutschen Artikelstamm
        query_de = translate_en_de(query)
        # Subkategorie-Matching: Query + Übersetzung + Kategoriepfad (für Cabling-Subtypen)
        _cat_str   = " ".join(category_path).lower() if category_path else ""
        _q_combined = f"{query} {query_de} {_cat_str}".lower()

        led_wall       = is_led_wall(query)
        traverse       = parse_traverse_info(query)
        is_verkabelung = is_verkabelung_position(query)
        is_tcorner     = is_t_corner(query)
        is_motor       = is_motor_position(query)
        motor_cap_kg   = parse_motor_capacity_kg(query) if is_motor else None
        motor_hub_req  = next((int(m.group(1)) for m in [_MOTOR_HUB_RE.search(query)] if m), None)
        is_boxcorner   = bool(_BOXCORNER_RE.search(query) or _BOXCORNER_RE.search(query_de))
        # "corner" im Traverse-Kontext → T-Corner/Boxcorner (z.B. "4-point truss 300mm - Corner")
        if traverse and not is_tcorner and re.search(r'\bcorner\b', query, re.IGNORECASE):
            is_tcorner = True
            is_boxcorner = True
        # "90°" / "90 Grad" / "Winkel" im Traverse-Kontext → Boxcorner-Modus
        if traverse and not is_boxcorner:
            if re.search(r'\b90\s*[°g]|\bwinkel\b|\bl[\s-]?ecke\b', query, re.IGNORECASE) \
               or re.search(r'\b90\s*[°g]|\bwinkel\b|\bl[\s-]?ecke\b', query_de, re.IGNORECASE):
                is_boxcorner = True
        # Langtext-Hinweis: "t-corner" im Langtext ergänzt fehlende Query-Info
        if not is_tcorner and long_text and re.search(r'\bt[\s-]?corner\b', long_text, re.IGNORECASE):
            if traverse:
                is_tcorner = True
        is_metal_pipe  = bool(
            _IS_PIPE_RE.search(query) or _IS_PIPE_RE.search(query_de)
            or _GENERIC_PIPE_RE.search(query)
        )
        # 90°-Ecke: explizit L-förmig (2-Weg), KEIN T-Stück
        _is_90_corner = is_boxcorner and bool(
            re.search(r'\b90\s*°|\b90\s*grad\b', query, re.IGNORECASE)
            or re.search(r'\b90\s*°|\b90\s*grad\b', query_de, re.IGNORECASE)
            or re.search(r'corner\s+90|90\s+corner|l[\s-]?90\b', query, re.IGNORECASE)
        )
        # Display-Kontext: Touch explizit angefragt? Welche Zoll?
        touch_required  = bool(_TOUCH_RE.search(query) or _TOUCH_RE.search(_cat_str))
        display_inch_q  = _parse_display_inch(query) or _parse_display_inch(_cat_str)

        # is_display_ctx: NUR wenn die ANFRAGE SELBST auf ein Display hindeutet.
        # bool(preferred_art) allein reicht NICHT – damit würde z.B. "MediaPlayer - Mini PC"
        # in einer GAEB-Gruppe "Displaytechnik" fälschlicherweise Display-Scoring bekommen.
        _has_display_word = bool(
            _DISPLAY_WORD_RE.search(query) or _DISPLAY_WORD_RE.search(query_de)
        )
        # Fallback: freie Zahl als Zoll wenn Anfrage Display-Wörter enthält ("Display 55 UHD")
        if _has_display_word and display_inch_q is None:
            for _m in re.finditer(r'\b(\d{2,3})\b', query):
                _v = int(_m.group(1))
                if 24 <= _v <= 110:
                    display_inch_q = _v
                    break
        is_display_ctx = _has_display_word or (display_inch_q is not None)

        # Metallrohre (Alu/Stahl) sind keine Traversen → Traverse-Scoring deaktivieren
        if traverse and is_metal_pipe:
            traverse = None

        # Touch-Display-Kontext aus Query ableiten wenn kein Kategoriepfad vorhanden
        if _DISPLAY_QUERY_RE.search(query):
            preferred_art = preferred_art | {"Touchdisplay", "Terminal", "Touchscreen"}
            touch_required = True

        # Wenn Laufmeter-Einheit und keine explizite Länge → Standard-Stücklänge für Scoring
        if traverse and traverse.length_m is None and unit and \
                unit.lower().strip(".") in {u.strip(".") for u in TRAVERSE_LFM_UNITS}:
            traverse.length_m = TRAVERSE_STANDARD_LENGTH_M

        # Erkannte Kontext-Flags für Score-Breakdown in der GUI
        _bd_detected: list[str] = []
        if is_display_ctx:
            _bd_detected.append(f"📺 Display")
            if display_inch_q:
                _bd_detected.append(f"📐 {display_inch_q}\"")
            _bd_detected.append("🤚 Touch" if touch_required else "🚫 kein Touch")
        if is_metal_pipe:
            _bd_detected.append("🔩 Metallrohr")
        if is_boxcorner:
            _bd_detected.append("📦 Boxcorner")
        if is_verkabelung:
            _bd_detected.append("🔌 Verkabelung")
        if traverse:
            _bd_detected.append("🏗️ Traverse")
        if led_wall:
            _bd_detected.append("💡 LED-Wand")
        if is_motor:
            _bd_detected.append("⚙️ Motor")
        # Langtext-Hersteller wird nach Pre-Loading ergänzt (erst dann befüllt)

        results: list[tuple[float, Matchable, str]] = []
        seen: set[str] = set()
        mapping_boosts: dict[int, float] = {}   # pool_idx → extra Punkte
        mapping_sources: dict[int, str]  = {}   # pool_idx → "train" | "gui"
        _lt_boosts: set[int] = set()            # pool_idx → Boost kam aus Langtext-Hersteller
        _lt_hersteller_found: set[str] = set()  # befüllt in 1i, für _bd_detected
        _breakdowns: dict[int, dict] = {}       # pool_idx → breakdown-dict für GUI

        # 1a. Training-Mappings: bekannte Artikel-Texte aus vergangenen Jobs
        if self._res_keys:
            for q in dict.fromkeys([query, query_de]):  # beide Varianten, ohne Dopplung
                map_hits = process.extract(
                    q, self._res_keys,
                    scorer=fuzz.token_sort_ratio,
                    limit=5,
                )
                for _, map_score, midx in map_hits:
                    if map_score >= 70:
                        num = self._res_nums[midx]
                        pidx = self._num_to_idx.get(num)
                        if pidx is not None:
                            # Quadratische Formel: borderline-Treffer (~70%) geben kaum Boost,
                            # echte Treffer (90%+) geben starken Boost.
                            # 70%→1pt, 80%→9pt, 90%→25pt, 95%→45pt, 100%→68pt
                            boost = (map_score - 65) ** 2 / 15
                            if boost > mapping_boosts.get(pidx, 0):
                                mapping_boosts[pidx] = boost
                                mapping_sources[pidx] = self._res_sources[midx]

        # 1b. Section-Label Matching (besonders hilfreich für englische GAEB-Abschnitte)
        # Boost bewusst klein gehalten: Traverse-Typ-Scoring soll dominieren.
        # Hoher Threshold (68%) verhindert falsche Boosts bei ähnlichen aber anderen Sektionen.
        if self._section_map:
            section_keys = list(self._section_map.keys())
            section_labels_clean = [
                re.sub(r'^\d{2}\.\d{2}\.\d{2}\s+', '', s) for s in section_keys
            ]
            for q in dict.fromkeys([query, query_de]):
                sec_hits = process.extract(
                    q, section_labels_clean,
                    scorer=fuzz.token_sort_ratio, limit=3
                )
                for _, sec_score, sec_idx in sec_hits:
                    if sec_score >= 68:
                        sec_nums = self._section_map[section_keys[sec_idx]]
                        for i, num in enumerate(sec_nums):
                            pidx = self._num_to_idx.get(num)
                            if pidx is not None:
                                # Boost-Formel: kleiner als vorher, damit Traverse-Scoring dominiert
                                factor = 0.55 if i == 0 else 0.2
                                boost = (sec_score - 65) * factor
                                mapping_boosts[pidx] = max(mapping_boosts.get(pidx, 0), boost)

        # 1c. Synonym-Lookup in Artikel-Kommentaren
        q_lower = query.lower()
        for art in self.articles:
            for syn in art.gaeb_synonyms:
                if syn.lower() in q_lower or q_lower in syn.lower():
                    results.append((97.0, art, "synonym"))
                    seen.add(art.display_id)
                    break

        # 1d. Verkabelungs-Pauschal-Artikel vorab in mapping_boosts aufnehmen
        # → verhindert dass sie durch Fuzzy-Vorfilter herausfallen
        # Generisch: alle "Verkabelung"-Artikel bekommen Basis-Boost
        # Spezifisch: passendes AUDIO/STROM/LICHT/… → vollen Boost
        if is_verkabelung:
            for art in self.articles:
                bez_l = art.bezeichnung.lower()
                # "Verbrauch Verkabelung ..." = Materialverbrauch, keine Pauschale → überspringen
                if "verkabelung" in bez_l and "verbrauch" not in bez_l:
                    pidx = self._num_to_idx.get(art.nummer)
                    if pidx is None:
                        continue
                    boost = VERKABELUNG_BOOST // 2  # generisch immer
                    for subcat_keys, art_label in VERKABELUNG_SUBCAT:
                        if any(k in _q_combined for k in subcat_keys) \
                                and art_label in art.bezeichnung.upper():
                            boost = VERKABELUNG_BOOST  # spezifisch > generisch
                            break
                    mapping_boosts[pidx] = max(mapping_boosts.get(pidx, 0), boost)

        # 1e. Motor-Artikel vorab in mapping_boosts eintragen → garantiert Aufnahme in Pre-Hits
        if is_motor:
            for art in self.articles:
                if bool(_MOTOR_DETECT_RE.search(art.bezeichnung)):
                    pidx = self._num_to_idx.get(art.nummer)
                    if pidx is not None and pidx not in mapping_boosts:
                        mapping_boosts[pidx] = 0  # kein Boost, nur Kandidat sichern

        # 1f. Bei Display-Anfragen (erkennbar an preferred_art) alle Touchdisplay-Artikel vorab laden
        # Verhindert, dass richtige Displays durch den Bezeichnung-Pre-Filter herausfallen
        if preferred_art and "touchdisplay" in {kw.lower() for kw in preferred_art}:
            for art in self.articles:
                wg_l = art.warengruppe.lower()
                if "touchdisplay" in wg_l or "terminal" in wg_l:
                    pidx = self._num_to_idx.get(art.nummer)
                    if pidx is not None and pidx not in mapping_boosts:
                        mapping_boosts[pidx] = 0

        # 1g. Boxcorner/Eckelement-Anfragen: Eck-Artikel vorab sichern
        if is_boxcorner:
            _corner_kws_pre = ("boxcorner", "eckelement", "eckverbinder",
                               "l90", "l-ecke", "winkelecke", "corner 90", "corner90")
            for art in self.articles:
                bez_l = art.bezeichnung.lower()
                if any(kw in bez_l for kw in _corner_kws_pre):
                    pidx = self._num_to_idx.get(art.nummer)
                    if pidx is not None and pidx not in mapping_boosts:
                        mapping_boosts[pidx] = 0

        # 1h. Metall-Rohr-Anfragen: echte Rohr-Artikel vorab sichern
        if is_metal_pipe:
            for art in self.articles:
                bez_l = art.bezeichnung.lower()
                if "rohr" in bez_l and any(m in bez_l for m in ("alu", "aluminium", "stahl", "steel")):
                    pidx = self._num_to_idx.get(art.nummer)
                    if pidx is not None and pidx not in mapping_boosts:
                        mapping_boosts[pidx] = 0

        # 1i. Langtext-Hersteller-Lookup: Hersteller-Feld aus Easyjob gegen Langtext-Wörter prüfen
        # NUR das explizite Hersteller-Feld (kein Bezeichnung-Fallback – zu viele Common-Word-Treffer)
        # Wortgrenze-Match: "divers" matcht NICHT als Substring in "diverse"
        # Stop-Liste: generische Platzhalter-Hersteller wie "Diverse", "Divers" überspringen
        _LT_GENERIC_HERST = frozenset({
            "divers", "diverse", "various", "standard", "unbekannt",
            "sonstige", "sonstiges", "verschiedene", "misc", "n/a",
        })
        _lt_hersteller_found: set[str] = set()
        if long_text:
            lt_lower = long_text.lower()
            for art in self.articles:
                h = (art.hersteller or "").strip()
                if len(h) < 4 or h.lower() in _LT_GENERIC_HERST:
                    continue
                # Wortgrenze: "Lenovo" matcht "Lenovo" aber nicht als Teil von "LenovoTech"
                if re.search(r'\b' + re.escape(h.lower()) + r'\b', lt_lower):
                    pidx = self._num_to_idx.get(art.nummer)
                    if pidx is not None:
                        lt_boost = 35
                        if lt_boost > mapping_boosts.get(pidx, 0):
                            mapping_boosts[pidx] = lt_boost
                            _lt_boosts.add(pidx)
                        _lt_hersteller_found.add(h)

        # 1h-it. Notebook/Laptop-Anfragen: alle Computer-Notebook-Artikel vorab sichern
        # "Windows Laptops" → query_de = "Windows notebooks" – "notebook" erscheint nicht in allen
        # Artikel-Bezeichnungen nahe genug für Top-50 des Pre-Filters
        _is_notebook_q = bool(re.search(r'\b(notebook|laptop)s?\b', query_de, re.IGNORECASE)
                              or re.search(r'\b(notebook|laptop)s?\b', query, re.IGNORECASE))
        # Endstufen-Kontext: "amplifier", "endstufe", "verstärker" in Query → Rigging-Zubehör bestrafen
        _is_amplifier_q = bool(re.search(r'\b(amplifier|endstufe|verst[äa]rker)\b', query, re.IGNORECASE)
                               or re.search(r'\b(endstufe|verstärker)\b', query_de, re.IGNORECASE))
        if _is_notebook_q:
            for art in self.articles:
                if "notebook" in art.bezeichnung.lower():
                    pidx = self._num_to_idx.get(art.nummer)
                    if pidx is not None and pidx not in mapping_boosts:
                        mapping_boosts[pidx] = 0

        # 1h. Display-Anfragen mit Zoll-Angabe: Displays nahe der gesuchten Größe vorab sichern
        # Ohne dies fallen 85"/98"-Artikel oft aus den Top-50 des Bezeichnung-Pre-Filters heraus
        # Schwellwert 50": 55"-Displays fallen sonst aus den Top-50 bei langen Queries
        if is_display_ctx and display_inch_q and display_inch_q >= 50:
            _DISPLAY_WG_KEYS = ("lcd-display", "monitor", "bildschirm", "touchdisplay", "terminal")
            for art in self.articles:
                art_inch = _parse_art_inch(art.bezeichnung)
                if art_inch is None:
                    continue
                if abs(art_inch - display_inch_q) > 15:
                    continue
                wg_l = art.warengruppe.lower()
                if not any(kw in wg_l for kw in _DISPLAY_WG_KEYS):
                    continue
                pidx = self._num_to_idx.get(art.nummer)
                if pidx is not None and pidx not in mapping_boosts:
                    mapping_boosts[pidx] = 0

        # Langtext-Treffer jetzt bekannt → in Breakdown eintragen
        if _lt_hersteller_found:
            _bd_detected.append("📄 Langtext: " + ", ".join(sorted(_lt_hersteller_found)))

        # 2. Pre-filter: übersetzte Anfrage gegen deutschen Artikel-Korpus
        pre_hits = process.extract(
            query_de, self._bezeichnung,
            scorer=fuzz.token_sort_ratio,
            limit=max(50, limit * 10),
        )

        # 3. Re-Score mit kombiniertem Scorer + Kategorie-Anpassungen + Mapping-Boost
        rescored: list[tuple[float, int]] = []
        # Mapping-Boost-Artikel auch ins Pre-Hit-Set aufnehmen falls noch nicht drin
        pre_hit_idxs = {idx for _, _, idx in pre_hits}
        for pidx in mapping_boosts:
            if pidx not in pre_hit_idxs:
                pre_hits = list(pre_hits) + [("", 0, pidx)]

        for _, _, idx in pre_hits:
            item = self._pool[idx]
            sc = _combined_score(query_de, self._corpus[idx])
            _bd: list[tuple[float, str]] = [(sc, "Fuzzy-Score")]

            # Mapping-Boost anwenden (aus Training oder GUI)
            _mb = mapping_boosts.get(idx, 0)
            sc += _mb
            if _mb:
                if idx in _lt_boosts:
                    _mb_label = "Langtext-Hersteller-Treffer"
                elif mapping_sources.get(idx) == "gui":
                    _mb_label = "Mapping-Boost (GUI-Korrektur)"
                else:
                    _mb_label = "Mapping-Boost (Training)"
                _bd.append((_mb, _mb_label))

            if isinstance(item, Resource):
                if preferred_res and item.ressourcenart in preferred_res:
                    sc += RESOURCE_BOOST
                elif res_penalty:
                    sc -= res_penalty
            elif isinstance(item, Article):
                # Kabel-Abwertung – entfällt wenn die Anfrage selbst Verkabelung ist
                if item.warengruppe in PENALIZE_WARENGRUPPEN and not is_verkabelung:
                    sc -= WARENGRUPPE_PENALTY
                    _bd.append((-WARENGRUPPE_PENALTY, f"Warengruppen-Strafe ({item.warengruppe})"))
                # Endstufen-Kontext: alle Nicht-Endstufen abwerten
                if _is_amplifier_q:
                    _art_wg_l = item.warengruppe.lower()
                    if "endstufe" in _art_wg_l:
                        sc += 15
                        _bd.append((15, "Endstufen-Warengruppe (Amplifier-Anfrage)"))
                    elif "flug" in _art_wg_l or "rigging" in _art_wg_l:
                        sc -= 35
                        _bd.append((-35, f"Rigging-Zubehör bei Endstufen-Anfrage ({item.warengruppe})"))
                    elif any(kw in _art_wg_l for kw in ("lautsprecher", "basslautsprecher", "subwoofer", "kleinlautsprecher")):
                        sc -= 20
                        _bd.append((-20, f"Lautsprecher bei Endstufen-Anfrage ({item.warengruppe})"))
                    else:
                        sc -= 20
                        _bd.append((-20, f"Nicht-Endstufe bei Amplifier-Anfrage ({item.warengruppe})"))
                # Notebook-Kontext: echte Notebooks boosten, Stative/Ständer abwerten
                if _is_notebook_q:
                    _art_bez_l = item.bezeichnung.lower()
                    _art_wg_l2 = item.warengruppe.lower()
                    if any(kw in _art_bez_l for kw in ("stativ", "ständer", "bodenstativ", "stand", "halter", "halterung")):
                        sc -= 40
                        _bd.append((-40, "Stativ/Ständer bei Notebook-Anfrage"))
                    elif "computer" in _art_wg_l2 and "notebook" in _art_bez_l:
                        sc += 20
                        _bd.append((20, "Notebook-Computer (Notebook-Anfrage)"))
                # Verkabelungs-Pauschale boosten: generisch + Subtyp-spezifisch
                # Verbrauchsartikel explizit ausschließen (separate Penalty weiter unten)
                if is_verkabelung and "verkabelung" in item.bezeichnung.lower() \
                        and "verbrauch" not in item.bezeichnung.lower():
                    sc += VERKABELUNG_BOOST // 2
                    _bd.append((VERKABELUNG_BOOST // 2, "Verkabelungs-Bonus (generisch)"))
                    for subcat_keys, art_label in VERKABELUNG_SUBCAT:
                        if any(k in _q_combined for k in subcat_keys) \
                                and art_label in item.bezeichnung.upper():
                            sc += VERKABELUNG_BOOST
                            _bd.append((VERKABELUNG_BOOST, f"Verkabelungs-Bonus (Typ: {art_label})"))
                            break
                # Qualitäts-Penalties nur wenn kein starker Lern-Boost vorliegt
                # (Gelernte Artikel sollen trotz schlechter Stammdaten gewinnen dürfen)
                has_learning = mapping_boosts.get(idx, 0) > 12
                # Mietinventar-Scoring: physischer Bestand ist immer relevant
                # (kein Lern-Bypass — ob wir den Artikel haben ist eine Tatsache)
                if item.mietinventar == 0:
                    sc -= 20
                    _bd.append((-20, "Kein Mietinventar (Fremdgerät)"))
                elif item.mietinventar > 0:
                    _inv_bonus = 8 + (5 if item.mietinventar >= 5 else 0)
                    sc += _inv_bonus
                    _bd.append((_inv_bonus, f"Inventar ({item.mietinventar} Stk.)"))
                if not item.artikelart and not item.hersteller \
                        and not item.detail and not has_learning:
                    sc -= 8
                    _bd.append((-8, "Fehlende Stammdaten"))
                # Artikel mit Detailbeschreibung bevorzugen (werden aktiv angeboten)
                if item.detail:
                    sc += 5
                    _bd.append((5, "Detailbeschreibung vorhanden"))
                # Artikel-Gruppen-Boost wenn Kategorie passt
                if preferred_art:
                    art_text = f"{item.warengruppe} {item.mutterwarengruppe} {item.bezeichnung}".lower()
                    # Bei Metallrohr-Anfragen traverse/rigging-Boost unterdrücken –
                    # GAEB-Kategoriepfad enthält oft "truss/traverse", was Bodenplatten etc. fälschlich boosten würde
                    _boost_art = preferred_art
                    if is_metal_pipe:
                        _traverse_kws = {"rigging", "traverse", "motor", "truss"}
                        _boost_art = {kw for kw in preferred_art
                                      if kw.lower() not in _traverse_kws}
                    if any(kw.lower() in art_text for kw in _boost_art):
                        sc += ARTICLE_BOOST_KEYWORD
                        _bd.append((ARTICLE_BOOST_KEYWORD, f"Kategorie-Treffer ({item.warengruppe})"))
                    if "touchdisplay" in {kw.lower() for kw in preferred_art} \
                            and "steglos" in item.warengruppe.lower():
                        sc -= 30
                        _bd.append((-30, "Steglosdisplay (kein Display-Gerät)"))
                    if "touchdisplay" in {kw.lower() for kw in preferred_art}:
                        wg_l = item.warengruppe.lower()
                        is_display_wg = (
                            "touchdisplay" in wg_l or "terminal" in wg_l
                            or "monitor" in wg_l
                        )
                        if not is_display_wg:
                            sc -= 35
                            _bd.append((-35, f"Falsche Warengruppe ({item.warengruppe})"))

                # Display-Kontext: Touch-Anforderung + Größen-Scoring
                if is_display_ctx:
                    art_bez_l = item.bezeichnung.lower()
                    is_touch_art = "touch" in art_bez_l
                    wg_l = item.warengruppe.lower()
                    if "overlay" in art_bez_l or "touchoverlay" in art_bez_l:
                        sc -= 35
                        _bd.append((-35, "Touchoverlay (kein eigenständiges Display)"))
                    is_display_like_wg = any(kw in wg_l for kw in (
                        "display", "monitor", "lcd", "bildschirm", "terminal", "touchdisplay"
                    ))
                    if not is_display_like_wg:
                        sc -= 25
                        _bd.append((-25, f"Keine Display-Warengruppe ({item.warengruppe})"))
                    if touch_required and not is_touch_art:
                        sc -= 20
                        _bd.append((-20, "Touch gefordert, Artikel hat kein Touch"))
                    if not touch_required and is_touch_art:
                        sc -= 50
                        _bd.append((-50, "Touch nicht gefordert, Artikel ist Touch-Display"))
                    is_pcap_art = "pcap" in art_bez_l or "objekterkennung" in art_bez_l
                    pcap_requested = bool(re.search(
                        r'\b(pcap|objekt|object.?recogni|multitouch|multi.?touch)\b',
                        query, re.IGNORECASE,
                    ))
                    if is_pcap_art and not pcap_requested:
                        sc -= 18
                        _bd.append((-18, "PCAP/Objekterkennung nicht angefragt"))
                    if display_inch_q:
                        art_inch = _parse_art_inch(item.bezeichnung)
                        if art_inch is not None:
                            diff = abs(art_inch - display_inch_q)
                            if diff == 0:
                                sc += 22
                                _bd.append((22, f"Größe exakt ({art_inch}\")"))
                            elif diff <= 5:
                                sc += 10
                                _bd.append((10, f"Größe nah ({art_inch}\" – {diff}\" Abw.)"))
                            elif diff <= 9:
                                sc -= 10
                                _bd.append((-10, f"Größe daneben ({art_inch}\" – {diff}\" Abw.)"))
                            else:
                                sc -= 22
                                _bd.append((-22, f"Größe falsch ({art_inch}\" – {diff}\" Abw.)"))
                        else:
                            _bd.append((0, "Keine Zoll-Angabe im Artikel"))

                # Traverse / T-Corner Boost
                if traverse:
                    art_lower = f"{item.bezeichnung} {item.warengruppe}".lower()
                    # Englische Farben normalisieren für Artikel-Matching
                    norm_color = _COLOR_NORMALIZE.get(
                        traverse.color or "", traverse.color
                    )
                    if is_tcorner:
                        # ── T-Corner/3-Weg-Position ──────────────────────────
                        is_tcorner_art = (
                            "t-stück" in art_lower or "t-ecke" in art_lower
                            or "3-weg" in art_lower or "3weg" in art_lower
                        )
                        if is_tcorner_art:
                            if traverse.is_hb_rohr:
                                # 2-Holm T-Corner → HB-Rohr T-Stück / Global Truss F32
                                if "hb-rohr" in art_lower or "zweiholm" in art_lower \
                                        or "f32" in art_lower:
                                    sc += 30
                                else:
                                    sc += 10
                            else:
                                # 3/4-Holm T-Corner → passende HD-Serie
                                series_l = traverse.hd_series.lower()
                                if series_l in art_lower:
                                    sc += 30
                                elif f"hd{series_l[2]}" in art_lower:
                                    sc += 15
                            if norm_color and norm_color in art_lower:
                                sc += 10
                            elif norm_color:
                                sc -= 8
                        else:
                            # Nicht-T-Corner-Artikel bei T-Corner-Position abwerten
                            sc -= 15
                    else:
                        # ── Laufmeter-Traverse-Position ──────────────────────
                        if traverse.is_hb_rohr:
                            # 2-Holm → HB-Rohr
                            if "hb-rohr" in art_lower:
                                sc += 28
                            elif "zweiholm" in art_lower:
                                sc += 8
                        else:
                            series_l = traverse.hd_series.lower()
                            if series_l in art_lower:
                                sc += 28
                            elif f"hd{series_l[2]}" in art_lower:
                                sc += 8
                        if norm_color and norm_color in art_lower:
                            sc += 10
                        elif norm_color:
                            sc -= 8
                        if traverse.length_m:
                            cm_val = int(traverse.length_m * 100)
                            # Länge sowohl als cm ("300cm") als auch als m ("3m") prüfen
                            cm_str = f"{cm_val}cm"
                            m_int  = f"{int(traverse.length_m)}m" \
                                     if traverse.length_m == int(traverse.length_m) else None
                            if cm_str in art_lower or (m_int and m_int in art_lower):
                                sc += 15
                        # Zubehör/Werkzeug penalisieren (kein Traversenstück)
                        if "eckelement" in art_lower or "boxcorner" in art_lower or \
                           "t-stück" in art_lower or "konusbuchse" in art_lower or \
                           "pinclaw" in art_lower or "pin claw" in art_lower or \
                           "zapfenbuchse" in art_lower or \
                           ("werkzeug" in art_lower and "traverse" in art_lower):
                            sc -= 25

                # LED-Wand: boost passende, penalize Monitor/Display
                if led_wall:
                    art_lower = f"{item.warengruppe} {item.mutterwarengruppe} {item.bezeichnung}".lower()
                    if any(kw in art_lower for kw in LED_WALL_BOOST_SUBSTRINGS):
                        sc += LED_WALL_BOOST
                    if any(kw.lower() in art_lower for kw in LED_WALL_PENALIZE_KEYWORDS):
                        sc -= LED_WALL_BOOST
                    # Controller/Stagebox sind Zubehör, nicht die eigentliche LED-Wand
                    if "controller" in art_lower or "stagebox" in art_lower:
                        sc -= 35
                        _bd.append((-35, "LED-Controller/Stagebox (kein Modul)"))

                # Motor/Hebezeug-Scoring: Kapazität und Hub-Länge
                if is_motor:
                    # Warengruppe "Hebezeuge" allein nicht ausreichend (enthält auch Steel/Schäkel)
                    # → Artikelname muss Motor-Keyword enthalten
                    is_motor_art = bool(_MOTOR_DETECT_RE.search(item.bezeichnung))
                    if not is_motor_art:
                        sc -= 50  # Kein Motor-Artikel → stark abwerten
                    else:
                        art_cap = motor_art_capacity_kg(item.bezeichnung)
                        if art_cap and motor_cap_kg:
                            if art_cap >= motor_cap_kg:
                                overshoot = art_cap / motor_cap_kg
                                if overshoot <= 1.0:
                                    sc += 35   # exakt
                                elif overshoot <= 1.6:
                                    sc += 28   # nächst-höher (z.B. 500 für 320kg)
                                elif overshoot <= 2.5:
                                    sc += 15   # deutlich größer
                                else:
                                    sc += 5    # viel zu groß
                            else:
                                sc -= 40       # zu kleine Kapazität → Sicherheitsrisiko
                        # Hub-Länge: extra Punkte wenn passend
                        art_hub = motor_art_hub_m(item.bezeichnung)
                        if motor_hub_req and art_hub:
                            if art_hub == motor_hub_req:
                                sc += 12
                            elif abs(art_hub - motor_hub_req) <= 6:
                                sc += 5

                # Metall-Rohr: echte Rohr-Artikel boosten, Traverse/Rigging stark abwerten
                if is_metal_pipe:
                    bez_l = item.bezeichnung.lower()
                    wg_l  = item.warengruppe.lower()
                    if "hb-rohr" in bez_l:
                        sc -= 30
                        _bd.append((-30, "HB-Rohr ist kein Metallrohr"))
                    elif "rohr" in bez_l and any(m in bez_l for m in
                            ("alu", "aluminium", "stahl", "steel")):
                        sc += 22
                        _bd.append((22, "Alu/Stahl-Rohr (Metallrohr-Anfrage)"))
                    elif any(kw in wg_l for kw in ("traverse", "truss", "rigging")):
                        sc -= 30
                        _bd.append((-30, f"Traverse/Rigging bei Metallrohr-Anfrage ({item.warengruppe})"))

                # Boxcorner: Query sucht explizit ein Eck-Element → boosten
                if is_boxcorner:
                    art_lower_bc = item.bezeichnung.lower()
                    _corner_kws = ("boxcorner", "eckelement", "eckverbinder",
                                   "winkel", "l-ecke", "l90", "90°", "90 grad", "corner 90",
                                   "corner90", "winkelecke")
                    _is_corner_art = any(kw in art_lower_bc for kw in _corner_kws)
                    _is_tcorner_art = any(kw in art_lower_bc
                                          for kw in ("t-stück", "t stück", "3weg", "3-weg",
                                                     "t-ecke", "t ecke"))
                    if _is_corner_art:
                        sc += 35
                        _bd.append((35, f"Boxcorner/90°-Eckartikel ({item.bezeichnung[:30]})"))
                        # 90°-Anfrage: T-Corner (3-Weg) ist falsch → abwerten
                        if _is_90_corner and _is_tcorner_art:
                            sc -= 30
                            _bd.append((-30, "T-Corner (3-Weg) bei 90°-Anfrage (L-Ecke gesucht)"))
                    elif traverse:
                        sc -= 30  # Normale Traverse ist falsch für Boxcorner
                        _bd.append((-30, "Nicht-Boxcorner bei Boxcorner-Anfrage"))

                # Cabling: Verbrauchsartikel penalisieren (gehört nicht zu Pauschalen)
                if is_verkabelung and "verbrauch" in item.bezeichnung.lower():
                    sc -= 30
                    _bd.append((-30, "Verbrauchsartikel (kein Pauschalartikel)"))

            # Breakdown speichern (für GUI-Anzeige)
            if isinstance(item, Article):
                _breakdowns[idx] = {
                    "query_de": query_de,
                    "search_text": self._corpus[idx][:100],
                    "detected": _bd_detected,
                    "scores": _bd,
                }
            rescored.append((sc, idx))

        rescored.sort(reverse=True)

        for score, idx in rescored:
            item = self._pool[idx]
            if item.display_id not in seen:
                results.append((score, item, "fuzzy", _breakdowns.get(idx, {})))
                seen.add(item.display_id)
            if len(results) >= limit + 10:
                break

        results.sort(key=lambda x: x[0], reverse=True)

        # Alternativen-Qualität: bevorzuge gleiche Warengruppe für Positionen 2-N
        # Damit zeigen Alternativen relevante Artikel statt zufällig höher scorende
        if limit > 1 and len(results) > 1:
            _best_wg = getattr(results[0][1], "warengruppe", None)
            if _best_wg:
                _same_wg = [r for r in results[1:] if getattr(r[1], "warengruppe", None) == _best_wg]
                _diff_wg = [r for r in results[1:] if getattr(r[1], "warengruppe", None) != _best_wg]
                results = results[:1] + (_same_wg + _diff_wg)[: limit - 1]
            else:
                results = results[:limit]
        else:
            results = results[:limit]

        return [
            (MatchResult(
                matched=item,
                score=round(min(score, 99.0), 1),
                method=method,
                confident=score >= HIGH_SCORE,
                breakdown=bd,
            ), item)
            for score, item, method, bd in results
        ]

    def suggest_bundle_articles(self, query: str,
                                min_score: float = 88.0) -> list["Article"]:
        """Gibt alle Artikel einer bekannten Sektion zurück wenn Score ≥ min_score.

        Nutzt section_articles aus mappings.json. Wenn eine Position aus einem
        vergangenen Job bekannt ist (z.B. '4-point truss 300mm silver') und dort
        mehrere Artikel geplant wurden, werden alle zurückgegeben (z.B. Traverse
        + Konus + Abstandhalter).
        Gibt leere Liste zurück wenn kein sicherer Multi-Artikel-Treffer vorliegt.
        """
        if not self._section_map:
            return []

        query_de = translate_en_de(query)
        section_keys = list(self._section_map.keys())
        section_labels_clean = [
            re.sub(r'^\d{2}\.\d{2}\.\d{2}\s+', '', s) for s in section_keys
        ]

        best_score = 0.0
        best_nums: list[str] = []
        for q in dict.fromkeys([query, query_de]):
            hits = process.extract(
                q, section_labels_clean,
                scorer=fuzz.token_sort_ratio, limit=1,
            )
            if hits:
                _, score, idx = hits[0]
                if score > best_score:
                    best_score = score
                    best_nums = self._section_map[section_keys[idx]]

        if best_score >= min_score and len(best_nums) > 1:
            result = []
            for num in best_nums:
                pidx = self._num_to_idx.get(num)
                if pidx is not None:
                    result.append(self.articles[pidx])
            return result
        return []

    def best_match(self, query: str,
                   category_path: list[str] | None = None,
                   qty: float = 0.0, unit: str = "",
                   long_text: str = "") -> MatchResult:
        hits = self.match(query, limit=1, category_path=category_path,
                          qty=qty, unit=unit, long_text=long_text)
        if not hits:
            return MatchResult(matched=None, score=0.0, method="none", confident=False)
        return hits[0][0]

    def search(self, query: str, limit: int = 10,
               only_articles: bool = False,
               only_resources: bool = False) -> list[Matchable]:
        """Freie Suche für den Override-Dialog."""
        query_de = translate_en_de(query)
        pool = self._pool
        corpus = self._bezeichnung
        if only_articles:
            pool = self.articles
            corpus = [a.display_name for a in self.articles]
        elif only_resources:
            pool = self.resources
            corpus = [r.display_name for r in self.resources]

        # WRatio kombiniert mehrere Strategien → bessere Treffer, weniger unpassende Zufallstreffer
        # Mindest-Score 38%: verhindert komplett unpassende Artikel im Dropdown
        hits = process.extract(query_de, corpus, scorer=fuzz.WRatio, limit=limit * 4)
        return [pool[idx] for _, sc, idx in hits if sc >= 38][:limit]


# ─── Claude-Fallback ──────────────────────────────────────────────────────────

def make_article_from_ej(item: dict, details: dict | None = None,
                         local_matcher: "UnifiedMatcher | None" = None) -> "Article":
    """Konvertiert ein EJ-API-Dict in eine Article-Instanz.

    Wenn die Artikelnummer im lokalen Pool vorhanden ist, wird der lokale
    Artikel zurückgegeben (hat vollständige Stammdaten inkl. Preis).
    """
    nummer = str(item.get("Number", "")).strip()
    if local_matcher and nummer:
        local_idx = local_matcher._num_to_idx.get(nummer)
        if local_idx is not None:
            return local_matcher._pool[local_idx]
    inv     = int(details.get("RentalInventory", 0)) if details else 0
    comment = (details.get("Comment", "") or "").strip() if details else ""
    return Article(
        nummer=nummer,
        bezeichnung=item.get("Caption", ""),
        warengruppe=item.get("Category", ""),
        mutterwarengruppe=item.get("CategoryParent", ""),
        artikelart="",
        hersteller="",
        detail=comment,
        kommentar="",
        mietpreis=0.0,
        einheit="Tag",
        mietinventar=inv,
        gaeb_synonyms=[],
    )


async def match_with_claude(query: str, candidates: list[Matchable],
                            api_key: str) -> MatchResult:
    import anthropic

    def _label(i: int, m: Matchable) -> str:
        if isinstance(m, Article):
            return f"{i+1}. [{m.nummer}] {m.bezeichnung} ({m.artikelart}, {m.hersteller})"
        return f"{i+1}. [{m.display_id}] {m.funktion} ({m.ressourcenart})"

    candidate_text = "\n".join(_label(i, c) for i, c in enumerate(candidates))
    prompt = f"""Du bist ein Experte für Veranstaltungstechnik.

GAEB-Position: "{query}"

Kandidaten (Artikel und Ressourcen):
{candidate_text}

Welche Nummer passt am besten? Antworte nur mit der Ziffer (1-{len(candidates)}) oder 0 wenn keiner passt."""

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        idx = int(message.content[0].text.strip()) - 1
        if 0 <= idx < len(candidates):
            return MatchResult(matched=candidates[idx], score=80.0,
                               method="claude", confident=True)
    except (ValueError, IndexError):
        pass
    return MatchResult(matched=None, score=0.0, method="claude", confident=False)
