# Personalplanung (Crew-Matrix)

Fachliche und technische Spezifikation des Moduls „Personalplanung". Ersetzt die
Excel-Personallisten (`Personal_Kalkulation_<Projekt>.xlsx`), die bisher parallel zum
Tool gepflegt wurden.

**Stand:** 27.08.2026 · Stufen 1 und 2 gebaut, Stufen 3 und 4 offen
**Mockup:** https://claude.ai/code/artifact/ee346096-b7d5-45b2-b602-a81a827a512b

---

## 1. Warum

Die Personalplanung lief bisher in einer Excel je Projekt: Zeilen = Ressourcentyp,
Spalten = Kalendertage, Zelle = Personenzahl, rechts die Kalkulationsspalten
(Tagessatz, Spesen, Hotel, Reisekosten). Drei Probleme, alle drei an der
Schneider-Electric-Liste (HMI 2026) nachweisbar:

1. **Stille Rechenfehler.** 12 von 22 Zeilen summierten nur bis Spalte `Z` (24.04.)
   statt bis `AF` (30.04.) — `=SUM(B34:Z34)`. Bei vier Zeilen lagen dahinter echte
   Abbautage: 9 Manntage / 4.904 € geplant, aber nicht bepreist. Dazu ein
   Reisekostensatz von 8 € statt 250 € (`AO45`) und eine fest eingetippte 5, wo
   überall sonst `=AH44` steht (`AJ44`). Gesamt 5.146 € auf 117.406 € Angebotssumme.
2. **Zwei Dateien für einen Stand.** Die Abgabeversion ist eine Kopie, in der die
   Kostenspalten von Hand gelöscht wurden. Ab dem Moment driften Kundenversion und
   Kalkulation auseinander.
3. **Kein Bezug zum LV.** Die Excel weiß nichts von den Positionen, deren Preis sie
   eigentlich begründet. Der EP für `03.03.01 Installation (1 psch)` wurde geschätzt,
   obwohl die Excel ihn exakt enthält.

Dazu die fachliche Lücke, die das Modul schließen soll: **eine Ressource arbeitet
nacheinander auf mehreren LV-Positionen** — erst Aufbau, dann Betreuung, dann Abbau.
In der Excel führt das zu doppelten Zeilen (der Block „Service/Operating Crew"
wiederholt Funktionen, die oben schon stehen). In der Personalliste soll das
eine Zeile bleiben; aufgeteilt wird nur die *Position*.

---

## 2. Entschieden

| Frage | Entscheidung |
|---|---|
| Wer führt? | **Die Matrix.** Menge und EP der Personalpositionen kommen aus der Planung; das Mengenfeld an der Position wird zur Anzeige. |
| Wo lebt sie? | **Import-Flow und Projekt**, gleiche Tabellen. Die Planung ändert sich nach der Abgabe noch. |
| Zellinhalt | **Ganze Personen.** Keine halben Tage, keine Stunden. Halbe Tage bei Bedarf über eine eigene Zeile. |
| Zeilenherkunft | **Aus den Personalpositionen des LV vorbefüllt**, ergänzbar aus dem EJ-Ressourcenstamm. Vorlagen und Excel-Import bewusst nicht in diesem Ausbau. |
| Was ist eine Zeile? | **Der Ressourcentyp**, nicht die LV-Position. Ein Licht-Operator, der Aufbau und Show macht, ist eine Zeile mit zwei Abschnitten — nicht zwei Zeilen wie in der Excel. |
| Gliederung | **Nach Menüpunkten**, nicht nach Gewerken. Ein Menüpunkt ist eine LV-Position oder ein selbst angelegter Eintrag. Gewerke gab es zwischenzeitlich als freies Feld — sie sind wieder heraus, weil sie eine zweite Struktur neben der des Angebots aufgemacht haben. |
| Eigene Menüpunkte | Sind **nur Überschriften** der Übersicht halber. Sie haben kein Gegenstück im LV, also lässt sich keine Zeit auf sie buchen — sie erscheinen weder als Chip noch im Soll/Ist-Abgleich. |
| Standard-Position je Abschnitt | **Genau eine** je Abschnitt — mehrere wären nicht eindeutig, die erste hätte ohnehin immer gewonnen. Dieselbe Position darf in mehreren Abschnitten stehen (eine Installations-Pauschale ist oft für Licht *und* Ton zuständig). Wer dort Manntage eintippt, ordnet sie ohne weiteren Klick zu. |
| Position ≠ Abschnitt | Eine **Position** ist ein Chip, also ein Zuordnungsziel. Ein **Abschnitt** (Menüpunkt) ist eine Überschrift in der Matrix. Eine gematchte LV-Position wird nicht automatisch zum Abschnitt — sonst hätte die Matrix so viele Überschriften wie das LV Personalpositionen. Per Rechtsklick wird eine daraus, und in Easyjob bekommt ein Abschnitt später seine eigene Gruppe (Stufe 3), während ein reines Ziel in die Gruppe des Abschnitts über ihm läuft. |

---

## 3. Datenmodell

Die **Arbeitskopie liegt in der Session** (`ss.crew`), wie `matches` und `bundles`
auch — beim Import gibt es bis zum ersten Speichern noch gar keine Projektzeile, an
die man sie hängen könnte. Beim Speichern eines Entwurfs wandert sie in die
crew_*-Tabellen an derselben `projects`-Zeile; `promote_draft_to_project` behält
deren ID, die Planung überlebt das Hochladen also ohne Umzug. Sie liegt bewusst
**nicht** im `state_json`, damit die Projektansicht sie später ohne Entwurfs-State
lesen kann.

Das ist dieselbe Arbeitsteilung wie bei den Buchungen: `ss.bundles` ist der
Arbeitsstand, `project_bookings` der festgeschriebene.

```sql
-- ── Personalplanung (Crew-Matrix) ───────────────────────────────────────────
-- Hängt an derselben projects-Zeile wie der Import: ein Entwurf trägt die Planung
-- genauso wie das daraus entstandene Projekt (promote_draft_to_project behält die
-- Zeilen-ID). Die Arbeitskopie lebt bis zum Speichern in der Session.
CREATE TABLE IF NOT EXISTS crew_plans (
    project_id   INTEGER PRIMARY KEY REFERENCES projects(id),
    date_from    TEXT    NOT NULL,          -- ISO, erster Tag der Matrix
    date_to      TEXT    NOT NULL,          -- ISO, letzter Tag
    phases_json  TEXT    NOT NULL DEFAULT '[]',
    hotel_satz   REAL    DEFAULT 150,
    rk_satz      REAL    DEFAULT 250,
    next_row_id  INTEGER DEFAULT 1,
    updated_at   TIMESTAMP,
    updated_by   TEXT
);

-- Eine Zeile = eine Ressource. Sätze sind Kopien aus `personal`: eine spätere
-- Preispflege in Easyjob darf eine abgegebene Kalkulation nicht rückwirkend ändern.
CREATE TABLE IF NOT EXISTS crew_rows (
    id            INTEGER NOT NULL,          -- planweite ID (CrewPlan.next_row_id)
    project_id    INTEGER NOT NULL REFERENCES crew_plans(project_id),
    gewerk        TEXT    NOT NULL DEFAULT '',
    label         TEXT    NOT NULL,
    resource_id   INTEGER NOT NULL,          -- personal.id = EJ IdResourceFunction
    tagessatz     REAL    DEFAULT 0,
    eigenkosten   REAL    DEFAULT 0,
    spesen_satz   REAL    DEFAULT 0,
    hotel_naechte INTEGER DEFAULT 0,
    rk_anzahl     INTEGER DEFAULT 0,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, id)
);

-- Besetzung je Zeile und Tag. Nur belegte Tage stehen hier.
CREATE TABLE IF NOT EXISTS crew_cells (
    project_id INTEGER NOT NULL,
    row_id     INTEGER NOT NULL,
    day        TEXT    NOT NULL,             -- ISO-Datum
    persons    INTEGER NOT NULL,             -- ganze Personen, > 0
    PRIMARY KEY (project_id, row_id, day)
);

-- STUFE 2 (noch nicht angelegt): welcher Tagesabschnitt einer Zeile auf welche
-- LV-Position läuft. Abschnitte einer Zeile dürfen sich nicht überlappen.
CREATE TABLE IF NOT EXISTS crew_segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    row_id     INTEGER NOT NULL,            -- crew_rows(project_id, id)
    day_from   TEXT    NOT NULL,
    day_to     TEXT    NOT NULL,
    item_id    TEXT    NOT NULL,            -- GaebItem.item_id der LV-Position
    source     TEXT    DEFAULT 'phase'      -- 'phase' = aus der Phasenregel
                                            -- 'manual' = vom Benutzer gesetzt
);
CREATE INDEX IF NOT EXISTS idx_crew_seg_row ON crew_segments(project_id, row_id);
```

**Warum `crew_cells` und `crew_segments` getrennt sind:** Die Besetzung ändert sich
tageweise (mal 4, mal 2 Rigger), die Positions-Zuordnung ändert sich blockweise
(zwei Wochen Aufbau auf eine Pauschale). Beides in einer Tabelle hieße, die
Zuordnung 31-mal zu wiederholen und bei jeder Mengenänderung mitzuschleifen.

**`source`** merkt sich, ob ein Abschnitt aus der Phasenregel stammt oder von Hand
gesetzt wurde. Ändert der Benutzer später einen Phasenzeitraum, dürfen nur die
`phase`-Abschnitte nachgezogen werden — eine bewusst gesetzte Ausnahme (der eine
Lichttechniker, der am letzten Showtag schon abbaut) darf dabei nicht verschwinden.

---

## 4. Bedienung

**Zeitachse.** Aus `crew_plans.date_from/date_to`, vorbelegt aus Start-/Enddatum des
Imports. Eine Spalte je Tag, Wochenenden grau, Phasenband über den Tagesköpfen.

**Zellen.** Ganze Zahlen, Excel-artige Tastaturbedienung: tippen und weiter mit Tab
oder Pfeiltaste. Leer = 0 = kein Eintrag in `crew_cells`. Das Sticky-Grid gibt es
schon in `templates/partials/excel_mapping.html` (`.xl-corner` / `.xl-colhead` /
`.xl-rowhead`) — dasselbe Muster.

**Mehrere Felder auf einmal.** Bereich mit der Maus aufziehen oder mit Shift
anklicken, Zahl tippen, Enter — „drei Rigger, die ganze Aufbauwoche" ist ein
Handgriff statt fünf. Escape hebt die Auswahl auf.

Die Auswahl liegt als kräftige Fläche im Hintergrund und der Positionsrahmen als
inset-Schatten darüber: beides ist gleichzeitig zu sehen. Sie schlägt auch die
Schraffur nicht zugeordneter Tage — während man auswählt, will man die Auswahl
sehen, nicht den Fehlerzustand. Beim Drücken auf eine Zelle unterbindet das JS das
Standardverhalten — sonst beginnt der Browser über einer gefüllten Zelle eine
Textmarkierung und der Zug bleibt an der ersten Zahl hängen. Den Fokus setzt es
dafür selbst: `user-select:none` auf den Feldern und `removeAllRanges()` hatten
denselben Zweck, nahmen dem Feld aber den Cursor — in eine Auswahl ließ sich dann
nichts mehr schreiben. Das läuft über
`/api/crew/cells/fill` (Rechteck aus Zeilen × Tagen) und antwortet mit dem ganzen
Panel; der Handgriff ist abgeschlossen, der Eingabefokus also kein Verlust.

**Eine einzelne Zahl antwortet dagegen mit JSON**, und das Zuordnungsband wird im
Browser nachgezogen (`malBand`/`zeichneRow` in `static/crew_matrix.js`). Vorher lud
jede Zelle mit aktiver Position das Panel neu — dabei ging der Eingabefokus verloren,
und wer eine Zahl tippte und ins nächste Feld klickte, stand ohne Auswahl da.

**Termine.** Vorbelegt aus dem Terminplan der LV-Vorbemerkungen (Abschnitt 4a),
danach frei bearbeitbar: umbenennen, verschieben, löschen, eigene ergänzen (Proben,
Anlieferung, Umbautag). Termine überlappen nicht — ein Tag gehört zu genau einer
Phase, sonst wäre „Phase füllen" nicht eindeutig. Verlängert man einen Termin in
einen anderen hinein, weicht dessen Grenze, statt eine Fehlermeldung zu zeigen.

**Positionen und Zeilen folgen dem Matching — ohne Knopfdruck.** Bei jedem Aufbau
der Ansicht gleicht `CrewPlan.sync_positions` / `sync_rows` gegen die Positionen ab,
deren Match eine Personal-Ressource ist:

- Jede solche Position wird ein **Zuordnungsziel** (Chip), in LV-Reihenfolge — nicht
  automatisch ein Abschnitt. Standardmäßig hat die Matrix also keine Überschriften;
  wer eine Position zum Abschnitt machen will, sagt es per Rechtsklick, und eigene
  Abschnitte legt „+ Menüpunkt" an (Titel direkt im Chip eintippbar).
- Je **verschiedener Ressource** entsteht genau eine Zeile — ein Licht-Operator, der
  Aufbau und Show macht, steht einmal da und nicht zweimal wie in der Excel. Die
  Position ist Zuordnungsziel, nicht Zeile.
- Entfällt ein Match, verschwindet die Position wieder. **Es sei denn**, es hängen
  Zuordnungen daran oder sie wurde von Hand dazugelegt (`manual`) — sonst würden
  geplante Manntage still ihr Ziel verlieren.
- Zeilen werden nie automatisch entfernt (sie tragen die Besetzung). Wer eine löscht
  (× im Zeilenkopf, links und damit immer sichtbar — am rechten Zeilenende lag es
  hinter dem Scrollbereich), wählt damit die Ressource ab (`dismissed`); der Abgleich
  legt sie nicht wieder an. Fügt man sie von Hand wieder hinzu, ist die Abwahl
  hinfällig — wer sie sucht und anklickt, will sie haben.

- Die im LV geforderte Menge steht sofort in der Zelle des **ersten Tages** und ist
  der Position zugeordnet — so ist der Soll/Ist-Abgleich vom ersten Moment an
  stimmig und keine Zeile steht leer da. Verteilt wird von Hand: wo die Tage
  wirklich liegen, weiß das LV nicht, und eine erfundene Verteilung wäre schwerer
  zu korrigieren als eine Zahl, die sichtbar noch am falschen Platz steht.
  Übernommen wird nur, was in Tagen angegeben ist — „psch" macht keine
  Mengenaussage, „50 h" ist eine andere Größe. Aus demselben Grund schlägt die
  Mengenprüfung nur bei Tages-Einheiten an.

Zeilen ergänzen geht über „+ Ressource" (dieselbe Suche wie im Ressourcen-Dialog);
die neue Zeile landet unter dem gerade gewählten Abschnitt.

**Die Bezeichnung einer Zeile ist fest.** Es ist der Name der Ressource aus dem
Stamm — ihn hier zu überschreiben würde eine zweite Wahrheit neben Easyjob aufmachen.

**Zeilen einsortieren per Ziehen.** Zeilenkopf greifen, auf die Überschriftszeile
eines Menüpunkts ziehen. Ablageziel ist nur die Überschrift, nicht die Zeile selbst:
sonst müsste man innerhalb einer Gruppe zielen, und ein Fehlgriff würde die Zeile
still woanders einsortieren.

**Zwei Wege zur Zuordnung, in dieser Reihenfolge:**

1. **Die gewählte Position schlägt alles.** Ein Klick auf einen Positions-Chip macht
   sie zum Ziel für alles, was danach passiert: eingetippte Manntage laufen sofort
   auf sie, eine neu angelegte Ressource wird unter ihr einsortiert, im Band
   überstrichene Tage werden ihr zugeordnet.
2. **Ohne Auswahl greift der Standard der Zeile** (`CrewPlan.default_pos_for`) —
   zuerst die Standard-Position ihres Abschnitts, sonst die Position, aus der die
   Zeile entstanden ist. Damit ist eine Ressource aus dem Matching von Anfang an ihrer
   LV-Position zugeordnet, auch bevor irgendein Abschnitt existiert — sonst müsste
   man erst gliedern, bevor die erste Zahl irgendwohin läuft. Der Zeilenkopf zeigt
   diese Position als kleine Marke, damit niemand raten muss, wohin seine Zahlen gehen.

Beides gilt auch beim Füllen eines Bereichs — dort allerdings **je Zeile**: ohne
ausdrückliche Auswahl bekommt jede Zeile ihre eigene Standardposition. Die der
ersten für alle zu nehmen wäre falsch, ein Rigger und ein Lichttechniker gehören
selten auf dieselbe Position.

**Fußzeile.** Personen je Tag, Spitzenwert hervorgehoben. Das ist der
Kapazitätsblick, den die Excel schon hatte (Zeile „Gesamt") und der beim Angebot
die Frage „schaffen wir das personell?" beantwortet.

---

## 4a. Woher die Termine kommen

Ausschreibungen schreiben ihren Terminplan in die Vorbemerkungen, meist
zweisprachig. Im Schneider-LV steht dort wörtlich:

```
Installation 31.03.2026 till 19.04.2026
Execution / event 20.04. - 24.04.2026
Dismantling 24.04.2026 start at 18:00 till 30.04.2026 until 18:00
```

Das ist derselbe Kopf, den die Excel-Personalliste von Hand trug — also wird er
gelesen (`crew_plan.detect_schedule`), statt abgetippt. Daraus kommen Zeitraum
**und** Phasen der Matrix; das Start-/Enddatum aus der Projektanlage taugt
schlechter, weil dort nach Auswahl einer Veranstaltung deren Laufzeit steht, also
ohne Auf- und Abbau.

Die Regeln, jede von einem Fehlfund in echten LVs erzwungen:

- **Stichwörter in Prioritätsfolge.** „Execution / event" ist eindeutig, das bloße
  „event" steht in Fließtext überall. Das erste Stichwort, das Datumspaare liefert,
  gewinnt.
- **Der längste Zeitraum je Phase.** Terminplantabellen listen unter der Gesamtzeit
  die Einzelschritte auf („Build Up Cabeling 01.04.–03.04.").
- **Ein Zeitraum im Zeitraum ist zweierlei.** Endet er mit dem umgebenden, ist es
  ein eigener Termin und er schneidet dessen letzten Tag ab: „Installation bis
  19.04." plus „Rehearsel Stage 19.04." ergibt Aufbau bis 18.04. und Proben am
  19.04. Endet er davor, ist es eine Unterzeile der Terminplantabelle — sie als
  Phase zu nehmen würde den Aufbau zerreißen und ein Loch hinterlassen, also fällt
  sie weg.
- **„opening" ist kein Stichwort.** In Messe-LVs steht damit die Hallenöffnungszeit
  („Hall Opening from the 10.04. - 19.04."), also mitten im Aufbau.
- **Ein Tag Überlappung wird aufgelöst**, indem der spätere Zeitraum nach hinten
  rückt: „Dismantling 24.04. start at 18:00" heißt für eine Tagesmatrix, dass der
  24.04. zur Veranstaltung gehört und der Abbau am 25.04. beginnt.
- **Fehlt die Veranstaltung**, ist sie die Lücke zwischen Auf- und Abbau. Messe-LVs
  nennen oft nur die Auf- und Abbauzeiten und setzen die Laufzeit als bekannt voraus.
- **Unplausibles wird verworfen** (falsche Reihenfolge, mehr als ein Tag
  Überlappung). Kein Vorschlag ist besser als ein falscher.

Getroffen wird damit bei 8 von 11 LVs in `infos/` etwas Brauchbares, beim
Schneider-LV alle vier Phasen tagesgenau (Aufbau 31.03.–18.04., Proben 19.04.,
Veranstaltung 20.–24.04., Abbau 25.–30.04.). Es bleibt ein **Vorschlag**: im Startzustand
sichtbar mit dem Hinweis, dass er aus Text gelesen ist, und danach frei bearbeitbar.

---

## 5. Positions-Zuordnung und Preisbildung

### Zuordnen

Unter der Matrix stehen die LV-Positionen als **Chips**. Die Position einer Zelle
steckt als **Hinterlegung in der Zahlenzelle selbst** — ein eigener Bandstreifen
unter jeder Zeile hat die Matrix doppelt so hoch gemacht und ist wieder heraus.

**Umhängen:** Felder auswählen (aufziehen oder Shift-Klick), dann eine Position
anklicken — `/api/crew/assign-range`. Der Chip „Zuordnung löschen" ist der Radierer.

**Position und Abschnitt sind zwei getrennte Auswahlen.** Die Position (Chip) ist das
Buchungsziel, der Abschnitt nur die Überschrift, unter der neue Ressourcen landen.
Gewählt wird er durch Klick auf **seine Überschrift in der Matrix** — seit eigene
Menüpunkte keine Chips mehr sind, gäbe es sonst keinen Weg dorthin. Ist eine
LV-Position selbst ein Abschnitt, setzt ein Klick auf ihren Chip beides.

**Zellenfarben, abschließend:** besetzt und zugeordnet → Positionsfarbe als
**Rahmen** um die Zelle (`box-shadow: inset`), Feld weiß und Zahl schwarz; besetzt
ohne Position → schraffiert; leer → Wochenendton oder nichts. Als Füllung war die
Farbe entweder abgeschwächt (und stimmte dann nicht mehr mit dem Chip überein) oder
voll (und die Zahl darauf schlecht lesbar) — als Rahmen ist sie farbecht und die
Zelle bleibt ruhig.

**Zuordnung nachreichen.** Erst tippen, später zuordnen: der Knopf an der Warnzeile
legt alle besetzten, noch offenen Tage auf die gewählte Position
(`/api/crew/assign-open`). Schon zugeordnete Tage bleiben unberührt.

**Abschnitt umbenennen** geht per **Doppelklick** auf seine Überschrift; der
Einfachklick wählt ihn aus. Das Titelfeld nimmt bis dahin keine Klicks an
(`pointer-events:none`) — es füllt die Zelle sonst so weit aus, dass zum Auswählen
nichts übrig bleibt.

**Ohne Besetzung keine Farbe.** Wird eine Zelle geleert, fällt ihre Zuordnung mit
weg (`set_cell`). Eine stehengebliebene Farbe würde behaupten, dass dort Manntage
auf eine Position laufen, was nicht mehr stimmt.

Abkürzung für den Normalfall: **Phase füllen**. Ein Klick legt alle besetzten, noch
nicht zugeordneten Tage einer Phase auf die gewählte Position — über alle Zeilen.
Schon zugeordnete Tage bleiben unberührt, eine bewusst gesetzte Ausnahme wird also
nicht wieder eingesammelt. An der Schneider-Liste folgt die Zuordnung in 20 von 23
Fällen der Phase.

**Regeln:**

- Die Zuordnung liegt intern **je Tag** (`CrewRow.assign`), nicht als Block. Blöcke
  wären beim Ändern ein Ärgernis: jedes Umhängen eines Tages müsste sie aufteilen,
  kürzen und wieder zusammenführen. `CrewRow.segments()` fasst sie fürs Band und für
  die Speicherung zusammen — Überlappungen kann es dadurch gar nicht geben.
- Zugeordnet werden auch **unbesetzte** Tage: wer erst den Block malt und dann die
  Zahlen tippt, soll nicht zweimal arbeiten. Gerechnet wird nur, was besetzt ist.
- Besetzte Tage **ohne** Position sind der einzige echte Fehlerzustand: schraffiert im
  Band und gezählt über der Positionsliste („⚠ 9 Manntage ohne Position"). Genau das
  Loch, das in der Excel niemandem auffiel. Die frühere Auflistung je Zeile ist
  wieder heraus — sie machte das Panel hoch, und mit den Standard-Positionen kommt
  der Fall selten vor.
- Eine Position aus der Planung zu nehmen löst ihre Tage — die stehen danach als
  offen da, statt still auf eine Position zu zeigen, die es nicht mehr gibt.

### Rechnen

Je Zeile:

```
Manntage   = Σ persons über alle Tage
Nächte     = min(hotel_naechte, Manntage)

Tageskosten = Manntage × tagessatz
Spesen      = (Manntage − Nächte) × satz/2 + Nächte × satz
Hotel       = Nächte × (row.hotel_satz oder plan.hotel_satz)
Reisekosten = rk_anzahl × (row.rk_satz oder tagessatz × 0,5)
```

**Eingegeben wird nur die Anzahl** — Hotelnächte und Reisen. Alles andere rechnet
sich. Ausnahme ist der **Preis je Nacht**: der steht in der Leiste für die ganze
Planung, weil meist alle im selben Haus übernachten, und lässt sich je Zeile
überschreiben (`crew_rows.hotel_satz`, 0 = der Preis der Planung gilt) — für den
Regisseur im teureren Hotel oder den Kollegen, der privat unterkommt. Dasselbe bei
den **Reisen** (`crew_rows.rk_satz`): vorbelegt ist ein halber Tagessatz der Zeile —
ein Rigger reist günstiger als ein Lichtdesigner, und das steht schon im Stamm —,
eingetragen wird nur, was davon abweicht, etwa ein Flug statt der Bahn. Beide Felder
stehen als `Anzahl × Preis` nebeneinander und bleiben leer, solange die Vorgabe
gilt.

- Der **Spesensatz** kommt aus dem Easyjob-Stamm, wo je Land ein Arbeitsmittel liegt
  („Spesensatz Inland" 32 €, „Spesensatz Schweiz" 62 € …). Gewählt wird er **einmal
  je Planung**: das Land hängt an der Reise, nicht an der Person. Der volle Satz gilt
  für Tage mit Übernachtung, die Hälfte für Tage ohne — das ist die
  Zwölf-Stunden-Pauschale und genau das Paar 32/16, das in der Schneider-Liste von
  Hand je Zeile gesetzt war.
- **Nächte werden auf die Manntage gedeckelt.** Mehr Nächte als Einsatztage sind eine
  Fehleingabe (in der Excel kam das durch zusammengeführte Zeilen vor) und würden die
  Spesen ins Negative rechnen.
- **Reisekosten sind ein halber Tagessatz** der jeweiligen Ressource. Ein Rigger
  reist günstiger als ein Lichtdesigner, und beides steht schon im Stamm.
- Die Matrix kennt nur ganze Personen je Tag, keine Stunden — einen Acht- von einem
  Zwölfstundentag zu unterscheiden geht damit nicht.

**Mehrere Zeilen auf einmal:** sind Zeilen ausgewählt, setzen Hotel, Reisen und
Tagessatz mit Enter den Wert für alle davon (`/api/crew/rows/field`) — „die ganze
Crew übernachtet fünf Nächte" ist ein Handgriff und nicht acht.

**Verteilung der Nebenkosten:** standardmäßig anteilig nach Manntagen wie die
Tageskosten. Je Abschnitt lässt sich stattdessen eine Position bestimmen, die sie
komplett übernimmt.

In der Überschrift eines Abschnitts stehen dafür **zwei gleich aufgebaute Marken**:
**Ziel** (worauf seine Manntage laufen) und **NK** (wohin seine Nebenkosten gehen).
Beide tragen den Farbpunkt ihrer Position, beide nehmen genau eine, beide werden
gesetzt, indem man oben eine Position anklickt und hier bestätigt. Der Preis je
Hotelnacht steht daneben in der Leiste — derselbe Ort, dieselbe Zeit, derselbe Preis.

Je Position die Summe ihrer Anteile. Daraus der EP:

- **Pauschalposition** (`psch`, Menge 1): EP = Summe der zugeordneten Abschnitte.
- **Stückposition** (`5 d`, `50 h`): EP = Summe ÷ geforderte Menge, dazu die Prüfung
  „geplante Manntage = geforderte Menge?" mit Warnung bei Abweichung.

### Zusammenspiel mit dem bestehenden EP-Weg

Nach der EJ-Anlage rechnet `routes/projects.py` den D84-Einheitspreis bereits aus
zwei Aggregaten je Gruppe: Artikelkosten aus `StockType2Job` und Personalkosten aus
`SUM(TotalPrice) GROUP BY IdStockType2JobGroup` über `ResourceFunctionAllocation`
(siehe `docs/DB_DIREKT_ZUGRIFFE.md`, Punkte 10 und 11).

**Daraus folgt: die Matrix braucht keinen zweiten Preisweg.** Sie muss nur jeden
Abschnitt in die Gruppe der zugeordneten Position buchen, dann fällt der EP aus dem
vorhandenen Aggregat. Die lokale Rechnung oben ist die Vorschau *vor* der EJ-Anlage
(für Angebot und PDF) — beide Wege müssen dasselbe Ergebnis liefern, das gehört in
die Tests.

---

## 6. Buchung nach Easyjob

Früher (`routes/import_.py`, Abschnitt 4b) entstand **eine** `ResourceFunctionAllocation`
je Personalposition, mit `DateStart` = Projektstart, `DateEnd` = Start + 1 Tag und
`DaysInAction` = Menge. Die gesamte Crew klumpte damit am Starttag; terminlich war die
Disposition in EJ wertlos.

Abschnitt **4c** bucht jetzt die Planung. Dazu gehört untrennbar, dass 4b die
geplanten Positionen auslässt — sonst steht jede Leistung zweimal im Angebot, einmal
aus dem Matching und einmal aus der Matrix. Maßgeblich ist dabei, worauf die Matrix
tatsächlich Manntage legt, nicht welche Positionen in ihr auftauchen: eine Position
ohne eingetragene Tage bleibt Sache des Matchings, sonst fiele sie still weg.
Dieselbe Unterscheidung gilt in `_calc_import_metrics`, damit der Voranschlag zeigt,
was der Import dann bucht.

`crew_plan.bookings(plan)` erzeugt daraus die Liste der Zeilen —
reine Logik ohne Datenbank und ohne Easyjob, damit sie gegen die Kalkulation prüfbar
ist (`tests/test_crew_plan.py`, Abschnitt 15). Eine **RFA je Tagesblock** mit den
echten Daten:

| Feld | Wert |
|---|---|
| `IdResourceFunction` | `crew_rows.resource_id` |
| `IdJob` | Job der zugeordneten Position (bestehende `_res_job_grp`-Logik) |
| `IdStockType2JobGroup` | Gruppe der zugeordneten Position |
| `DateStart` / `DateEnd` | Anfang und Ende des Tagesblocks, 08:00 bis 18:00 |
| `DaysInAction` | Kalendertage des Blocks |
| `Quantity` / `QuantityInvoice` | Kopfzahl des Blocks |
| `DayPayment` / `TotalPrice` | Tagessatz / Manntage × Tagessatz |
| `FixedCostDayPayment` / `TotalCosts` | Eigenkosten analog |

Spesen, Hotel und Reisekosten laufen als **eigene RFA-Zeilen auf die passenden
Ressourcen und Arbeitsmittel**. Welche das sind, steht in der Planung
(`crew_plans.spesen_id`, `hotel_id`, `rk_id`) und wird mit dem Entwurf gespeichert —
sonst bucht ein späterer Import woanders hin, als die Matrix anzeigt. Ausgewählt wird
in der Leiste nur der **Spesensatz**: den gibt es je Land, und welches gilt,
entscheidet sich am Projekt. Für **Hotel** (37 „Hotelkosten") und
**Reisekosten** (126 „Reisekosten Pauschal") gibt es in der Angebotsphase je genau
eine Ressource, die stehen als Vorgabe in `crew_plan.py`. Die Route
`/api/crew/kosten` ist trotzdem allgemein gehalten — kommt eine zweite dazu, ist das
ein Aufklapper in der Vorlage und kein Umbau.

Beim Hotel gibt es im Stamm mehrere ähnlich heißende Einträge. Maßgeblich ist, was
tatsächlich benutzt wird: „Hotelkosten" (37) trägt über zwei Jahre rund 2.000
Buchungen, „Hotelkosten eigenes Personal" (167) und „Hotelkosten freie Mitarbeiter"
(171) je gut 800. Ausdrücklich **nicht** als Zuschlag
auf den Tagessatz der Techniker: in Easyjob sollen sie als das erscheinen, was sie
sind, sonst sind sie später weder auswertbar noch abrechenbar.

Ein **Tagesblock** endet, wo die Position oder die Besetzung wechselt: drei Tage mit
zwei Leuten und ein vierter mit einem sind zwei Blöcke, nicht einer. Unbesetzte Tage
in der Mitte trennen ebenfalls — gebucht wird nur, was besetzt ist.

Je Block entsteht **eine** Zeile; die Kopfzahl steht in `Quantity`. Easyjob rechnet
daraus `TotalPrice = Quantity × DaysInAction × DayPayment` — im Testsystem tragen über
25.000 Zeilen eine Kopfzahl größer eins, `QuantityInvoice` ist dort immer gleich
`Quantity`. Drei Leute an zwei Tagen sind also eine Zeile mit Anzahl 3 und zwei Tagen,
nicht drei Zeilen. Uhrzeit 08:00 bis 18:00, ebenfalls abgelesen: rund 40.000 Zeilen
tragen genau diese, Mitternacht bis Mitternacht praktisch keine.

**Nebenkosten liegen nicht über dem Einsatzzeitraum**, sondern auf einem eigenen Tag
`EJ_NK_VORLAUF` (= 2) Tage vor dem ersten besetzten Tag der Planung. Über die ganze
Spanne gezogen legen sie sich in der Personaldisposition quer über alles und
verdecken, wer wann wirklich arbeitet. Ihre Menge steckt in `DaysInAction` und nicht
in `Quantity`: die Spalte ist ganzzahlig, die anteilige Verteilung ergibt aber
Bruchteile (2,86 Nächte).

Die Nebenkosten treffen dieselben Positionen wie die Tageskosten, anteilig nach
Manntagen. Verteilt wird dabei die **Menge**, nicht der Betrag: `DaysInAction` hat in
Easyjob zwei Nachkommastellen, ein anteiliger Betrag ließe sich dort gar nicht
abbilden. Der Rundungsrest liegt auf dem größten Anteil, damit die Summe stimmt. Das
erzeugt Bruchteile („2,86 Nächte"); wer das sauber haben will, setzt am Abschnitt die
**NK**-Marke, dann geht jeder Posten in genau einer Zeile auf eine Position.

Diese Verteilung steht in `CrewPlan.nebenkosten_posten` und wird von **beiden** Seiten
benutzt — von `position_stats` für die Anzeige und von `bookings` für Easyjob.
Rechneten sie getrennt, zeigte die Matrix je Position etwas anderes an, als dort
gebucht wird, und genau daraus entsteht der Einheitspreis. Ein Zufallstest über 200
Planungen hält die Gleichheit fest (`tests/test_crew_booking.py`, Abschnitt 5).

Tage **ohne** Positionszuordnung bekommen nichts — auch nicht anteilig. Ihre
Tageskosten werden ebenso wenig gebucht; sie stehen in der Matrix als offen, bis
jemand sie zuordnet. Nur **Tageskosten** lösen übrigens das Matching ab: eine
Position, die bloß Nebenkosten abbekommt, behält ihre eigene Ressource. Die Spesen bleiben in beiden Fällen auf zwei
Zeilen aufgeteilt — halber Satz für Tage ohne Übernachtung, voller mit —, weil ein
Mischsatz in Easyjob nicht mehr nachvollziehbar wäre.

Welche Gruppe ein Abschnitt trifft, entscheidet der Positionsmodus aus Abschnitt 2:
ein **Menüpunkt** bekommt seine eigene Gruppe, eine **Sammelposition** läuft in die
Gruppe ihres Menüpunkts (`CrewPlan.position_parent`). Damit stimmt auch der
EP-Aggregat-Weg aus Abschnitt 5: was in eine Gruppe gebucht wird, landet im
Einheitspreis genau dieser Position.

---

## 7. Export

**PDF** — `crew_pdf.py`, gebaut mit `reportlab`. Reines Python-Wheel, läuft im
`python:3.12-slim` ohne zusätzliche Systempakete; WeasyPrint bräuchte Pango und Cairo
im Image. Querformat A4, Wochenenden grau, Phasenband oben, Tagessummen unten.

Die Tagesspalten sind mindestens 11 pt breit; passt der Zeitraum damit nicht auf eine
Seite, wird er auf mehrere verteilt und die Namensspalte auf jeder wiederholt. Ein
Monat geht auf ein Blatt — das ist der Regelfall, und ein Umbruch mitten in der
Veranstaltung wäre ein schlechter Tausch.

Der Briefbogen liegt als A4 **hoch** vor. Statt ihn quer zu zerren, werden die beiden
Bänder ausgeschnitten — der schwarze Logo-Block oben rechts, das gelbe Adressband
unten — und in ihrer echten Druckgröße gesetzt (150 dpi, also Faktor 72/150). Das Logo
ist auf dem Querformat damit genauso groß wie auf einem gewöhnlichen Brief. Die
Adresse ist Teil des Bildes und wird nicht als Text nachgebaut: sonst müsste sie hier
gepflegt werden und liefe der echten irgendwann hinterher.

**Zwei Varianten aus denselben Daten:**

- **Kundenversion** — nur Besetzung, keine Sätze, keine Beträge. Beilage zur Ausschreibung.
- **Kalkulation** — zusätzlich TS, Spesen, Hotel, RK, Zeilensumme, Aufteilung nach Position.

Damit entfällt die zweite Datei aus Problem 2 in Abschnitt 1.

**Excel** — `crew_xlsx.py`, dasselbe Layout über `openpyxl` (war bereits
Abhängigkeit). Die Kostenspalten stehen dort als **Formeln** in der Datei, nicht als
ausgerechnete Zahlen: der einzige Grund, die Liste als Excel statt als PDF zu geben,
ist das Weiterrechnen. Die Spesenformel bildet die Regel ab — `(MT − Nächte) × halber
Satz + Nächte × voller Satz` —, damit sie nachvollziehbar bleibt. Namensspalte und
Kopfzeilen sind fixiert.

**Wo** — in der Matrixleiste unter „Export" (vier Einträge: PDF und Excel, je Kunde
und Kalkulation) und im Kopf der Projektübersicht, sobald zu einem abgelegten Projekt
eine Planung gespeichert ist. Letzteres, damit sich eine Beilage auch nachreichen
lässt, wenn der Import längst vorbei ist; die Positionsbeschriftungen kommen dann aus
dem lokalen Abbild der Buchungen statt aus dem LV in der Sitzung.

Die Variante steckt im **Dateinamen** (`…_Kalkulation.pdf` / `…_Besetzung.pdf`) —
eine falsch verschickte Datei fällt sonst niemandem auf.

---

## 8. Stufen

| # | Inhalt | Dateien |
|---|---|---|
| 1 ✅ | Datenmodell, Matrix-UI, Zeilen anlegen (LV + Stamm), Summen, Speichern | `crew_plan.py`, `routes/crew.py`, `templates/partials/crew_matrix.html`, `templates/partials/crew_resources.html`, `static/crew_matrix.js` (alle neu) · `db.py`, `state.py`, `server.py`, `routes/import_.py`, `templates/import.html`, `static/style.css` · Tests: `tests/test_crew_plan.py`, `tests/test_crew_routes.py` |
| 2 ✅ | Laufender Abgleich mit dem Matching, Zuordnung (Chip-Palette + Band), gewählte Position als Kontext, Phase füllen, Soll/Ist je Position, Menüpunkt/Sammelposition per Rechtsklick, eigene Menüpunkte, Zeilen per Drag & Drop einsortieren, Terminerkennung aus dem LV und Termin-Editor, Rückkopplung in den Positionsbaum | `templates/partials/crew_positions.html`, `crew_phases.html` (neu) · `crew_plan.py`, `db.py`, `routes/crew.py`, `routes/import_.py`, `templates/partials/crew_matrix.html`, `templates/partials/import_groups.html`, `static/crew_matrix.js`, `static/style.css` |
| 3 ✅ | EJ-Buchung je Einsatz mit echten Terminen, Spesen/Hotel/RK als eigene RFA, keine Doppelbuchung neben dem Matching | `crew_plan.py` (`bookings()`), `routes/import_.py` (Abschnitt 4b/4c, `_calc_import_metrics`), `tools/probe_rfa.py` (neu) · Test: `tests/test_crew_booking.py` (neu) |
| 4 ✅ | PDF- und Excel-Export, beide Varianten, aus Import und Projektübersicht | `crew_pdf.py`, `crew_xlsx.py` (neu) · `routes/crew.py`, `routes/projects.py`, `templates/partials/crew_matrix.html`, `templates/projects_overview.html`, `requirements.txt` · Test: `tests/test_crew_export.py` (neu) |

Nach Stufe 2 ist das Modul für die Ausschreibung brauchbar, nach Stufe 4 ersetzt es
die Excel vollständig.

**Stufen 1 und 2 stehen.** Die Matrix hängt als aufklappbares Panel
„3. Personalplanung" über der Positionsliste auf `/import`. Zeitachse festlegen,
Zeilen aus dem LV übernehmen oder aus dem Stamm suchen, Gewerk eintragen,
Personenzahlen tippen (Tab und Pfeiltasten wie in Excel), Sätze je Zeile ändern.
Gematchtes Personal steht sofort da — Positionen als Chips, Ressourcen als Zeilen,
ohne Knopfdruck. Ein Chip anklicken macht ihn zum Ziel für alles Folgende,
Rechtsklick schaltet Menüpunkt/Sammelposition, „+ Menüpunkt" am Fuß der Matrix legt
einen eigenen an (und setzt den Fokus gleich in sein Titelfeld),
Zeilen wandern per Ziehen zwischen den Menüpunkten. Soll/Ist und EP-Vorschlag stehen
aufklappbar darunter. Die Matrix zeigt immer die Kalkulation — die Kundenversion ohne
Preise ist eine Sache des Exports (Abschnitt 7) und kein Schalter, der beim Arbeiten
danebensteht. Im Positionsbaum des Imports schaltet
„📋 über Personalplanung" eine Position auf die Planung um, statt dort eine Ressource
zu buchen — und wer im Ressourcen-Dialog dort jemanden bucht, schaltet sie
damit gleich mit um: Personal gehört in die Matrix, Menge und Termine kommen von
dort und nicht aus einer Pauschalmenge am Match. Gespeichert wird mit dem Entwurf.

Verlässt jemand die Importseite, gibt der Browser die Bearbeitungssperre frei.
Kommt er zurück, holt die Seite sie sich wieder — hat inzwischen jemand anderes
den Entwurf übernommen, wird die eigene Arbeitskopie verworfen, statt später über
dessen Arbeit zu schreiben (`_verwerfe_arbeitskopie`, Test
`tests/test_draft_lock.py`).

**Stufe 3 steht ebenfalls.** Beim Hochladen bucht Abschnitt 4c je Einsatz eine RFA
mit echten Terminen (08:00–18:00), eine Zeile je Person, Nebenkosten auf ihre eigenen
Ressourcen. Abschnitt 4b lässt die geplanten Positionen dabei aus — ohne das stünde
jede Leistung zweimal im Angebot.

Alte RFA werden **nicht** gelöscht: jeder Import legt in Easyjob ein neues Projekt an,
ein zweiter Lauf in dasselbe Projekt kommt nicht vor. Damit entfällt der einzige Fall,
in dem gelöscht werden müsste.

**Stufe 4 steht.** Export als PDF und Excel, je in Kunden- und Kalkulationsfassung,
aus der Matrixleiste und aus der Projektübersicht (Abschnitt 7). Damit ersetzt das
Modul die Excel-Personalliste vollständig — der Punkt, an dem Abschnitt 1 anfing.

---

## 9. Offene technische Punkte

- **Personenzahl in EJ — geklärt.** `ResourceFunctionAllocation` hat keine
  Kopfzahl-Spalte. `tools/probe_rfa.py` liest rein lesend aus dem Testsystem aus, wie
  Disponenten das über die Oberfläche lösen. Befund für die letzten zwei Jahre:

  | Form | Zeilen |
  |---|---|
  | `DaysInAction` = Kalendertage (eine Person je Zeile) | 50.518 |
  | `DaysInAction` > Kalendertage (Kopfzahl in einer Zeile) | 9.288 |
  | weniger (Teilzeit, Lücken) | 8.129 |

  Die Auflösung steht aber in einer anderen Spalte: **`Quantity`**. Über 25.000
  Zeilen tragen dort eine Zahl größer eins, `QuantityInvoice` ist immer gleich, und
  `TotalPrice` rechnet sich als `Quantity × DaysInAction × DayPayment` (an Beispielen
  nachgerechnet). Gebucht wird deshalb **eine Zeile je Block mit `Quantity` =
  Kopfzahl**. Uhrzeiten 08:00–18:00 (39.997 Zeilen), nicht Mitternacht.

  Die Nebenkosten-Ressourcen sind aktiv und haben alle `DayPayment = 0` — der Satz
  muss also an der Zeile stehen. Beim **Hotel** ist 37 „Hotelkosten" die benutzte
  (rund 2.000 Buchungen gegenüber je gut 800 auf 167 und 171).
- **Löschen/Neuanlage.** Wird ein Projekt ein zweites Mal importiert, müssen alte RFAs
  entfernt werden, sonst summieren sich die Personalkosten je Gruppe doppelt (der
  EP-Aggregat-Weg aus Abschnitt 5 merkt das nicht).
- **Sperre.** `projects.locked_by` gilt für den Entwurf. Ob die Matrix am angelegten
  Projekt dieselbe Sperre braucht, ist offen — zwei Leute, die gleichzeitig Zellen
  tippen, überschreiben sich sonst.
- **Doppelbuchung.** Abschnitt 4b in `routes/import_.py` bucht bis heute jede Position
  mit Personal-Match als RFA, ohne die Planung zu kennen. Ist die Position von der
  Planung abgedeckt, entsteht die Buchung damit zweimal. Dasselbe gilt für
  `_calc_import_metrics`: die Personalkosten in der Seitenleiste kommen aus den
  Matches, nicht aus der Planung. Beides gehört in Stufe 3 zusammengeführt — es ist
  die eigentliche Arbeit daran, nicht das Schreiben der RFA-Zeilen.

---

## 10. Regressionsfall

Die Schneider-Liste ist der Testfall: `infos/Personal_Kalkulation_Schneider_Electric@HMI_2026.xlsx`
gegen `infos/251127_Schneider Electric_Hannover Messe26_AVL_Tender.x83`.

Nachgerechnet muss das Modul reproduzieren:

- **Tagessummen** 19, 19, 0, 0, 2, 7, 5, 5, 5, 0×6, 1, 13, 18, 10, 9, 6, 6, 6, 6, 21, 10, 0, 0, 0, 13, 14
  (identisch zur Zeile „Gesamt" der Excel)
- **195 Manntage**, 18 Zeilen (statt 22 in der Excel — die sechs Show-Crew-Zeilen sind
  Abschnitte vorhandener Zeilen)
- **122.607 €** gegenüber 117.406 € in der Excel. Davon sind 5.146 € die in
  Abschnitt 1 genannten Formelfehler, der Rest kommt aus dem Nebenkostenmodell
  (Spesen je nach Übernachtung statt eines von Hand gewählten Satzes, Reisekosten als
  halber Tagessatz statt 250 € pauschal). Aufgeschlüsselt: 3.872 € Spesen,
  7.050 € Hotel, 2.725 € Reisekosten.
