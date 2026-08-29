"""Personalplanung (Crew-Matrix) — Datenmodell und Rechnung.

Eine Zeile ist eine Ressource (EJ-Ressourcenfunktion), eine Spalte ein Kalendertag,
eine Zelle die Personenzahl an diesem Tag. Die Manntage einer Zeile sind damit die
Summe ihrer Zellen — anders als in der bisherigen Excel, wo die Summenformel den
Tagesbereich noch einmal getrennt benannte und dabei danebenlag (siehe
``docs/PERSONALPLANUNG.md``, Abschnitt 1).

Bewusst ohne Web-, DB- und Easyjob-Abhängigkeiten: ``routes/crew.py`` bedient die
Oberfläche, ``db.py`` speichert, hier wird nur gerechnet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

# Obergrenze für die Zeitachse. Ausschreibungen laufen über Wochen, nicht über Jahre;
# ein vertipptes Enddatum ("2036") würde sonst 3600 Spalten rendern.
MAX_DAYS = 400
MAX_PERSONS = 99

# Standardsätze, wie sie in den bisherigen Excel-Listen stehen.
DEFAULT_HOTEL_SATZ = 150.0

# Hotel und Reisekosten laufen auf eigene Ressourcen. Im Stamm stehen mehrere mit
# „Hotel" im Namen; gebucht wird auf „Hotelkosten" (37) — das ist die, die im
# Testsystem tatsächlich benutzt wird (rund 2.000 Buchungen über zwei Jahre gegenüber
# gut 800 auf „Hotelkosten eigenes Personal" und ebenso vielen auf die Fassung für
# freie Mitarbeiter).
DEFAULT_HOTEL_ID = 37
DEFAULT_HOTEL_NAME = "Hotelkosten"
DEFAULT_RK_ID = 126
DEFAULT_RK_NAME = "Reisekosten Pauschal"

# Spesen kommen aus einem Arbeitsmittel des Easyjob-Stamms — dort steht je Land ein
# Satz („Spesensatz Inland" 32 €, „Spesensatz Schweiz" 62 € …). Gewählt wird er
# einmal je Planung, nicht je Zeile: es ist eine Eigenschaft der Reise, nicht der
# Person. Der volle Satz gilt für Tage mit Übernachtung, die Hälfte für Tage ohne —
# genau das Paar 32/16, das in der Schneider-Liste von Hand gesetzt war.
DEFAULT_SPESEN_ID = 123
DEFAULT_SPESEN_NAME = "Spesensatz Inland"
DEFAULT_SPESEN_SATZ = 32.0

# Reisekosten: ein halber Tagessatz der jeweiligen Ressource. Ein Rigger reist
# günstiger als ein Lichtdesigner, und beides steht schon im Stamm.
DEFAULT_RK_FAKTOR = 0.5

PHASE_NAMES = ("Aufbau", "Proben", "Veranstaltung", "Abbau")


def parse_day(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def parse_number(text: str) -> float:
    """Zahl aus einer Eingabe lesen — deutsch und englisch geschrieben.

    Die Tücke ist der Punkt: „1.250" meint tausendzweihundertfünfzig, „520.50" meint
    fünfhundertzwanzig fünfzig. Beides kommt vor, weil die Tastatur auf dem Zehnerblock
    einen Punkt hat. Regel: sind beide Trenner da, ist der hintere der Dezimaltrenner.
    Steht nur ein Punkt und danach genau drei Ziffern, ist es ein Tausenderpunkt —
    sonst ein Dezimalpunkt. Ein Tagessatz von 1,25 € gibt es nicht, 1.250 € schon.
    """
    s = str(text or "").strip().replace("€", "").replace(" ", "").replace(" ", "")
    if not s:
        return 0.0
    neg = s.startswith("-")
    s = s.lstrip("+-")

    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")      # 1.250,50
        else:
            s = s.replace(",", "")                        # 1,250.50
    elif "," in s:
        s = s.replace(",", ".") if s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")                            # 1.250.000
    elif "." in s:
        ganz, _, rest = s.rpartition(".")
        if len(rest) == 3 and ganz:
            s = ganz + rest                               # 1.250 → Tausenderpunkt
    value = float(s)
    return -value if neg else value


def format_number(value: float) -> str:
    """Zahl fürs Eingabefeld: deutsch, ohne unnötige Nullen (520,5 · 520 · 1250)."""
    text = f"{float(value or 0):.2f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def day_key(value: date) -> str:
    return value.isoformat()


# Einheiten, bei denen die LV-Menge Manntage sind. „psch" hat keine Mengenaussage,
# „h" ist eine andere Größe — beides wird nicht übernommen, sondern von Hand geplant.
_TAGES_EINHEITEN = ("d", "day", "days", "tag", "tage", "td", "mt", "at", "pt")


def _manntage_aus_lv(kandidat: dict) -> int:
    """Manntage, die eine LV-Position fordert — 0, wenn sie keine Aussage dazu macht."""
    einheit = (kandidat.get("unit") or "").strip().lower().rstrip(".")
    if einheit not in _TAGES_EINHEITEN:
        return 0
    try:
        menge = float(kandidat.get("qty") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(int(round(menge)), MAX_PERSONS))


def _tag_vor(iso: str) -> str:
    return day_key(parse_day(iso) - timedelta(days=1))


def _tag_nach(iso: str) -> str:
    return day_key(parse_day(iso) + timedelta(days=1))


@dataclass
class Phase:
    """Ein benannter Abschnitt der Zeitachse. ``default_item_id`` ist die
    LV-Position, die neuen Tagen in dieser Phase vorgeschlagen wird (Stufe 2)."""
    name: str
    day_from: str
    day_to: str
    default_item_id: str = ""

    def contains(self, day: str) -> bool:
        return bool(self.day_from) and self.day_from <= day <= self.day_to

    def to_dict(self) -> dict:
        return {"name": self.name, "from": self.day_from, "to": self.day_to,
                "default_item_id": self.default_item_id}

    @classmethod
    def from_dict(cls, d: dict) -> "Phase":
        return cls(name=str(d.get("name") or ""),
                   day_from=str(d.get("from") or ""),
                   day_to=str(d.get("to") or ""),
                   default_item_id=str(d.get("default_item_id") or ""))


@dataclass
class Segment:
    """Ein zusammenhängender Tagesblock einer Zeile, der auf eine LV-Position läuft.
    Nur zur Anzeige und zum Speichern — gerechnet wird tageweise (siehe CrewRow.assign)."""
    day_from: str
    day_to: str
    item_id: str

    @property
    def days(self) -> int:
        return (parse_day(self.day_to) - parse_day(self.day_from)).days + 1


@dataclass
class CrewRow:
    """Eine Ressourcenzeile. Die Sätze werden beim Anlegen aus dem Personalstamm
    vorbelegt und sind danach entkoppelt: eine spätere Preispflege in Easyjob darf
    eine bereits abgegebene Kalkulation nicht rückwirkend verändern.

    ``assign`` hält die Positions-Zuordnung **je Tag**, nicht als Blöcke. Blöcke
    wären beim Anzeigen und Speichern kompakter, beim Ändern aber ein Ärgernis:
    jedes Umhängen eines einzelnen Tages müsste Blöcke aufteilen, kürzen und wieder
    zusammenführen. Tageweise ist die Zuordnung so einfach wie die Besetzung selbst;
    ``segments()`` fasst sie fürs Band und die Speicherung wieder zusammen.
    """
    id: int
    label: str
    resource_id: int
    # Menüpunkt, unter dem die Zeile in der Matrix steht — item_id einer LV-Position
    # oder "eigen:N" für einen selbst angelegten. Leer = noch nicht einsortiert.
    # Das ersetzt das frühere freie Gewerk-Feld: die Gliederung folgt jetzt der
    # Struktur, die auch das Angebot hat, statt einer zweiten daneben.
    group_key: str = ""
    tagessatz: float = 0.0
    eigenkosten: float = 0.0
    hotel_naechte: int = 0
    # Preis je Nacht für genau diese Zeile. 0 heißt: der Preis der Planung gilt —
    # meist übernachtet die Crew im selben Haus. Wer einzeln abweicht (der Regisseur
    # im teureren Hotel, ein Kollege privat untergebracht), trägt es hier ein, ohne
    # dass die übrigen Zeilen davon wissen müssen.
    hotel_satz: float = 0.0
    rk_anzahl: int = 0
    # Preis je Reise für genau diese Zeile. 0 heißt: ein halber Tagessatz der Zeile —
    # ein Rigger reist günstiger als ein Lichtdesigner, und das steht schon im Stamm.
    # Eingetragen wird hier nur, was davon abweicht (Flug statt Bahn etwa).
    rk_satz: float = 0.0
    sort_order: int = 0
    cells: dict[str, int] = field(default_factory=dict)    # ISO-Datum → Personen
    assign: dict[str, str] = field(default_factory=dict)   # ISO-Datum → item_id

    @property
    def manntage(self) -> int:
        return sum(self.cells.values())

    def segments(self, days: list[str] | None = None) -> list[Segment]:
        """Zuordnung als zusammenhängende Blöcke, in Tagesreihenfolge.

        ``days`` begrenzt auf die Zeitachse (und bestimmt, was „benachbart" heißt):
        zwei Tage mit derselben Position, zwischen denen ein Tag ohne Zuordnung
        liegt, bleiben zwei Blöcke.
        """
        keys = days if days is not None else sorted(self.assign)
        out: list[Segment] = []
        vorher: str | None = None
        for day in keys:
            item = self.assign.get(day)
            if item and vorher == item:
                out[-1].day_to = day
            elif item:
                out.append(Segment(day_from=day, day_to=day, item_id=item))
            vorher = item
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label, "resource_id": self.resource_id,
            "group_key": self.group_key, "tagessatz": self.tagessatz,
            "eigenkosten": self.eigenkosten,
            "hotel_naechte": self.hotel_naechte, "hotel_satz": self.hotel_satz,
            "rk_anzahl": self.rk_anzahl, "rk_satz": self.rk_satz,
            "sort_order": self.sort_order, "cells": dict(self.cells),
            "assign": dict(self.assign),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CrewRow":
        cells = {}
        for k, v in (d.get("cells") or {}).items():
            n = int(v or 0)
            if n > 0:
                cells[str(k)] = min(n, MAX_PERSONS)
        assign = {str(k): str(v) for k, v in (d.get("assign") or {}).items() if v}
        return cls(
            id=int(d.get("id") or 0),
            label=str(d.get("label") or ""),
            resource_id=int(d.get("resource_id") or 0),
            group_key=str(d.get("group_key") or ""),
            tagessatz=float(d.get("tagessatz") or 0),
            eigenkosten=float(d.get("eigenkosten") or 0),
            hotel_naechte=int(d.get("hotel_naechte") or 0),
            hotel_satz=max(0.0, float(d.get("hotel_satz") or 0)),
            rk_satz=max(0.0, float(d.get("rk_satz") or 0)),
            rk_anzahl=int(d.get("rk_anzahl") or 0),
            sort_order=int(d.get("sort_order") or 0),
            cells=cells,
            assign=assign,
        )


@dataclass
class CrewPlan:
    date_from: str
    date_to: str
    phases: list[Phase] = field(default_factory=list)
    rows: list[CrewRow] = field(default_factory=list)
    hotel_satz: float = DEFAULT_HOTEL_SATZ
    # Der gewählte Spesensatz (Arbeitsmittel aus dem Stamm) samt Bezeichnung, damit
    # im PDF steht, welches Land gerechnet wurde.
    spesen_id: int = DEFAULT_SPESEN_ID
    spesen_name: str = DEFAULT_SPESEN_NAME
    spesen_satz: float = DEFAULT_SPESEN_SATZ
    hotel_id: int = DEFAULT_HOTEL_ID
    hotel_name: str = DEFAULT_HOTEL_NAME
    rk_id: int = DEFAULT_RK_ID
    rk_name: str = DEFAULT_RK_NAME
    rk_faktor: float = DEFAULT_RK_FAKTOR
    # Je Abschnitt: LV-Position, auf die seine Nebenkosten laufen. Fehlt sie, werden
    # sie anteilig nach Manntagen auf die Positionen der Zeile verteilt.
    menu_nk_pos: dict[str, str] = field(default_factory=dict)
    next_row_id: int = 1
    # Positionen und Menüpunkte in Anzeigereihenfolge. Der Inhalt kommt aus zwei
    # Quellen: Positionen, deren Match eine Personal-Ressource ist, wandern
    # automatisch hier hinein (sync_positions) — und zwar bei jedem Aufbau der
    # Ansicht, damit die Liste dem Matching folgt statt auf einen Knopfdruck zu
    # warten. Dazu kommen von Hand dazugelegte Positionen und eigene Menüpunkte,
    # die in `manual` stehen und vom Abgleich unberührt bleiben.
    positions: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    # Ressourcen, deren Zeile jemand gelöscht hat. Ohne dieses Gedächtnis würde der
    # Abgleich sie beim nächsten Aufbau der Ansicht wieder herbeizaubern.
    dismissed: list[int] = field(default_factory=list)
    # Selbst angelegte Menüpunkte: "eigen:N" → Titel. Sie stehen mit in `positions`
    # und verhalten sich wie eine Pauschalposition ohne LV-Bezug — für Personal, das
    # das LV nicht als eigene Position führt.
    custom_titles: dict[str, str] = field(default_factory=dict)
    next_custom: int = 1
    # Standard-Position je Abschnitt: Schlüssel → item_id. Wer dort Manntage einträgt,
    # ordnet sie damit ohne weiteren Klick dieser Position zu. Eine Position darf in
    # mehreren Abschnitten stehen (etwa eine Installations-Pauschale, die für Licht
    # UND Ton zuständig ist) — je Abschnitt ist es aber genau eine: mehrere wären
    # nicht eindeutig, und die erste hätte ohnehin immer gewonnen.
    menu_positions: dict[str, str] = field(default_factory=dict)
    # item_id → 'batch' für Sammelpositionen, die an den Menüpunkt darüber angehängt
    # werden. 'menu' ist der Standard und wird nicht gespeichert.
    pos_modes: dict[str, str] = field(default_factory=dict)

    # ── Zeitachse ────────────────────────────────────────────────────────────

    def days(self) -> list[date]:
        try:
            start, end = parse_day(self.date_from), parse_day(self.date_to)
        except (ValueError, TypeError):
            return []
        if end < start:
            start, end = end, start
        span = (end - start).days + 1
        return [start + timedelta(days=i) for i in range(min(span, MAX_DAYS))]

    def day_keys(self) -> list[str]:
        return [day_key(d) for d in self.days()]

    def phase_of(self, day: str) -> Phase | None:
        for p in self.phases:
            if p.contains(day):
                return p
        return None

    def set_range(self, date_from: str, date_to: str) -> None:
        """Zeitachse ändern.

        Zellen außerhalb des neuen Bereichs bleiben erhalten, werden aber nicht mehr
        angezeigt und nicht mitgerechnet — wer den Bereich aus Versehen zu eng zieht,
        verliert dadurch keine Eingaben.

        Die Phasen ziehen mit: sie werden auf den neuen Bereich beschnitten, und was
        ganz herausfällt, verschwindet. Ohne das zeigen sie nach einer Korrektur des
        Zeitraums auf Tage, die es nicht mehr gibt — „Phase füllen" täte dann
        stillschweigend nichts, und im Kopf der Matrix stünde kein Phasenband.
        """
        self.date_from, self.date_to = date_from, date_to
        keys = self.day_keys()
        if not keys:
            return
        erster, letzter = keys[0], keys[-1]
        behalten: list[Phase] = []
        for ph in self.phases:
            if not ph.day_from or ph.day_to < erster or ph.day_from > letzter:
                continue                      # liegt komplett außerhalb
            ph.day_from = max(ph.day_from, erster)
            ph.day_to = min(ph.day_to, letzter)
            behalten.append(ph)
        # Bleibt nichts übrig, ist ein frischer Vorschlag hilfreicher als kein Band.
        self.phases = behalten or default_phases(erster, letzter)

    # ── Zeilen ───────────────────────────────────────────────────────────────

    def row(self, row_id: int) -> CrewRow | None:
        return next((r for r in self.rows if r.id == row_id), None)

    def add_row(self, label: str, resource_id: int, group_key: str = "",
                tagessatz: float = 0.0, eigenkosten: float = 0.0) -> CrewRow:
        row = CrewRow(
            id=self.next_row_id, label=label, resource_id=int(resource_id),
            group_key=group_key, tagessatz=float(tagessatz or 0),
            eigenkosten=float(eigenkosten or 0),
            sort_order=(max((r.sort_order for r in self.rows), default=0) + 1),
        )
        self.next_row_id += 1
        self.rows.append(row)
        return row

    def remove_row(self, row_id: int) -> bool:
        """Zeile entfernen und die Ressource als abgewählt merken — sonst legt der
        Abgleich mit dem Matching sie sofort wieder an."""
        row = self.row(row_id)
        if row is None:
            return False
        if row.resource_id and row.resource_id not in self.dismissed:
            self.dismissed.append(row.resource_id)
        self.rows = [r for r in self.rows if r.id != row_id]
        return True

    def set_cell(self, row_id: int, day: str, persons: int) -> bool:
        row = self.row(row_id)
        if row is None:
            return False
        n = max(0, min(int(persons or 0), MAX_PERSONS))
        if n:
            row.cells[day] = n
        else:
            # Ohne Besetzung keine Zuordnung: die Farbe der Zelle zeigt, auf welche
            # Position die Manntage laufen — bei null Manntagen gibt es nichts zu
            # zeigen, und eine stehengebliebene Farbe würde etwas behaupten, was
            # nicht mehr stimmt.
            row.cells.pop(day, None)
            row.assign.pop(day, None)
        return True

    def menu_keys(self) -> list[str]:
        """Die Menüpunkte in Anzeigereihenfolge — Sammelpositionen zählen nicht dazu."""
        return [k for k in self.positions if self.pos_mode(k) == "menu"]

    def home_of(self, row: CrewRow) -> str:
        """Menüpunkt einer Zeile. Zeigt sie auf etwas, das es nicht mehr gibt (oder
        auf eine zur Sammelposition gewordene Position), gilt sie als nicht
        einsortiert — besser sichtbar oben als unsichtbar verschwunden."""
        return row.group_key if row.group_key in set(self.menu_keys()) else ""

    def sorted_rows(self) -> list[CrewRow]:
        order = {k: i for i, k in enumerate(self.menu_keys())}
        # Nicht einsortierte Zeilen zuerst: sie brauchen Aufmerksamkeit.
        return sorted(self.rows,
                      key=lambda r: (order.get(self.home_of(r), -1), r.sort_order))

    def groups(self) -> list[tuple[str, list[CrewRow]]]:
        """Zeilen als [(Menüpunkt-Key, Zeilen)] in Anzeigereihenfolge. Menüpunkte ohne
        Zeilen kommen mit — sonst könnte man dort keine ablegen."""
        nach_key: dict[str, list[CrewRow]] = {}
        for row in self.sorted_rows():
            nach_key.setdefault(self.home_of(row), []).append(row)
        out: list[tuple[str, list[CrewRow]]] = []
        if nach_key.get(""):
            out.append(("", nach_key[""]))
        for key in self.menu_keys():
            out.append((key, nach_key.get(key, [])))
        return out

    # ── Eigene Menüpunkte ────────────────────────────────────────────────────

    def add_custom_menu(self, title: str) -> str:
        key = f"eigen:{self.next_custom}"
        self.next_custom += 1
        self.custom_titles[key] = (title or "Ohne Titel").strip()[:80]
        self.positions.append(key)
        return key

    def rename_menu(self, key: str, title: str) -> bool:
        if key not in self.custom_titles:
            return False
        self.custom_titles[key] = (title or "Ohne Titel").strip()[:80]
        return True

    def menu_pos(self, key: str) -> str:
        """Standard-Position eines Abschnitts — leer, wenn keine oder wenn die
        eingetragene nicht mehr zur Planung gehört."""
        ziel = self.menu_positions.get(key, "")
        return ziel if self.covers(ziel) else ""

    def set_menu_pos(self, key: str, item_id: str) -> bool:
        """Standard-Position setzen; leeres ``item_id`` nimmt sie wieder heraus."""
        if key not in self.menu_keys():
            return False
        if not item_id:
            self.menu_positions.pop(key, None)
            return True
        if not self.covers(item_id):
            return False
        self.menu_positions[key] = item_id
        return True

    def default_pos_for(self, row: CrewRow) -> str:
        """Position, auf die neue Manntage dieser Zeile ohne weiteres Zutun laufen.

        Zwei Quellen, in dieser Reihenfolge:

        1. Die Standard-Position des Abschnitts, in dem die Zeile steht.
        2. Die Position, aus der die Zeile entstanden ist (``group_key``). Kommt eine
           Ressource aus dem Matching, ist sie damit von Anfang an ihrer LV-Position
           zugeordnet — auch bevor irgendein Abschnitt angelegt wurde. Ohne das müsste
           man erst gliedern, bevor man die erste Zahl eintippen kann.
        """
        ziel = self.menu_pos(self.home_of(row))
        if ziel:
            return ziel
        return row.group_key if self.covers(row.group_key) else ""

    def is_custom(self, key: str) -> bool:
        return key.startswith("eigen:")

    def menu_title(self, key: str) -> str:
        return self.custom_titles.get(key, "")

    # ── Rechnung ─────────────────────────────────────────────────────────────

    def manntage(self, row: CrewRow) -> int:
        """Nur Tage innerhalb der Zeitachse zählen — sonst würde eine nachträglich
        verkürzte Zeitachse unsichtbare Manntage weiterbezahlen."""
        keys = set(self.day_keys())
        return sum(n for d, n in row.cells.items() if d in keys)

    def naechte(self, row: CrewRow) -> int:
        """Hotelnächte, gedeckelt auf die Manntage. Mehr Nächte als Einsatztage sind
        eine Fehleingabe (in der Excel kam das durch zusammengeführte Zeilen vor) und
        würden die Spesen ins Negative rechnen."""
        return max(0, min(row.hotel_naechte, self.manntage(row)))

    def row_spesen(self, row: CrewRow) -> float:
        """Voller Satz für Tage mit Übernachtung, halber für Tage ohne.

        Das bildet die Zwölf-Stunden-Pauschale ab: wer auswärts übernachtet, ist den
        ganzen Tag unterwegs; wer abends heimfährt, bekommt den halben Satz. In der
        Schneider-Liste stand dieselbe Unterscheidung als 32 gegen 16, nur von Hand
        je Zeile gewählt.
        """
        mt, n = self.manntage(row), self.naechte(row)
        return (mt - n) * (self.spesen_satz / 2) + n * self.spesen_satz

    def hotel_satz_of(self, row: CrewRow) -> float:
        """Preis je Nacht für diese Zeile — ihr eigener, sonst der der Planung."""
        return row.hotel_satz if row.hotel_satz > 0 else self.hotel_satz

    def row_hotel(self, row: CrewRow) -> float:
        return self.naechte(row) * self.hotel_satz_of(row)

    def rk_satz_of(self, row: CrewRow) -> float:
        """Preis je Reise für diese Zeile — ihr eigener, sonst ein halber Tagessatz."""
        return row.rk_satz if row.rk_satz > 0 else row.tagessatz * self.rk_faktor

    def row_rk(self, row: CrewRow) -> float:
        return row.rk_anzahl * self.rk_satz_of(row)

    def row_nebenkosten(self, row: CrewRow) -> float:
        """Spesen, Hotel und Reisekosten — alles, was nicht am Tagessatz hängt."""
        return self.row_spesen(row) + self.row_hotel(row) + self.row_rk(row)

    def row_total(self, row: CrewRow) -> float:
        return self.manntage(row) * row.tagessatz + self.row_nebenkosten(row)

    def nk_pos_for(self, row: CrewRow) -> str:
        """Position, auf die die Nebenkosten dieser Zeile laufen — leer heißt
        anteilig nach Manntagen wie die Tageskosten."""
        ziel = self.menu_nk_pos.get(self.home_of(row), "")
        return ziel if self.covers(ziel) else ""

    def set_nk_pos(self, key: str, item_id: str) -> bool:
        """Nebenkosten eines Abschnitts auf eine Position lenken; leeres ``item_id``
        stellt auf anteilig zurück."""
        if key not in self.menu_keys():
            return False
        if not item_id:
            self.menu_nk_pos.pop(key, None)
            return True
        if not self.covers(item_id):
            return False
        self.menu_nk_pos[key] = item_id
        return True

    def row_costs(self, row: CrewRow) -> float:
        """Eigenkosten — die Gegenrechnung zum Weiterbelasten (EJ: TotalCosts)."""
        return self.manntage(row) * row.eigenkosten

    # ── Positions-Zuordnung ──────────────────────────────────────────────────

    def assign_days(self, row_id: int, day_from: str, day_to: str,
                    item_id: str) -> int:
        """Tagesbereich einer Zeile auf eine LV-Position legen. Leeres ``item_id``
        hebt die Zuordnung auf. Gibt die Anzahl geänderter Tage zurück.

        Zugeordnet werden auch unbesetzte Tage: wer erst den Block malt und dann die
        Zahlen tippt, soll nicht zweimal arbeiten. Gerechnet wird nur, was besetzt ist.
        """
        row = self.row(row_id)
        if row is None:
            return 0
        if day_to < day_from:
            day_from, day_to = day_to, day_from
        n = 0
        for day in self.day_keys():
            if not (day_from <= day <= day_to):
                continue
            if item_id:
                if row.assign.get(day) != item_id:
                    row.assign[day] = item_id
                    n += 1
            elif row.assign.pop(day, None) is not None:
                n += 1
        return n

    def fill_cells(self, row_ids: list[int], day_from: str, day_to: str,
                   persons: int, item_id: str | None = None) -> int:
        """Einen Bereich (Zeilen × Tage) mit derselben Personenzahl belegen.

        Für den häufigsten Handgriff der Planung: „drei Rigger, die ganze Aufbauwoche".
        Einzeln getippt sind das fünf Zellen und fünf Anfragen; hier ist es eine.

        ``item_id`` bestimmt die Zuordnung der gefüllten Tage:

        * ein Wert → alle Zeilen laufen auf diese Position (jemand hat oben eine
          ausdrücklich gewählt),
        * ``None`` → **jede Zeile bekommt ihre eigene** Standardposition. Über mehrere
          Zeilen hinweg die der ersten zu nehmen wäre falsch: ein Rigger und ein
          Lichttechniker gehören selten auf dieselbe Position,
        * ``""`` → keine Zuordnung.

        Gibt die Anzahl geänderter Zellen zurück.
        """
        if day_to < day_from:
            day_from, day_to = day_to, day_from
        tage = [d for d in self.day_keys() if day_from <= d <= day_to]
        n = 0
        for rid in row_ids:
            row = self.row(rid)
            if row is None:
                continue
            ziel = self.default_pos_for(row) if item_id is None else item_id
            for day in tage:
                if self.set_cell(rid, day, persons):
                    n += 1
                if persons and ziel:
                    row.assign[day] = ziel
                elif not persons:
                    # Geleerte Zelle braucht keine Zuordnung mehr — sonst bliebe eine
                    # Farbe ohne Besetzung stehen.
                    row.assign.pop(day, None)
        return n

    def assign_open(self, item_id: str, row_id: int = 0) -> int:
        """Alle besetzten, aber noch nicht zugeordneten Tage auf eine Position legen.

        Damit lässt sich erst die Besetzung tippen und die Zuordnung nachreichen —
        das ist die natürliche Reihenfolge, wenn man beim Planen noch nicht weiß, auf
        welche Position eine Woche am Ende läuft. Schon zugeordnete Tage bleiben
        unberührt: eine bewusst gesetzte Ausnahme darf ein späteres Nachziehen nicht
        wieder einsammeln. ``row_id=0`` nimmt alle Zeilen.
        """
        n = 0
        for row in self.rows:
            if row_id and row.id != row_id:
                continue
            for day in self.day_keys():
                if row.cells.get(day) and not row.assign.get(day):
                    row.assign[day] = item_id
                    n += 1
        return n

    def fill_phase(self, row_id: int, phase_index: int, item_id: str) -> int:
        """Alle besetzten, aber noch nicht zugeordneten Tage einer Phase zuordnen.

        Das ist der Abkürzung-Knopf: in der Schneider-Liste folgt die Zuordnung in
        20 von 23 Fällen der Phase, angeklickt werden nur die Ausnahmen. Bereits
        zugeordnete Tage bleiben unberührt — eine bewusst gesetzte Ausnahme darf ein
        späteres „Phase füllen" nicht wieder einsammeln.
        """
        row = self.row(row_id)
        if row is None or not (0 <= phase_index < len(self.phases)):
            return 0
        ph = self.phases[phase_index]
        n = 0
        for day in self.day_keys():
            if not ph.contains(day) or not row.cells.get(day):
                continue
            if not row.assign.get(day):
                row.assign[day] = item_id
                n += 1
        return n

    def covers(self, item_id: str) -> bool:
        """Ist das ein gültiges Zuordnungsziel?

        Nur LV-Positionen. Ein selbst angelegter Menüpunkt ist eine Überschrift der
        Übersicht halber und hat kein Gegenstück im Leistungsverzeichnis — Zeiten
        darauf zu buchen ginge nirgendwo hin.
        """
        return item_id in self.positions and not self.is_custom(item_id)

    def target_keys(self) -> list[str]:
        """Alle Zuordnungsziele in Anzeigereihenfolge (ohne eigene Menüpunkte)."""
        return [k for k in self.positions if not self.is_custom(k)]

    def add_position(self, item_id: str, manual: bool = True) -> bool:
        """Position aufnehmen. ``manual=True`` schützt sie vor dem Abgleich mit dem
        Matching — sie bleibt also auch dann stehen, wenn dort keine Personal-Ressource
        hängt (etwa eine Pauschale, deren Personal man hier planen will)."""
        if not item_id:
            return False
        if manual and item_id not in self.manual:
            self.manual.append(item_id)
        if item_id in self.positions:
            return False
        self.positions.append(item_id)
        return True

    def sync_positions(self, auto_ids: list[str]) -> None:
        """Positionsliste an das Matching angleichen.

        ``auto_ids`` sind die LV-Positionen mit Personal-Match, in LV-Reihenfolge.
        Neue kommen dazu, weggefallene verschwinden — es sei denn, es hängen schon
        Zuordnungen daran oder jemand hat sie von Hand dazugelegt. Sonst würde ein
        geänderter Match stillschweigend zugeordnete Manntage mitnehmen.
        """
        belegt = {i for row in self.rows for i in row.assign.values()}
        geschuetzt = set(self.manual) | belegt
        auto = set(auto_ids)
        self.positions = [
            k for k in self.positions
            if k in auto or k in geschuetzt or self.is_custom(k)
        ]
        vorhanden = set(self.positions)
        # Neue vorne einsortieren, in LV-Reihenfolge — eigene Menüpunkte und von Hand
        # dazugelegte Positionen behalten ihren Platz dahinter.
        neu = [k for k in auto_ids if k not in vorhanden]
        if neu:
            eigene = [k for k in self.positions if self.is_custom(k) or k in self.manual]
            rest = [k for k in self.positions if k not in eigene]
            self.positions = rest + neu + eigene

    def sync_rows(self, candidates: list[dict]) -> int:
        """Für jede gematchte Personal-Ressource eine Zeile — automatisch.

        Eine Zeile je Ressource, nicht je Position: wer im Aufbau und in der Show
        arbeitet, steht einmal da. Gelöschte Ressourcen bleiben gelöscht (``dismissed``),
        vorhandene werden nicht angefasst — die Besetzung ist Handarbeit.
        """
        keys = self.day_keys()
        vorhanden = {r.resource_id for r in self.rows}
        neu = 0
        for c in candidates:
            rid = int(c["resource_id"])
            if rid in vorhanden or rid in self.dismissed:
                continue
            row = self.add_row(label=c.get("funktion") or c.get("label") or "",
                               resource_id=rid, group_key=c.get("item_id") or "",
                               tagessatz=c.get("tagessatz") or 0,
                               eigenkosten=c.get("eigenkosten") or 0)
            # Die im LV geforderte Menge gleich eintragen, auf den ersten Tag der
            # Zeitachse, und der Position zuordnen. Damit steht die Zeile nicht leer
            # da und der Soll/Ist-Abgleich stimmt vom ersten Moment an. Verteilt wird
            # von Hand — wo die Tage wirklich liegen, weiß das LV nicht, und eine
            # erfundene Verteilung wäre schwerer zu korrigieren als eine Zahl, die
            # sichtbar noch am falschen Platz steht.
            menge = _manntage_aus_lv(c)
            if menge and keys:
                self.set_cell(row.id, keys[0], menge)
                if c.get("item_id"):
                    row.assign[keys[0]] = c["item_id"]
            vorhanden.add(rid)
            neu += 1
        return neu

    def remove_position(self, item_id: str) -> int:
        """Position aus der Planung nehmen. Ihre Zuordnungen fallen mit weg — die
        betroffenen Tage stehen danach als „offen" da, statt still auf eine Position
        zu zeigen, die es nicht mehr gibt. Gibt die Anzahl gelöster Tage zurück."""
        if item_id in self.positions:
            self.positions.remove(item_id)
        if item_id in self.manual:
            self.manual.remove(item_id)
        for key in [k for k, v in self.menu_positions.items() if v == item_id]:
            self.menu_positions.pop(key, None)
        for key in [k for k, v in self.menu_nk_pos.items() if v == item_id]:
            self.menu_nk_pos.pop(key, None)
        self.pos_modes.pop(item_id, None)
        self.custom_titles.pop(item_id, None)
        n = 0
        for row in self.rows:
            if row.group_key == item_id:
                row.group_key = ""      # sonst zeigt sie ins Leere
        for row in self.rows:
            for day in [d for d, i in row.assign.items() if i == item_id]:
                del row.assign[day]
                n += 1
        return n

    def move_position(self, item_id: str, delta: int) -> bool:
        """Position in der Liste verschieben. Die Reihenfolge ist nicht Kosmetik:
        eine Sammelposition ('batch') hängt an dem Menüpunkt, der über ihr steht."""
        if item_id not in self.positions:
            return False
        i = self.positions.index(item_id)
        j = max(0, min(len(self.positions) - 1, i + delta))
        if i == j:
            return False
        self.positions.insert(j, self.positions.pop(i))
        return True

    def position_parent(self, item_id: str) -> str | None:
        """Der Menüpunkt, an dem eine Sammelposition hängt — der nächste über ihr in
        der Liste. Gibt es keinen, steht sie für sich; in Easyjob bekommt sie dann
        ihre eigene Gruppe (Stufe 3)."""
        if item_id not in self.positions or self.pos_mode(item_id) != "batch":
            return None
        for other in reversed(self.positions[:self.positions.index(item_id)]):
            if self.pos_mode(other) == "menu":
                return other
        return None

    # ── Phasen ───────────────────────────────────────────────────────────────

    def add_phase(self, name: str, day_from: str, day_to: str) -> bool:
        """Eigenen Termin anlegen. Phasen dürfen sich nicht überlappen — ein Tag
        gehört zu genau einer Phase, sonst wäre „Phase füllen" nicht eindeutig."""
        if not name.strip() or not day_from or not day_to:
            return False
        if day_to < day_from:
            day_from, day_to = day_to, day_from
        for ph in self.phases:
            if ph.day_from <= day_to and day_from <= ph.day_to:
                return False
        self.phases.append(Phase(name.strip()[:40], day_from, day_to))
        self.phases.sort(key=lambda p: p.day_from)
        return True

    def remove_phase(self, index: int) -> bool:
        if 0 <= index < len(self.phases):
            del self.phases[index]
            return True
        return False

    def set_phase(self, index: int, name: str | None = None,
                  day_from: str = "", day_to: str = "") -> bool:
        """Termin ändern. Überlappt der neue Zeitraum einen anderen, weichen dessen
        Grenzen — wer den Aufbau verlängert, verkürzt damit die Veranstaltung, statt
        eine Fehlermeldung zu bekommen."""
        if not (0 <= index < len(self.phases)):
            return False
        ph = self.phases[index]
        if name is not None and name.strip():
            ph.name = name.strip()[:40]
        if day_from:
            ph.day_from = day_from
        if day_to:
            ph.day_to = day_to
        if ph.day_to < ph.day_from:
            ph.day_from, ph.day_to = ph.day_to, ph.day_from
        for i, other in enumerate(self.phases):
            if i == index or not other.day_from:
                continue
            if other.day_from <= ph.day_to and ph.day_from <= other.day_to:
                if other.day_from < ph.day_from:
                    other.day_to = min(other.day_to, _tag_vor(ph.day_from))
                else:
                    other.day_from = max(other.day_from, _tag_nach(ph.day_to))
        self.phases = [p for p in self.phases if p.day_from <= p.day_to]
        self.phases.sort(key=lambda p: p.day_from)
        return True

    def default_mode(self, key: str) -> str:
        """Ein selbst angelegter Menüpunkt ist eine Überschrift, eine LV-Position
        zunächst nur ein Zuordnungsziel.

        Andersherum wäre die Matrix unbrauchbar: aus dem Matching kommen je LV
        Dutzende Personalpositionen, und jede als eigene Überschrift zu führen
        ergäbe eine Liste aus Überschriften ohne Inhalt. Wer eine Position zum
        Abschnitt machen will, sagt es per Rechtsklick.
        """
        return "menu" if self.is_custom(key) else "batch"

    def pos_mode(self, item_id: str) -> str:
        """'menu' = eigene Überschrift in der Matrix (und in Easyjob eine eigene
        Gruppe), 'batch' = reines Zuordnungsziel, das in die Gruppe des Menüpunkts
        darüber läuft."""
        return self.pos_modes.get(item_id) or self.default_mode(item_id)

    def set_pos_mode(self, item_id: str, mode: str) -> None:
        if mode == self.default_mode(item_id):
            self.pos_modes.pop(item_id, None)      # Standard nicht mitschleppen
        else:
            self.pos_modes[item_id] = mode
        # Wird eine LV-Position zum Abschnitt, ist sie auch das nächstliegende Ziel
        # für alles, was darunter eingetragen wird — sie ist ja diese Leistung. Ohne
        # das müsste man sie unmittelbar danach von Hand als Ziel setzen.
        if mode == "menu" and not self.is_custom(item_id) and not self.menu_pos(item_id):
            self.set_menu_pos(item_id, item_id)

    def nebenkosten_posten(self, row: CrewRow) -> list[tuple]:
        """Die Nebenkosten einer Zeile, fertig auf Positionen verteilt.

        Liefert ``(art, resource_id, item_id, menge, satz, bezeichnung)``. **Eine**
        Quelle für die Matrix und für die Buchung nach Easyjob: rechneten beide
        getrennt, zeigte die Matrix je Position etwas anderes an, als dort gebucht
        wird — und genau daraus entsteht der Einheitspreis.

        Zwei Feinheiten, die man leicht übersieht:

        * Verteilt wird die **Menge**, nicht der Betrag. ``DaysInAction`` hat in
          Easyjob zwei Nachkommastellen; ein anteiliger Betrag ließe sich dort gar
          nicht abbilden, und die Matrix zeigte eine Zahl, die nie gebucht wird.
        * Tage ohne Positionszuordnung bekommen **nichts** — auch nicht anteilig.
          Ihre Tageskosten werden ebenso wenig gebucht; sie stehen in der Matrix als
          offen, bis jemand sie zuordnet. Die Menge wird deshalb auf den
          zugeordneten Anteil heruntergerechnet, statt den Rest der größten Position
          zuzuschlagen.
        """
        mt = self.manntage(row)
        if not mt:
            return []
        n = self.naechte(row)
        posten = [
            ("spesen", self.spesen_id, mt - n, self.spesen_satz / 2,
             f"{row.label} · Spesen halber Satz"),
            ("spesen", self.spesen_id, n, self.spesen_satz,
             f"{row.label} · Spesen mit Übernachtung"),
            ("hotel", self.hotel_id, n, self.hotel_satz_of(row),
             f"{row.label} · Hotel"),
            ("reise", self.rk_id, row.rk_anzahl, self.rk_satz_of(row),
             f"{row.label} · Reisekosten"),
        ]
        posten = [x for x in posten if x[2] > 0 and x[3] > 0]
        if not posten:
            return []

        ziel = self.nk_pos_for(row)
        if ziel:
            # Ausdrücklich gelenkt: alles dorthin, unabhängig von der Zuordnung.
            anteile, quote = [(ziel, 1.0)], 1.0
        else:
            keys = set(self.day_keys())
            je_pos: dict[str, int] = {}
            for day, pers in row.cells.items():
                item = row.assign.get(day)
                if day in keys and item and not self.is_custom(item):
                    je_pos[item] = je_pos.get(item, 0) + pers
            if not je_pos:
                return []
            anteile = [(i, m / mt) for i, m in je_pos.items()]
            quote = sum(je_pos.values()) / mt

        out = []
        for kind, rid, menge, satz, label in posten:
            mengen = _runden([menge * q for _, q in anteile],
                             round(menge * quote, 2))
            for (item, _), m in zip(anteile, mengen):
                if m > 0:
                    out.append((kind, rid, item, m, satz, label))
        return out

    def position_stats(self) -> dict[str, dict]:
        """Je LV-Position: Manntage, Kosten und Anzahl beteiligter Zeilen.

        Spesen, Hotel und Reisekosten hängen an der Zeile, nicht am Tag — sie werden
        den Positionen im Verhältnis der Manntage zugeschlagen. Wer eine Woche Aufbau
        und einen Showtag macht, verteilt seine Hotelnächte also entsprechend.
        """
        keys = set(self.day_keys())
        out: dict[str, dict] = {}

        def eintrag(item: str) -> dict:
            return out.setdefault(item, {"mt": 0, "cost": 0.0, "row_ids": set()})

        for row in self.rows:
            mt_row = self.manntage(row)
            if not mt_row:
                continue
            tageskosten = mt_row * row.tagessatz
            je_pos: dict[str, int] = {}
            for day, persons in row.cells.items():
                item = row.assign.get(day)
                if day in keys and item:
                    je_pos[item] = je_pos.get(item, 0) + persons
            for item, mt in je_pos.items():
                if self.is_custom(item):
                    continue
                e = eintrag(item)
                e["mt"] += mt
                e["cost"] += tageskosten * mt / mt_row
                e["row_ids"].add(row.id)
            # Nebenkosten aus derselben Quelle wie die Buchung — sonst zeigt die
            # Matrix je Position etwas anderes an, als in Easyjob landet.
            for _art, _rid, item, menge, satz, _lbl in self.nebenkosten_posten(row):
                e = eintrag(item)
                e["cost"] += menge * satz
                e["row_ids"].add(row.id)
        for e in out.values():
            e["cost"] = round(e["cost"], 2)
            e["rows"] = len(e["row_ids"])
        return out

    def unassigned(self) -> list[dict]:
        """Besetzte Tage ohne Position — der einzige echte Fehlerzustand der Planung.
        Solche Manntage wären geplant und bezahlt, würden aber in keiner Position
        auftauchen: genau das Loch, das in der Excel niemandem auffiel."""
        keys = self.day_keys()
        out = []
        for row in self.sorted_rows():
            tage = [d for d in keys if row.cells.get(d) and not row.assign.get(d)]
            if tage:
                out.append({"row": row, "days": tage,
                            "mt": sum(row.cells[d] for d in tage)})
        return out

    def day_total(self, day: str) -> int:
        return sum(r.cells.get(day, 0) for r in self.rows)

    def day_totals(self) -> dict[str, int]:
        return {d: self.day_total(d) for d in self.day_keys()}

    def totals(self) -> dict:
        rows = self.rows
        return {
            "rows":     len(rows),
            "manntage": sum(self.manntage(r) for r in rows),
            "summe":    round(sum(self.row_total(r) for r in rows), 2),
            "kosten":   round(sum(self.row_costs(r) for r in rows), 2),
            "spesen":   round(sum(self.row_spesen(r) for r in rows), 2),
            "hotel":    round(sum(self.row_hotel(r) for r in rows), 2),
            "rk":       round(sum(self.row_rk(r) for r in rows), 2),
            "peak":     max(self.day_totals().values(), default=0),
        }

    # ── Serialisierung ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "v": 1,
            "date_from": self.date_from, "date_to": self.date_to,
            "hotel_satz": self.hotel_satz,
            "spesen_id": self.spesen_id, "spesen_name": self.spesen_name,
            "spesen_satz": self.spesen_satz, "rk_faktor": self.rk_faktor,
            "hotel_id": self.hotel_id, "hotel_name": self.hotel_name,
            "rk_id": self.rk_id, "rk_name": self.rk_name,
            "menu_nk_pos": dict(self.menu_nk_pos),
            "next_row_id": self.next_row_id,
            "positions": list(self.positions),
            "manual": list(self.manual),
            "dismissed": list(self.dismissed),
            "custom_titles": dict(self.custom_titles),
            "next_custom": self.next_custom,
            "menu_positions": {k: v for k, v in self.menu_positions.items() if v},
            "pos_modes": dict(self.pos_modes),
            "phases": [p.to_dict() for p in self.phases],
            "rows": [r.to_dict() for r in self.rows],
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "CrewPlan | None":
        if not d:
            return None
        rows = [CrewRow.from_dict(r) for r in (d.get("rows") or [])]
        plan = cls(
            date_from=str(d.get("date_from") or ""),
            date_to=str(d.get("date_to") or ""),
            phases=[Phase.from_dict(p) for p in (d.get("phases") or [])],
            rows=rows,
            hotel_satz=float(d.get("hotel_satz") or DEFAULT_HOTEL_SATZ),
            spesen_id=int(d.get("spesen_id") or DEFAULT_SPESEN_ID),
            spesen_name=str(d.get("spesen_name") or DEFAULT_SPESEN_NAME),
            spesen_satz=float(d.get("spesen_satz") or DEFAULT_SPESEN_SATZ),
            rk_faktor=float(d.get("rk_faktor") or DEFAULT_RK_FAKTOR),
            hotel_id=int(d.get("hotel_id") or DEFAULT_HOTEL_ID),
            hotel_name=str(d.get("hotel_name") or DEFAULT_HOTEL_NAME),
            rk_id=int(d.get("rk_id") or DEFAULT_RK_ID),
            rk_name=str(d.get("rk_name") or DEFAULT_RK_NAME),
            menu_nk_pos={str(k): str(v) for k, v in (d.get("menu_nk_pos") or {}).items() if v},
            next_row_id=int(d.get("next_row_id") or 0),
            positions=[str(x) for x in (d.get("positions") or [])],
            manual=[str(x) for x in (d.get("manual") or [])],
            dismissed=[int(x) for x in (d.get("dismissed") or [])],
            custom_titles={str(k): str(v) for k, v in (d.get("custom_titles") or {}).items()},
            # Ältere Entwürfe hielten hier eine Liste — die erste galt ohnehin.
            menu_positions={str(k): str(v[0] if isinstance(v, list) else v)
                            for k, v in (d.get("menu_positions") or {}).items() if v},
            next_custom=int(d.get("next_custom") or 1),
            # Beide Modi lesen, nicht nur „batch": eine LV-Position, die zum
            # Abschnitt gemacht wurde, ist der Fall, den man am ehesten wieder
            # aufmacht — und genau der ging beim Laden verloren.
            pos_modes={str(k): str(v) for k, v in (d.get("pos_modes") or {}).items()
                       if v in ("batch", "menu")},
        )
        # Gegen kaputte Entwürfe: die nächste ID muss über allen vergebenen liegen,
        # sonst bekämen zwei Zeilen dieselbe und die Zellen einer davon wären weg.
        plan.next_row_id = max(plan.next_row_id, max((r.id for r in rows), default=0) + 1)
        return plan


# ─── Buchungen für Easyjob ───────────────────────────────────────────────────
# Die Nebenkosten laufen auf eigene Ressourcen und nicht als Zuschlag auf den
# Tagessatz der Techniker, sonst sind sie später weder auswertbar noch abrechenbar.
# Welche Ressource das ist, steht in der Planung (``hotel_id``, ``rk_id``,
# ``spesen_id``) — im Stamm liegen mehrere zur Auswahl.

# Uhrzeiten der Einsätze. Aus dem Testsystem abgelesen: von den Zeilen, die über die
# Oberfläche entstanden sind, tragen rund 40.000 genau 08:00 bis 18:00 — mit Abstand
# die häufigste Form. Mitternacht bis Mitternacht, was der bisherige Import schreibt,
# kommt praktisch nicht vor (siehe tools/probe_rfa.py).
EJ_TAG_BEGINN = 8
EJ_TAG_ENDE = 18

# Spesen, Hotel und Reisekosten hängen an der Zeile, nicht an einzelnen Tagen. Über
# den ganzen Einsatzzeitraum gezogen legen sie sich in der Personaldisposition quer
# über alles und verdecken, wer wann wirklich arbeitet. Sie bekommen deshalb einen
# eigenen Tag, zwei Tage vor dem ersten Einsatz — außerhalb des Arbeitsfensters und
# trotzdem beim Projekt.
EJ_NK_VORLAUF = 2


@dataclass
class Buchung:
    """Eine Zeile für die ResourceFunctionAllocation in Easyjob.

    ``count`` ist die Kopfzahl und landet in ``Quantity``/``QuantityInvoice``, ``days``
    in ``DaysInAction``. Easyjob rechnet daraus ``TotalPrice = Quantity × Tage × Satz``
    — im Testsystem tragen über 25.000 Zeilen eine Kopfzahl größer eins, das ist dort
    der übliche Weg. Drei Leute zwei Tage sind damit **eine** Zeile mit Anzahl 3, nicht
    drei Zeilen.
    """
    kind: str            # 'tage' | 'spesen' | 'hotel' | 'reise'
    resource_id: int
    item_id: str         # LV-Position → bestimmt Job und Gruppe
    day_from: str
    day_to: str
    days: float
    day_pay: float
    count: int = 1
    fixed_cost: float = 0.0
    label: str = ""

    @property
    def total(self) -> float:
        return round(self.count * self.days * self.day_pay, 2)

    def start_dt(self) -> "datetime":
        from datetime import datetime as _dt
        d = parse_day(self.day_from)
        return _dt(d.year, d.month, d.day, EJ_TAG_BEGINN)

    def end_dt(self) -> "datetime":
        from datetime import datetime as _dt
        d = parse_day(self.day_to)
        return _dt(d.year, d.month, d.day, EJ_TAG_ENDE)


def _runden(betraege: list[float], summe: float) -> list[float]:
    """Anteile auf zwei Stellen runden und die Differenz auf den größten legen.
    Ohne das fehlen am Ende Cents, und die Summe der Positionen weicht von der
    Zeilensumme ab — genau die Art Abweichung, die niemand mehr wiederfindet."""
    out = [round(b, 2) for b in betraege]
    if not out:
        return out
    rest = round(summe - sum(out), 2)
    if rest:
        out[out.index(max(out))] += rest
    return out


def _nk_tag(plan: "CrewPlan") -> str:
    """Der Tag, an dem die Nebenkosten liegen: ``EJ_NK_VORLAUF`` Tage vor dem ersten
    besetzten Tag der ganzen Planung. Leer, wenn niemand eingeteilt ist."""
    keys = set(plan.day_keys())
    besetzt = sorted(d for row in plan.rows for d, n in row.cells.items()
                     if n and d in keys)
    if not besetzt:
        return ""
    return day_key(parse_day(besetzt[0]) - timedelta(days=EJ_NK_VORLAUF))


def bookings(plan: "CrewPlan") -> list[Buchung]:
    """Alle Buchungszeilen einer Planung.

    **Tageskosten** je Zeile und Tagesblock: aufeinanderfolgende Tage mit derselben
    Position UND derselben Personenzahl werden ein Block. Damit steht in Easyjob ein
    Einsatz mit echten Terminen statt eines Klumpens am Projektstart.

    Eine Mehrfachbesetzung wird zu **einer** Zeile mit ``count`` als Kopfzahl —
    Easyjob führt dafür ``Quantity`` und rechnet ``TotalPrice = Quantity × Tage ×
    Satz``. Drei Leute zwei Tage sind also eine Zeile mit Anzahl 3 und zwei Tagen.

    **Nebenkosten** kommen aus ``CrewPlan.nebenkosten_posten`` — derselben Quelle, aus
    der die Matrix ihre Zahlen je Position nimmt. Sie liegen nicht über dem
    Einsatzzeitraum, sondern auf einem eigenen Tag davor (``_nk_tag``): über die ganze
    Spanne gezogen legen sie sich in der Personaldisposition quer über alles.
    """
    keys = plan.day_keys()
    nk_tag = _nk_tag(plan)
    out: list[Buchung] = []

    for row in plan.sorted_rows():
        mt = plan.manntage(row)
        if not mt:
            continue

        # ── Tageskosten: Blöcke gleicher Position und Besetzung ──────────────
        block: dict | None = None
        for day in keys + [None]:
            item = row.assign.get(day) if day else None
            pers = row.cells.get(day, 0) if day else 0
            passt = (block is not None and item and pers
                     and block["item"] == item and block["pers"] == pers
                     and _tag_nach(block["bis"]) == day)
            if passt:
                block["bis"] = day
                continue
            if block:
                tage = (parse_day(block["bis"]) - parse_day(block["von"])).days + 1
                out.append(Buchung("tage", row.resource_id, block["item"],
                                   block["von"], block["bis"], float(tage),
                                   row.tagessatz, count=int(block["pers"]),
                                   fixed_cost=row.eigenkosten, label=row.label))
            block = ({"item": item, "pers": pers, "von": day, "bis": day}
                     if day and item and pers else None)

        # ── Nebenkosten ─────────────────────────────────────────────────────
        # Alle auf denselben Tag vor dem Einsatz. Die Menge steckt in ``days`` und
        # nicht in ``count``: Quantity ist in Easyjob ganzzahlig, die anteilige
        # Verteilung ergibt aber Bruchteile (2,86 Nächte).
        for kind, rid, item, menge, satz, label in plan.nebenkosten_posten(row):
            out.append(Buchung(kind, rid, item, nk_tag, nk_tag, menge, satz,
                               label=label))
    return out


# ─── Aufbau einer neuen Planung ──────────────────────────────────────────────

def default_phases(date_from: str, date_to: str) -> list[Phase]:
    """Aufbau / Veranstaltung / Abbau als grober Vorschlag: die letzten beiden Tage
    sind Abbau, die zwei davor Veranstaltung, der Rest Aufbau. Das trifft selten
    genau — es ist ein Startpunkt, den man in der Kopfzeile verschiebt."""
    try:
        start, end = parse_day(date_from), parse_day(date_to)
    except (ValueError, TypeError):
        return []
    span = (end - start).days + 1
    if span < 3:
        return [Phase("Veranstaltung", day_key(start), day_key(end))]
    abbau_from = end - timedelta(days=1)
    show_from = abbau_from - timedelta(days=min(2, span - 3))
    return [
        Phase("Aufbau",        day_key(start),                      day_key(show_from - timedelta(days=1))),
        Phase("Veranstaltung", day_key(show_from),                  day_key(abbau_from - timedelta(days=1))),
        Phase("Abbau",         day_key(abbau_from),                 day_key(end)),
    ]


# ─── Terminplan aus dem LV lesen ─────────────────────────────────────────────
# Ausschreibungen schreiben ihre Termine in die Vorbemerkungen, meist zweisprachig:
#   „Installation 31.03.2026 till 19.04.2026"
#   „Execution / event 20.04. - 24.04.2026"
#   „Dismantling 24.04.2026 start at 18:00 till 30.04.2026 until 18:00"
# Das ist derselbe Kopf, den die Excel-Personalliste von Hand trug. Wenn es da steht,
# muss es niemand abtippen.

_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?")

# Reihenfolge = Reihenfolge der Phasen. Die Stichwörter sind absichtlich knapp:
# „installation" trifft auch „Technical Installation", das fängt die Auswahl des
# längsten Zeitraums weiter unten wieder ab.
_PHASE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Aufbau",        ("aufbau", "montage", "installation", "build up", "build-up",
                       "buildup", "set up", "set-up", "aufbauzeit")),
    ("Proben",        ("probe", "proben", "rehearsal", "rehearsel")),
    # „opening" fehlt hier bewusst: in Messe-LVs steht damit die Hallenöffnungszeit
    # („Hall Opening from the 10.04. - 19.04."), also mitten im Aufbau.
    ("Veranstaltung", ("execution / event", "veranstaltungszeit", "veranstaltung",
                       "messelaufzeit", "laufzeit", "durchführung", "event day",
                       "eventtag", "execution", "event")),
    ("Abbau",         ("abbau", "demontage", "dismantling", "dismantle", "dismantel",
                       "teardown", "tear down", "abbauzeit")),
)


def _read_date(m: re.Match, jahr_hinweis: int) -> date | None:
    tag, monat, jahr = int(m.group(1)), int(m.group(2)), m.group(3)
    if not (1 <= tag <= 31 and 1 <= monat <= 12):
        return None
    if jahr is None:
        j = jahr_hinweis
    else:
        j = int(jahr)
        if j < 100:
            j += 2000
    try:
        return date(j, monat, tag)
    except ValueError:
        return None


def _ranges_after(text: str, wort: str, jahr: int, fenster: int = 140) -> list[tuple[date, date]]:
    """Alle Datumspaare, die kurz hinter einem Stichwort stehen.

    Das Fenster ist nötig, weil zwischen den beiden Daten Text stehen darf
    („Dismantling 24.04.2026 start at 18:00 till 30.04.2026").
    """
    out: list[tuple[date, date]] = []
    tief = text.lower()
    pos = 0
    while True:
        i = tief.find(wort, pos)
        if i < 0:
            return out
        pos = i + len(wort)
        gefunden: list[date] = []
        for m in _DATE_RE.finditer(text, pos, pos + fenster):
            d = _read_date(m, jahr)
            if d:
                gefunden.append(d)
            if len(gefunden) == 2:
                break
        if len(gefunden) == 2:
            von, bis = gefunden
            if bis < von:                       # Jahreswechsel im Text ohne Jahr
                try:
                    bis = bis.replace(year=bis.year + 1)
                except ValueError:
                    continue
            if (bis - von).days <= MAX_DAYS:
                out.append((von, bis))


def detect_schedule(text: str, jahr_hinweis: int | None = None) -> dict:
    """Aufbau / Proben / Veranstaltung / Abbau aus einem Vorbemerkungstext lesen.

    Je Phase wird der **längste** gefundene Zeitraum genommen. Terminpläne listen
    unter der Gesamtzeit oft die Einzelschritte auf („Build Up Cabeling 01.04.–03.04."),
    und die Gesamtzeit ist die, die zählt.

    Ergibt {"phases": [Phase, …], "date_from": ISO, "date_to": ISO} oder ein leeres
    Dict, wenn nichts Verlässliches drinsteht. Unplausible Funde (Phasen, die sich
    überlappen oder in der falschen Reihenfolge stehen) werden verworfen — ein falscher
    Vorschlag ist schlechter als keiner.
    """
    if not text:
        return {}
    if jahr_hinweis is None:
        jahre = [int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
                 for m in _DATE_RE.finditer(text) if m.group(3)]
        jahr_hinweis = max(set(jahre), key=jahre.count) if jahre else date.today().year

    treffer: list[tuple[str, date, date]] = []
    for name, woerter in _PHASE_WORDS:
        # Stichwörter stehen in Prioritätsfolge: „Execution / event" ist eindeutig,
        # das bloße „event" steht in Fließtext überall. Das erste Stichwort, das
        # überhaupt Datumspaare liefert, gewinnt — die allgemeineren werden dann
        # gar nicht mehr befragt.
        for wort in woerter:
            kandidaten = _ranges_after(text, wort, jahr_hinweis)
            if kandidaten:
                # Terminplantabellen listen unter der Gesamtzeit die Einzelschritte
                # („Build Up Cabeling 01.04.–03.04."). Die Gesamtzeit ist die längste.
                von, bis = max(kandidaten, key=lambda r: (r[1] - r[0]).days)
                treffer.append((name, von, bis))
                break

    if not treffer:
        return {}

    treffer.sort(key=lambda x: x[1])

    # Ein Zeitraum, der in einem anderen liegt, ist zweierlei — und beides kommt in
    # denselben LVs vor:
    #
    #   „Installation 31.03.–19.04." + „Rehearsel Stage 19.04."
    #       Die Probe ist ein eigener Termin. Sie sitzt am ENDE des Aufbaus und
    #       schneidet dessen letzten Tag ab: Aufbau 31.03.–18.04., Proben 19.04.
    #
    #   „Installation 01.03.–20.03." + irgendein Schritt „05.03.–08.03."
    #       Das ist eine Unterzeile der Terminplantabelle, mitten im Block. Sie als
    #       Phase zu nehmen würde den Aufbau in zwei Teile reißen und dazwischen ein
    #       Loch lassen — also weg damit.
    #
    # Unterschieden wird am Ende: fällt es mit dem des umgebenden Zeitraums zusammen,
    # ist es ein eigener Termin; liegt es davor, ist es eine Unterzeile.
    behalten: list[tuple[str, date, date]] = []
    for name, von, bis in treffer:
        umgebend = [(v2, b2) for _, v2, b2 in treffer
                    if v2 <= von and bis <= b2 and (v2, b2) != (von, bis)]
        if umgebend and not any(bis == b2 for _, b2 in umgebend):
            continue
        behalten.append((name, von, bis))
    treffer = behalten
    if not treffer:
        return {}

    # Reihenfolge muss der Natur der Sache folgen — ein Abbau vor dem Aufbau ist ein
    # Fehlfund, und dann ist gar kein Vorschlag besser als ein falscher.
    reihenfolge = [n for n, _, _ in treffer]
    if reihenfolge != [n for n, _ in _PHASE_WORDS if n in reihenfolge]:
        return {}

    # Tagesgenaue Grenze ziehen. LVs schreiben „Dismantling 24.04. start at 18:00",
    # also am Abend des letzten Showtags. Für eine Tagesmatrix gehört der 24.04. zur
    # Veranstaltung und der Abbau beginnt am 25.04. — der SPÄTERE Zeitraum rückt
    # nach hinten, nicht der frühere nach vorn.
    bereinigt: list[tuple[str, date, date]] = []
    for name, von, bis in treffer:
        if bereinigt:
            vor_name, vor_von, vor_bis = bereinigt[-1]
            if von <= vor_bis:
                if bis <= vor_bis:
                    # Der spätere liegt IM früheren und endet mit ihm: der frühere gibt
                    # seinen letzten Tag ab. So wird aus „Installation bis 19.04." plus
                    # „Rehearsel 19.04." ein Aufbau bis 18.04. und Proben am 19.04.
                    # Reicht der spätere darüber hinaus, ist es keine Verschachtelung,
                    # sondern eine Grenze — dann rückt er nach hinten (Zweig unten).
                    bereinigt[-1] = (vor_name, vor_von, von - timedelta(days=1))
                    if bereinigt[-1][2] < vor_von:
                        bereinigt.pop()
                elif (vor_bis - von).days > 1:
                    return {}          # mehr als ein Tag Überlappung: unplausibel
                else:
                    von = vor_bis + timedelta(days=1)
        if bis < von:
            continue                   # durch das Verschieben leer geworden
        bereinigt.append((name, von, bis))
    if not bereinigt:
        return {}

    phasen = [Phase(name, day_key(von), day_key(bis)) for name, von, bis in bereinigt]

    # Aufbau und Abbau gefunden, aber keine Veranstaltung? Dann ist sie die Lücke
    # dazwischen — genau so steht es in den Messe-LVs, die nur Auf- und Abbauzeiten
    # nennen und die Messelaufzeit als bekannt voraussetzen.
    namen = [p.name for p in phasen]
    if "Veranstaltung" not in namen and "Aufbau" in namen and "Abbau" in namen:
        auf = phasen[namen.index("Aufbau")]
        ab = phasen[namen.index("Abbau")]
        luecke_von = parse_day(auf.day_to) + timedelta(days=1)
        luecke_bis = parse_day(ab.day_from) - timedelta(days=1)
        if luecke_bis >= luecke_von:
            phasen.insert(namen.index("Abbau"),
                          Phase("Veranstaltung", day_key(luecke_von), day_key(luecke_bis)))

    return {"phases": phasen,
            "date_from": phasen[0].day_from,
            "date_to": phasen[-1].day_to}


def schedule_from_project(project) -> dict:
    """Terminplan aus den Vorbemerkungen und Hinweisen eines LV lesen."""
    if project is None:
        return {}
    teile: list[str] = []
    for r in list(getattr(project, "preliminaries", []) or []) +              list(getattr(project, "remarks", []) or []):
        teile.append(getattr(r, "title", "") or "")
        teile.append(getattr(r, "long_text", "") or "")
    return detect_schedule("\n".join(teile))


def new_plan(date_from: str, date_to: str) -> CrewPlan:
    return CrewPlan(date_from=date_from, date_to=date_to,
                    phases=default_phases(date_from, date_to))


def lv_row_candidates(project, matches: dict) -> list[dict]:
    """Personalpositionen des LV als Zeilenvorschläge.

    Eine Position zählt als Personalposition, wenn ihr Match eine Ressource der
    Art „Personal" ist — dieselbe Unterscheidung, die der Import beim Buchen trifft
    (Ressourcen werden als ResourceFunctionAllocation gebucht, nicht als Artikel).

    Ein Eintrag **je Position**, auch wenn zwei Positionen dieselbe Ressource
    treffen: die Position wird zum Zuordnungsziel, und ob daraus eine neue Zeile
    entsteht, entscheidet der Aufrufer (``routes/crew.py:crew_seed`` legt je
    Ressource genau eine Zeile an).
    """
    from matcher import Resource as _Resource

    out: list[dict] = []
    for item in getattr(project, "items", []):
        mr = matches.get(item.item_id)
        res = getattr(mr, "matched", None) if mr else None
        if not isinstance(res, _Resource) or res.ressourcenart != "Personal":
            continue
        out.append({
            "item_id":     item.item_id,
            "oz":          item.oz,
            "label":       (item.description or res.funktion).strip()[:80],
            "resource_id": res.id,
            "funktion":    res.funktion,
            "tagessatz":   float(res.tagessatz or 0),
            "eigenkosten": float(res.eigenkosten or 0),
            "qty":         float(item.qty or 0),
            "unit":        item.unit or "",
        })
    return out
