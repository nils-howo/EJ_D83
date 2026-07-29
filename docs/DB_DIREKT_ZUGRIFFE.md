 # Direkte EJ-DB-Zugriffe (pyodbc)

Übersicht aller Stellen im Produktionscode, die direkt auf den EasyJob SQL Server zugreifen statt die REST-API zu nutzen. Testskripte (`test_*.py`) sind separat gelistet.

---

## Produktionscode

### 1. Nightly Sync — Personal lesen
**Datei:** `sync_odbc.py` · **Art:** READ  
**Tabellen:** `ResourceFunction`, `ResourceType`, `ResourceRate2Function`, `ResourceRate`  
**Warum:** Kein API-Endpunkt der Personal-Bulk-Export mit Tagessatz-JOIN liefert.  
**API-ersetzbar:** Nein

---

### 2. Nightly Sync — Artikel lesen
**Datei:** `sync_odbc.py` · **Art:** READ  
**Tabellen:** `StockType`, `StockTypeExtension`, `StockTypeCategory`, `StockTypeCategoryParent`, `Unit`, `StockTypePrice`  
**Warum:** Custom-Felder (`StockTypeExtension`) und aggregierter Primärpreis-JOIN fehlen in bekannten API-Endpunkten. Liest u.a. `StockType.IdTimeFactor` (Berechnungsgrundlage) mit, gespeichert lokal als `articles.id_time_factor`.  
**API-ersetzbar:** Nein

---

### 2b. Nightly Sync — Berechnungsgrundlagen (TimeFactor/TimeFactorItem) lesen
**Datei:** `sync_odbc.py` · **Art:** READ  
**Tabellen:** `TimeFactor`, `TimeFactorItem`  
**Warum:** Preis-Progressionskurven je Einsatztage (z.B. „Standard", „nur Ein Tag", „Video" — 5 Kurven, ~30 Stufen). Steuert wie EJ den Artikelpreis anhand `Job.CommitmentDays` (Einsatztage) berechnet. Kein API-Endpunkt der beide Tabellen kompakt liefert (nur Einzel-Grids `masterdata/timefactors/grid` + `masterdata/timefactoritems/grid` je Kurve). Lokal gespeichert in `time_factors`/`time_factor_items`, komplett ersetzt bei jedem Sync (kleine Referenzdaten).  
**API-ersetzbar:** Ja, aber unpraktisch (5+ Einzel-Requests statt 2 Bulk-Queries) — bewusst nicht gemacht.

---

### 3. Erste Job-ID nach Projekt-Anlage
**Datei:** `routes/import_.py` · **Art:** READ  
**Tabelle:** `Job`  
**SQL:** `SELECT TOP 1 IdJob FROM Job WHERE IdProject = ? ORDER BY IdJob ASC`  
**Warum:** Die API-Antwort von `projects/create` enthält nur die `IdProject`, nicht die ID des automatisch angelegten ersten Jobs.  
**API-ersetzbar:** Prinzipiell ja, falls `GET /api.json/v2/rental/jobs?IdProject=X` die Job-Liste liefert.

---

### 4. Job.RefNumber + Job.CommitmentDays (Einsatztage) schreiben
**Datei:** `routes/import_.py` · **Art:** WRITE (UPDATE)  
**Tabelle:** `Job`  
**SQL:** `UPDATE Job SET CommitmentDays = ? [, RefNumber = ?] WHERE IdProject = ?`  
**Warum:** Die Projekterstellungs-API setzt `RefNumber` nur am Projekt, nicht an den automatisch angelegten Jobs. `CommitmentDays` (die vom Benutzer gesetzten Einsatztage, Standard 2) wird hier **vor** dem Artikel-Einbuchen (Schritt 4 im Import) geschrieben, damit EJ Artikel mit Berechnungsgrundlage (`IdTimeFactor`, siehe Punkt 2/2b) beim Einbuchen automatisch mit dem korrekten Progressions-Faktor bepreist — wir übergeben dabei keinen Preis, EJ berechnet ihn serverseitig aus `CommitmentDays` + der Kurve des Artikels. Kein Job-Patch-Endpunkt für `RefNumber` bekannt.  
**API-ersetzbar:** `CommitmentDays` theoretisch über `POST /api.json/v2/rental/jobs/save` (Feld `CommitmentDays`), verlangt aber viele weitere Pflichtfelder die wir zum Zeitpunkt des Aufrufs nicht griffbereit haben — Round-Trip riskanter als der direkte, gezielte UPDATE. `RefNumber` möglicherweise ebenfalls über `jobs/save`.

---

### 4b. Project.IdContact_Customer (Kundenkontakt) schreiben
**Datei:** `routes/import_.py` · **Art:** WRITE (UPDATE)  
**Tabelle:** `Project`  
**SQL:** `UPDATE Project SET IdContact_Customer = ? WHERE IdProject = ?`  
**Warum:** Der Endpunkt `projects/create` verarbeitet `IdAddress_Customer` (Kundenadresse), **ignoriert aber `IdContact_Customer`** (empirisch bestätigt: nach dem Create ist die Spalte `NULL`, obwohl im Body gesetzt — und obwohl die openapi.json das Feld als `required` führt). Zusätzlich löst ein `IdContactDelivery != 0` im Create-Body einen **HTTP 500** aus (ebenfalls empirisch bestätigt) — daher werden im Create beide Kontakt-Felder bewusst auf `0` gesetzt. Damit der beim Import gewählte Ansprechpartner tatsächlich am Projekt hängt, wird er direkt nachgetragen — im selben DB-Schritt wie Punkt 4 (gleiche Verbindung), nur bei neuem Projekt und tatsächlich gewähltem Kontakt. `Project` hat als einzige Kontaktspalte `IdContact_Customer` (keine separate Lieferkontakt-Spalte).  
**API-ersetzbar:** Ja, bewusst nicht gemacht (Stand jetzt). Verifizierte Alternative: `POST /api.json/v2/rental/projects/propertyupdate` mit Body `{"IdProject": <pid>, "CustomerChanged": {"IdAddressCustomerChanged": <addr>, "IdContactCustomer": <contact>}}` — der EJ-native Weg (nutzt die Oberfläche ebenso). Wichtig: nur **eine** Property-Gruppe pro Request senden (mehrere → „Multiple Property Change request is not supported"), und die Adresse muss mitgegeben werden (nur `IdContactCustomer` allein verwirft EJ). Der direkte UPDATE bleibt bewusst als sichtbarer Marker, dass `projects/create` den Kontakt ignoriert.

---

### 5. Gruppen anlegen (Haupt- und Untergruppen)
**Datei:** `routes/import_.py` · **Art:** WRITE (DELETE + INSERT)  
**Tabellen:** `StockType2JobGroupParent`, `StockType2JobGroup`  
**Warum:** EJ legt beim Anlegen eines neuen Projekts automatisch eine Standard-Gruppe „Artikel" an. Diese wird per `DELETE FROM StockType2JobGroup/StockType2JobGroupParent WHERE IdJob=?` entfernt, bevor die eigenen GAEB-Gruppen eingefügt werden. Außerdem legt `Items/AddGroup` nur flache Untergruppen an; für Hauptgruppen (`StockType2JobGroupParent`) existiert kein API-Endpunkt. Die Parent-ID wird per `OUTPUT INSERTED` sofort für den Untergruppen-INSERT benötigt.  
**API-ersetzbar:** Nein (Hauptgruppen), teilweise (Untergruppen via `Items/AddGroup`)

---

### 6. Gruppen-Caption → ID-Mapping lesen
**Datei:** `routes/import_.py` · **Art:** READ  
**Tabelle:** `StockType2JobGroup`  
**SQL:** `SELECT Caption, IdStockType2JobGroup FROM StockType2JobGroup WHERE IdJob=?`  
**Warum:** Nach dem DB-Insert der Gruppen (Punkt 5) brauchen die Artikelbuchungen die `IdStockType2JobGroup`, um jede Position der richtigen EJ-Gruppe zuzuordnen. Diese ID kennt man erst nach dem INSERT.  
**API-ersetzbar:** Ja — dieser DB-Read ist selbst verursacht. `_insert_g` könnte die `OUTPUT INSERTED`-ID zurückgeben und direkt gecacht werden, analog zu `_insert_hg`. Dann entfällt der nachgelagerte SELECT komplett.

---

### 6b. Alternativ-/Eventualpositionen: Gruppe als „Alternative" markieren
**Datei:** `routes/import_.py` · **Art:** WRITE (UPDATE)  
**Tabelle:** `StockType2JobGroup`  
**SQL:** `UPDATE StockType2JobGroup SET Alternative = 1 WHERE IdStockType2JobGroup IN (…)`  
**Warum:** GAEB-Alternativpositionen (erkannt an `ALNSerNo ≥ 1`) und Eventual-/Bedarfspositionen (erkannt an `<Provis>`) werden in EJ zwar gebucht, sollen aber **nicht** in die Angebotssumme zählen. EJ kennt dafür das Gruppen-Flag `StockType2JobGroup.Alternative`. Nach dem Anlegen der Gruppen (Punkt 5) und dem Einbuchen der aktiven Positionen werden die Gruppen der gebuchten Alternativ-/Eventualpositionen auf `Alternative = 1` gesetzt. `StockType2Job` (Buchungszeile) hat kein solches Flag — die Alternative sitzt auf der Gruppe. (Hinweis: Der D84-Export im **Gruppen-Modus/HG** ist noch nicht sauber; die Modus-Auswahl ist in der GUI daher vorerst ausgeblendet, es läuft nur Positions-Modus.)  
**API-ersetzbar:** Nein (kein Endpunkt für das Alternative-Flag; Teil der ohnehin direkten DB-Gruppenanlage aus Punkt 5)

---

### ~~7. Artikel-ID per Artikelnummer nachschlagen~~ ✅ Erledigt
`IdStockType` wird jetzt beim Nightly Sync als `ej_id` in der lokalen `articles`-Tabelle gespeichert und direkt über `mr.article.ej_id` abgerufen. Kein EJ-DB-Call mehr nötig.

---

### ~~8. Tagessätze aus ResourceRate2Function lesen~~ ✅ Erledigt
`DayPayment` (Tagessatz) und `FixedCostDayPayment` (Eigenkosten) werden jetzt beim Nightly Sync als `tagessatz` und `eigenkosten` in der lokalen `personal`-Tabelle gespeichert und direkt über `res.tagessatz` / `res.eigenkosten` genutzt.

---

### 9. Personalallokationen schreiben (ResourceFunctionAllocation)
**Datei:** `routes/import_.py` · **Art:** WRITE (INSERT)  
**Tabelle:** `ResourceFunctionAllocation`  
**Warum:** GAEB-Positionen die eine Ressource (Personal/Fahrzeug) referenzieren, werden in EJ als Personalallokation gebucht — nicht als Artikelbuchung. `ResourceFunctionAllocation` verknüpft eine Ressource (`IdResourceFunction`) mit einem Job-Zeitraum und einer Gruppe, und speichert dabei Weiterbelasten (`TotalPrice` = Tage × Tagessatz) und Eigenkosten (`TotalCosts` = Tage × Eigenkosten). Kein API-Endpunkt vorhanden.  
**API-ersetzbar:** Nein

---

### 10. D84-Export: Artikelkosten je Gruppe
**Datei:** `routes/projects.py` · **Art:** READ (aggregierend)  
**Tabellen:** `StockType2Job`, `Job`  
**SQL:** `SUM(Factor × TimeFactor × COALESCE(RentalPrice, BasePrice, 0)) GROUP BY IdStockType2JobGroup`  
**Warum:** Komplexes Aggregat für Einheitspreisberechnung im D84. Kein API-Endpunkt für Gruppenkosten-Aggregate.  
**API-ersetzbar:** Nein

---

### 11. D84-Export: Personalkosten je Gruppe
**Datei:** `routes/projects.py` · **Art:** READ (aggregierend)  
**Tabellen:** `ResourceFunctionAllocation`, `Job`  
**SQL:** `SUM(TotalPrice) GROUP BY IdStockType2JobGroup`  
**Warum:** Ressourcen-Positionen landen in `ResourceFunctionAllocation`, nicht in `StockType2Job` (Artikel). Für den D84-Einheitspreis müssen beide Kostenarten je Gruppe summiert werden — Artikelkosten aus Punkt 10 + Personalkosten aus dieser Abfrage (`TotalPrice` = Weiterbelasten). Kein API-Endpunkt für dieses Aggregat.  
**API-ersetzbar:** Nein

---

### 12b. Projektübersicht: gebuchte Artikel lesen (StockType2Job)
**Datei:** `routes/projects.py` · **Art:** READ  
**Tabellen:** `StockType2Job`, `StockType2JobGroup`  
**SQL:** `SELECT s2j.IdStockType, s2j.Quantity, s2j.IdStockType2JobGroup, COALESCE(RentalPrice, BasePrice, 0), Factor, TimeFactor, g.Caption FROM StockType2Job s2j LEFT JOIN StockType2JobGroup g … WHERE s2j.IdJob IN (…)`  
**Warum:** Die Projektübersicht (`/projects/{id}/overview`) zeigt live aus EJ, welche Artikel je Position gebucht sind — mit aktueller Menge, Preis und Bestandswarnung. Kein API-Endpunkt liefert gebuchte Artikel je Job mit Gruppenkontext.  
**API-ersetzbar:** Nein

---

### 12c. Projektübersicht: gebuchte Ressourcen lesen (ResourceFunctionAllocation)
**Datei:** `routes/projects.py` · **Art:** READ  
**Tabellen:** `ResourceFunctionAllocation`  
**SQL:** `SELECT IdResourceFunction, DaysInAction, TotalPrice, IdStockType2JobGroup FROM ResourceFunctionAllocation WHERE IdJob IN (…)`  
**Warum:** Ressourcen/Personal-Buchungen werden in der Übersicht je Position angezeigt (Tage, Kosten). Kein API-Endpunkt vorhanden.  
**API-ersetzbar:** Nein

---

### 12. D84-Export: Gruppen-Captions (OZ-Fallback für Ressourcen-Positionen)
**Datei:** `routes/projects.py` · **Art:** READ  
**Tabellen:** `StockType2JobGroup`, `Job`  
**SQL:** `SELECT Caption, IdStockType2JobGroup FROM StockType2JobGroup WHERE IdProject = ?`  
**Warum:** Ressourcen-Positionen werden als `ResourceFunctionAllocation` gebucht (nicht als `StockType2Job`), daher gibt es für sie keinen Eintrag in der lokalen `project_bookings`-Tabelle. Im D84-Export fehlt damit die Gruppen-ID für deren Kostenberechnung. Fallback: Caption `[OZ] Beschreibung` aus EJ wird rückwärts geparst — OZ → `IdStockType2JobGroup`.  
**API-ersetzbar:** Nein

---

### 12d. D84-Export-Learning: aktuelle Buchung + Stückliste lesen
**Datei:** `routes/projects.py` (`_learn_from_ej`) · **Art:** READ  
**Tabellen:** `StockType2Job`, `ResourceFunctionAllocation`, `StockTypeReference`  
**SQL:** je Gruppe die Top-Level-Artikel (`IdStockType2Job_Parent IS NULL`) und Ressourcen, sowie `SELECT IdStockType_Parent, IdStockType FROM StockTypeReference WHERE IdStockType_Parent IN (…) AND (IsOptional = 0 OR IsOptional IS NULL)`  
**Warum:** Beim D84-Export wird die aktuelle EJ-Buchung je Position mit dem Import-Snapshot verglichen, um in EJ getauschte/ergänzte Artikel/Ressourcen als GUI-Mapping zu lernen. Der Stücklisten-Read (`StockTypeReference`) filtert die **nicht-optionalen** Referenzartikel (`IsOptional = 0`/`NULL`) heraus — das automatisch mitgebuchte Zubehör (Bolzen, Federstecker, Y-Case, Netzkabel, Batterie …), egal welcher `IdStockTypeReferenceType` (1 = Gebunden, 3 = Normal). **Optionale** Referenzen (`IsOptional = 1`, z.B. ein wählbarer ETC-Tubus) bleiben als eigenständige Artikel lernbar. So wird nur wirklich Getauschtes gelernt, nicht das ohnehin automatische Zubehör (das sonst beim nächsten Import doppelt gebucht würde). Kein API-Endpunkt liefert Buchung-je-Gruppe + Stückliste kompakt.  
**API-ersetzbar:** Nein (Aggregat/Join über mehrere Tabellen)

---

### 13. DB-Verbindungstest beim Login
**Datei:** `routes/auth.py` · **Art:** READ (reiner Connectivity-Check)  
**Tabelle:** keine  
**Warum:** Prüft beim Login ob die DB-Verbindung erreichbar ist, bevor `ej_db_conn` in der Session gespeichert wird.  
**API-ersetzbar:** Nein (gezielter DB-Check, kein Datenabruf)

---

### ~~14. Projektliste — Projektnummer auflösen~~ ✅ Über API gelöst
Die menschenlesbare Projektnummer (`Project.Number`, z.B. „26-0994") wird **rein über die API** ermittelt: beim Anlegen einmalig via `projects/grid?SearchText=<IdProject>` geholt (`EjLiveClient.get_project_number`) und lokal in `projects.ej_project_number` gespeichert. Altprojekte werden beim ersten Öffnen der Projekte-Seite einmalig per API nachgeladen und ebenfalls gespeichert. Im Steady State: **kein DB- und kein API-Zugriff** für die Nummer. Kein direkter EJ-DB-Zugriff.

---

## Warum kein API-Endpunkt existiert — Zusammenfassung

| Bereich | Fehlender Endpunkt |
|---|---|
| `ResourceFunctionAllocation` schreiben | Kein POST-Endpunkt in EJ WebAPI v6 |
| `StockType2JobGroupParent` anlegen | Kein Endpunkt (Designentscheidung EJ) |
| `ResourceRate2Function` lesen | Kein GET-Endpunkt bekannt |
| Gruppenkosten aggregiert lesen | Kein Aggregat-Endpunkt |
| Personal-Bulk-Export mit Tagessatz-JOIN | Kein vollständiger Export-Endpunkt |
| Artikel-Bulk-Export mit Custom-Feldern | `StockTypeExtension` nicht in API |

---

## Testskripte (nicht produktiv)

| Datei | Zugriff |
|---|---|
| `test_items_book.py` | READ: `StockType2JobGroup`, `StockType`, `StockType2Job`; WRITE: DELETE als Cleanup-Fallback |
| `test_groups.py` | READ: `StockType2JobGroupParent`, `StockType2JobGroup`, `Job` |
| `test_jobs_create.py` | READ: `Project` |

---

*Letzte Aktualisierung: 2026-07-29*
