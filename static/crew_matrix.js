/* Personalplanung (Crew-Matrix) — Tastaturbedienung und Speichern einzelner Zellen.
 *
 * Die Matrix kommt fertig vom Server. Beim Tippen wird sie NICHT neu gerendert:
 * jede Zelle schickt ihren Wert an /api/crew/cell und bekommt die neuen Summen als
 * JSON zurück, die hier eingetragen werden. Ein Neuaufbau würde den Eingabefokus
 * verlieren — nach zwei Zahlen tippt man sonst ins Leere.
 */
(function () {
  "use strict";

  var PANEL = "#crew-panel";
  var searchOpen = false;     // Ressourcensuche aufgeklappt

  var eur = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 0 });

  function panel() { return document.querySelector(PANEL); }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  // ── Anzeige-Schalter ──────────────────────────────────────────────────────

  window.crewToggleSearch = function () {
    var box = document.getElementById("crew-search");
    if (!box) return;
    searchOpen = box.hidden;
    box.hidden = !searchOpen;
    if (searchOpen) {
      // Die Suche steht unter der Matrix, der Knopf darüber — ohne Nachführen
      // passierte auf dem Bildschirm scheinbar nichts.
      box.scrollIntoView({ block: "nearest", behavior: "smooth" });
      var q = document.getElementById("crew-add-q");
      if (q) { q.focus(); q.select(); }
    }
  };

  // ── Speichern ─────────────────────────────────────────────────────────────

  function post(url, data, onOk, onFail) {
    var body = new URLSearchParams(data);
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest" },
      body: body.toString(),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (res.ok && res.j && res.j.ok) { onOk(res.j); } else { onFail(res.j); }
      })
      .catch(function (e) { onFail({ error: String(e) }); });
  }

  function applyTotals(j) {
    if (j.row_id !== undefined) {
      setText("crew-mt-" + j.row_id, j.row_mt);
      setText("crew-sum-" + j.row_id, eur.format(j.row_total) + " €");
      // Spesen hängen an den Manntagen, Reisekosten am Tagessatz — beide ändern sich
      // also mit, ohne dass jemand sie angefasst hätte.
      if (j.row_spesen !== undefined) {
        setText("crew-sp-" + j.row_id, eur.format(j.row_spesen) + " €");
        setText("crew-ho-" + j.row_id, eur.format(j.row_hotel) + " €");
        setText("crew-rk-" + j.row_id, eur.format(j.row_rk) + " €");
        nkTitel(j.row_id, "hotel_naechte", eur.format(j.row_hotel) + " €");
        nkTitel(j.row_id, "rk_anzahl", eur.format(j.row_rk) + " €");
      }
    }
    if (j.day !== undefined) {
      var cell = document.getElementById("crew-dt-" + j.day);
      if (cell) cell.textContent = j.day_total || "";
      // Spitzenwert kann durch jede Eingabe woanders liegen — alle Tage neu bewerten.
      var peak = j.peak || 0;
      var all = document.querySelectorAll('[id^="crew-dt-"]');
      for (var i = 0; i < all.length; i++) {
        var n = parseInt(all[i].textContent, 10) || 0;
        all[i].classList.toggle("crew-peak", peak > 0 && n === peak);
      }
    }
    if (j.totals) {
      setText("crew-tot-rows", j.totals.rows);
      setText("crew-tot-mt", j.totals.manntage);
      setText("crew-tot-sum", eur.format(j.totals.summe) + " €");
      setText("crew-tot-peak", j.totals.peak);
    }
  }

  // Hotel und Reisen zeigen nur die Anzahl; der Betrag steht im Tooltip und muss
  // deshalb mitgerechnet werden, sonst nennt er nach der ersten Änderung eine
  // Zahl, die nicht mehr stimmt.
  function nkTitel(rowId, feld, text) {
    var el = document.querySelector('#crew-table input[data-field="' + feld
      + '"][data-row="' + rowId + '"]');
    if (el && el.title.indexOf(" = ") > 0) {
      el.title = el.title.split(" = ")[0] + " = " + text;
    }
  }

  function flagError(input, msg) {
    input.classList.add("crew-err");
    input.title = msg || "Nicht gespeichert";
  }

  function clearError(input) {
    input.classList.remove("crew-err");
    input.removeAttribute("title");
  }

  function saveCell(input) {
    var raw = (input.value || "").replace(/\D/g, "");
    input.value = raw && raw !== "0" ? String(parseInt(raw, 10)) : "";
    post("/api/crew/cell",
      { row_id: input.dataset.row, day: input.dataset.day, persons: raw || 0,
        assign_to: activePos || "" },
      function (j) {
        clearError(input);
        input.value = j.persons ? String(j.persons) : "";
        input.closest("td").classList.toggle("crew-filled", !!j.persons);
        applyTotals(j);
        // Band lokal nachziehen. Vorher lud das ganze Panel neu — dabei ging der
        // Eingabefokus verloren, und wer eine Zahl tippte und ins nächste Feld
        // klickte, stand plötzlich wieder ohne Auswahl da.
        malZelle(input.dataset.row, input.dataset.day, j.assigned || "", !!j.persons);
      },
      function (j) { flagError(input, (j && j.error) || "Nicht gespeichert"); });
  }

  // Ein Feld für alle ausgewählten Zeilen. Gibt false zurück, wenn nur eine Zeile
  // betroffen ist — dann speichert der gewöhnliche Weg.
  function fuelleZeilen(input) {
    // Nur eine Auswahl aus den Kostenspalten überträgt auf mehrere Zeilen, und nur in
    // ihrer eigenen Spalte. Sonst würde eine Auswahl woanders unsichtbar mitwirken —
    // vorne die Tage, hinten die Nachbarspalte.
    if (!sel || sel.from || sel.rows.length < 2 || !window.htmx) return false;
    if (sel.feld !== input.dataset.field) return false;
    var zeilen = sel.rows.slice();
    sel = null;
    zeigeAuswahl();
    htmx.ajax("POST", "/api/crew/rows/field", {
      target: "#crew-panel", swap: "outerHTML",
      values: { row_ids: zeilen.join(","), field: input.dataset.field,
                value: input.value },
    });
    return true;
  }

  function saveField(input) {
    post("/api/crew/row/" + input.dataset.row + "/field",
      { field: input.dataset.field, value: input.value },
      function (j) {
        clearError(input);
        // Den normalisierten Wert zurückschreiben: „520.50" getippt, 520,5 gespeichert.
        // Ohne das steht im Feld etwas anderes als in der Kalkulation.
        if (j.value !== undefined && document.activeElement !== input) {
          input.value = j.value;
        }
        applyTotals(j);
      },
      function (j) { flagError(input, (j && j.error) || "Nicht gespeichert"); });
  }

  // ── Zuordnung malen ──────────────────────────────────────────────────────
  // Die Positionsliste unter der Matrix ist die Palette, das Band unter jeder
  // Zeile die Fläche — dasselbe Muster wie die Spaltenrollen im Excel-Mapping.
  // Gemalt wird nur lokal; erst beim Loslassen geht EIN Request raus.

  // Die gewählte Position ist der Kontext für ALLES: eingetippte Manntage laufen
  // auf sie, neue Ressourcen werden unter ihr einsortiert, im Band malt man mit ihr.
  var activePos = null;      // item_id, "" = Radierer, null = nichts gewählt
  var activeMode = "menu";   // "menu" | "batch"
  // Der gewählte Abschnitt ist etwas anderes als die gewählte Position: die Position
  // ist das Buchungsziel, der Abschnitt nur die Überschrift, unter der neue
  // Ressourcen landen. Seit eigene Menüpunkte keine Chips mehr sind, wird er über
  // seine Überschrift in der Matrix gewählt.
  var activeMenu = "";
  // Zuordnen läuft über die Auswahl: Felder markieren, dann eine Position
  // anklicken. Der frühere Bandstreifen unter jeder Zeile hat die Matrix doppelt so
  // hoch gemacht; die Position steht jetzt als Hinterlegung in der Zelle selbst.

  // Für htmx-Aufrufe aus den Vorlagen (hx-vals="js:{...}").
  window.crewActiveMenu = function () { return activeMenu; };
  window.crewActivePos = function () { return activePos || ""; };

  function chips() {
    return Array.prototype.slice.call(
      document.querySelectorAll("#crew-pos .crew-chip"));
  }

  function setActive(itemId, mode) {
    activePos = itemId;
    activeMode = mode || "menu";
    chips().forEach(function (c) {
      c.classList.toggle("crew-chip-on", c.dataset.itemId === itemId && itemId !== null);
    });
    var hint = document.getElementById("crew-pos-hint");
    if (!hint) return;
    if (itemId === null) {
      hint.textContent = "Anklicken wählt das Ziel · Rechtsklick macht daraus einen eigenen Abschnitt";
    } else if (!itemId) {
      hint.textContent = "Radierer gewählt — ausgewählte Felder verlieren ihre Zuordnung";
    } else {
      hint.textContent = "Ziel gewählt — eingetippte Manntage und neue Ressourcen "
        + "laufen jetzt darauf";
    }
    zeigeAbschnitt();
  }

  function zeigeAbschnitt() {
    var titel = "";
    var alle = document.querySelectorAll("#crew-table tr.crew-gewerk");
    for (var i = 0; i < alle.length; i++) {
      var an = alle[i].dataset.menu === activeMenu && activeMenu !== "";
      alle[i].classList.toggle("crew-gewerk-on", an);
      if (an) {
        var kopf = alle[i].querySelector(".crew-head");
        var feld = kopf && kopf.querySelector("input");
        titel = feld ? feld.value : (kopf ? kopf.textContent.trim() : "");
      }
    }
    var note = document.getElementById("crew-search-note");
    if (note) {
      note.textContent = activeMenu
        ? "Neue Zeilen landen unter „" + (titel || "gewähltem Abschnitt") + "“"
        : "Kein Abschnitt gewählt — neue Zeilen stehen oben unter „Ohne Abschnitt“";
    }
  }

  // Doppelklick auf die Überschrift schaltet ihr Titelfeld frei. Solange es gesperrt
  // ist, gehen Klicks an die Zelle darunter — sonst bliebe neben dem Feld kaum etwas
  // übrig, worauf man zum Auswählen klicken könnte.
  document.addEventListener("dblclick", function (e) {
    var tr = e.target.closest && e.target.closest("#crew-table tr.crew-gewerk");
    if (!tr) return;
    var feld = tr.querySelector("input.crew-head-input");
    if (!feld) return;
    e.preventDefault();
    feld.classList.add("crew-head-edit");
    feld.focus();
    feld.select();
  });

  document.addEventListener("focusout", function (e) {
    if (e.target.classList && e.target.classList.contains("crew-head-input")) {
      e.target.classList.remove("crew-head-edit");
    }
  });

  // Klick auf eine Abschnitts-Überschrift wählt ihn als Ziel für neue Ressourcen.
  // Nicht auf das Titelfeld oder das ×, sonst könnte man den Namen nicht ändern.
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    // Ein freigeschaltetes Titelfeld und die Knöpfe behalten ihren Klick.
    if (e.target.closest("button")) return;
    if (e.target.matches && e.target.matches("input:not(.crew-head-input), " +
                                             "input.crew-head-edit")) return;
    var tr = e.target.closest("#crew-table tr.crew-gewerk");
    if (!tr) return;
    var key = tr.dataset.menu || "";
    activeMenu = (key && key !== activeMenu) ? key : "";
    zeigeAbschnitt();
  });

  // Klick auf einen Chip: sind Felder ausgewählt, werden sie umgehängt. Sonst wird
  // der Chip zum Ziel für alles Folgende.
  document.addEventListener("mousedown", function (e) {
    var chip = e.target.closest && e.target.closest("#crew-pos .crew-chip");
    if (!chip || chip.classList.contains("crew-chip-new")) return;
    if (e.button === 2) return;               // Rechtsklick: siehe contextmenu
    e.preventDefault();
    var ziel = chip.dataset.itemId;
    if (sel && sel.from && (sel.rows.length > 1 || sel.from !== sel.to) && window.htmx) {
      var werte = { row_ids: sel.rows.join(","), day_from: sel.from,
                    day_to: sel.to, item_id: ziel };
      sel = null;
      htmx.ajax("POST", "/api/crew/assign-range",
                { target: "#crew-panel", swap: "outerHTML", values: werte });
      return;
    }
    var aus = ziel === activePos;
    setActive(aus ? null : ziel, chip.dataset.mode);
    // Ist die Position selbst ein Abschnitt, wird sie damit auch zum Ziel für
    // neue Ressourcen — sonst müsste man zweimal klicken.
    if (!aus && chip.dataset.mode === "menu") {
      activeMenu = ziel;
      zeigeAbschnitt();
    }
  });

  // Rechtsklick auf einen Chip schaltet Menüpunkt ⇄ Sammelposition.
  document.addEventListener("contextmenu", function (e) {
    var chip = e.target.closest && e.target.closest("#crew-pos .crew-chip");
    if (!chip || !chip.dataset.itemId || chip.classList.contains("crew-chip-new")) return;
    e.preventDefault();
    if (!window.htmx) return;
    htmx.ajax("POST", "/api/crew/pos/mode", {
      target: "#crew-panel", swap: "outerHTML",
      values: { item_id: chip.dataset.itemId,
                mode: chip.dataset.mode === "menu" ? "batch" : "menu" },
    });
  });

  // ── Zeilen per Ziehen einem anderen Menüpunkt zuordnen ───────────────────
  // Ablageziel ist die Überschriftszeile eines Menüpunkts. Die Zeile selbst ist
  // kein Ziel: sonst müsste man innerhalb einer Gruppe zielen, und ein Fehlgriff
  // würde die Zeile still woanders einsortieren.

  var dragRow = null;

  document.addEventListener("dragstart", function (e) {
    var head = e.target.closest && e.target.closest("td.crew-head[draggable]");
    if (!head) return;
    dragRow = head.dataset.row;
    e.dataTransfer.effectAllowed = "move";
    // Firefox startet ohne Nutzdaten kein Ziehen.
    e.dataTransfer.setData("text/plain", dragRow);
    head.closest("tr").classList.add("crew-dragging");
  });

  document.addEventListener("dragend", function () {
    dragRow = null;
    var alle = document.querySelectorAll(".crew-dragging, .crew-drop-on");
    for (var i = 0; i < alle.length; i++) {
      alle[i].classList.remove("crew-dragging", "crew-drop-on");
    }
  });

  function dropZiel(e) {
    var tr = e.target.closest && e.target.closest('tr[data-drop="1"]');
    return dragRow !== null && tr ? tr : null;
  }

  document.addEventListener("dragover", function (e) {
    var tr = dropZiel(e);
    if (!tr) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    tr.classList.add("crew-drop-on");
  });

  document.addEventListener("dragleave", function (e) {
    var tr = e.target.closest && e.target.closest('tr[data-drop="1"]');
    if (tr) tr.classList.remove("crew-drop-on");
  });

  document.addEventListener("drop", function (e) {
    var tr = dropZiel(e);
    if (!tr) return;
    e.preventDefault();
    var row = dragRow;
    dragRow = null;
    tr.classList.remove("crew-drop-on");
    if (!window.htmx) return;
    htmx.ajax("POST", "/api/crew/row/" + row + "/move-to", {
      target: "#crew-panel", swap: "outerHTML",
      values: { group_key: tr.dataset.menu || "" },
    });
  });

  // Phase füllen: braucht ein gewähltes Ziel, sonst wüsste der Server nicht, wohin.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".crew-fill-btn");
    if (!btn) return;
    if (!activePos) {
      var hint = document.getElementById("crew-pos-hint");
      if (hint) hint.textContent = "Erst eine Position anklicken, dann die Phase füllen.";
      return;
    }
    htmx.ajax("POST", "/api/crew/fill-phase", {
      target: "#crew-panel", swap: "outerHTML",
      values: { index: btn.dataset.phase, item_id: activePos, row_id: 0 },
    });
  });

  // ── Farbe einer Zelle lokal setzen ───────────────────────────────────────
  // Der Server bleibt die Wahrheit; hier wird nur nachgezogen, was eine einzelne
  // Eingabe geändert hat. Vorher lud dafür das ganze Panel neu, und der
  // Eingabefokus ging verloren.

  function chipFarbe(itemId) {
    if (!itemId) return "";
    var chips = document.querySelectorAll("#crew-pos .crew-chip");
    for (var i = 0; i < chips.length; i++) {
      if (chips[i].dataset.itemId === itemId) {
        var dot = chips[i].querySelector(".crew-chip-dot");
        return dot ? dot.style.background : "";
      }
    }
    return "";
  }

  function malZelle(rowId, day, itemId, besetzt) {
    var td = document.querySelector('#crew-table td.crew-cell[data-row="' + rowId +
                                    '"][data-day="' + day + '"]');
    if (!td) return;
    if (itemId) td.dataset.item = itemId;
    if (!besetzt) td.dataset.item = "";      // ohne Besetzung keine Zuordnung
    var item = td.dataset.item || "";
    var farbe = (item && besetzt) ? chipFarbe(item) : "";
    // Rahmen statt Füllung — dann bleibt die Zahl auf Weiß lesbar.
    td.style.boxShadow = farbe ? "inset 0 0 0 2px " + farbe : "";
    td.classList.toggle("crew-open", besetzt && !item);
  }

  // ── Mehrere Zellen auswählen und gemeinsam füllen ────────────────────────
  // „Drei Rigger, die ganze Aufbauwoche" ist der häufigste Handgriff der Planung.
  // Einzeln getippt sind das fünf Zellen; hier zieht man den Bereich auf, tippt die
  // Zahl und drückt Enter.

  var sel = null;        // {rows:[ids], from:day, to:day, anchor:{row,day}}
  var zieht = false;         // Maus wird über den Tagen gezogen
  var ziehtZeilen = false;   // … oder über den Kostenspalten

  function zellen() {
    return Array.prototype.slice.call(
      document.querySelectorAll("#crew-table input.crew-in"));
  }

  function tageAchse() {
    return Array.prototype.slice.call(
      document.querySelectorAll("#crew-table thead th.crew-day")).map(function (th) {
        return th.dataset.day;
      });
  }

  function zeilenAchse() {
    return Array.prototype.slice.call(
      document.querySelectorAll("#crew-table tr.crew-row")).map(function (tr) {
        return tr.dataset.row;
      });
  }

  function bereich(a, b) {
    var tage = tageAchse(), zeilen = zeilenAchse();
    var t1 = tage.indexOf(a.day), t2 = tage.indexOf(b.day);
    var z1 = zeilen.indexOf(a.row), z2 = zeilen.indexOf(b.row);
    if (t1 < 0 || t2 < 0 || z1 < 0 || z2 < 0) return null;
    return {
      rows: zeilen.slice(Math.min(z1, z2), Math.max(z1, z2) + 1),
      from: tage[Math.min(t1, t2)],
      to: tage[Math.max(t1, t2)],
      anchor: a,
    };
  }

  // Auswahl in den Kostenspalten: gezogen wird über Tagessatz, Nächte, Preis oder
  // Reisen. Dasselbe wie in der Matrix, nur ist die Spalte hier ein Feld statt eines
  // Tages — und es bleibt bei dem, in dem das Ziehen begonnen hat. Eine Zahl für zwei
  // verschiedene Felder gleichzeitig ergibt keinen Sinn: 600 als Tagessatz ist etwas
  // anderes als 600 Hotelnächte.
  function zeilenBereich(a, b) {
    var zeilen = zeilenAchse();
    var z1 = zeilen.indexOf(a.row), z2 = zeilen.indexOf(b.row);
    if (z1 < 0 || z2 < 0) return null;
    return { rows: zeilen.slice(Math.min(z1, z2), Math.max(z1, z2) + 1),
             from: null, to: null, feld: a.feld, anchor: a };
  }

  function zeigeAuswahl() {
    var aktiv = {};
    if (sel) {
      sel.rows.forEach(function (r) { aktiv[r] = true; });
    }
    zellen().forEach(function (el) {
      var drin = sel && sel.from && aktiv[el.dataset.row]
        && el.dataset.day >= sel.from && el.dataset.day <= sel.to;
      el.closest("td").classList.toggle("crew-sel", !!drin);
    });
    // Kostenspalten: markiert wird genau, was aufgezogen wurde — die gewählten
    // Zeilen in der gewählten Spalte. Vorher lag die Markierung auf der ganzen Zeile
    // und färbte auch Felder, die nichts abbekommen.
    var spalte = sel && !sel.from ? sel.feld : null;
    Array.prototype.slice.call(
      document.querySelectorAll("#crew-table input.crew-rate")).forEach(function (el) {
        el.classList.toggle("crew-rate-sel",
                            !!spalte && el.dataset.field === spalte
                            && !!aktiv[el.dataset.row]);
      });
    var hinweis = document.getElementById("crew-sel-hint");
    if (!hinweis) return;
    if (!sel) {
      hinweis.hidden = true;
      return;
    }
    if (!sel.from) {
      hinweis.hidden = sel.rows.length < 2;
      hinweis.textContent = sel.rows.length + " Felder gewählt — Zahl tippen und mit "
        + "Enter auf alle übertragen (Esc hebt die Auswahl auf)";
      return;
    }
    var tage = tageAchse();
    var n = sel.rows.length *
      (tage.indexOf(sel.to) - tage.indexOf(sel.from) + 1);
    hinweis.hidden = n < 2;
    hinweis.textContent = n + " Felder gewählt — Zahl tippen und mit Enter "
      + "auf alle übertragen (Esc hebt die Auswahl auf)";
  }

  function loescheAuswahl() {
    sel = null;
    zeigeAuswahl();
  }

  function fuelleAuswahl(persons) {
    if (!sel || !sel.from || !window.htmx) return false;
    var werte = {
      row_ids: sel.rows.join(","), day_from: sel.from, day_to: sel.to,
      persons: persons, assign_to: activePos || "",
    };
    sel = null;
    htmx.ajax("POST", "/api/crew/cells/fill",
              { target: "#crew-panel", swap: "outerHTML", values: werte });
    return true;
  }

  document.addEventListener("mousedown", function (e) {
    var el = e.target;

    // Über Hotel, Reisen und Tagessatz lässt sich genauso ziehen wie über die
    // Tage — nur wählt das ganze Zeilen aus. Ohne das musste man die Zeilen erst
    // in der Matrix markieren, um einen Wert für alle zu setzen; das hat niemand
    // erraten.
    var kostenZelle = el.closest && el.closest("#crew-table td.crew-cost");
    if (kostenZelle) {
      // preventDefault auch neben dem Feld: sonst beginnt der Browser über den
      // Beträgen eine Textmarkierung, und die liegt als zweite Auswahl über der
      // eigenen.
      e.preventDefault();
      // Neben dem Feld angefasst: das Feld der Zelle nehmen. MT, Spesen und Summe
      // werden gerechnet — dort gibt es keins, also auch nichts auszuwählen.
      var feld = el.classList.contains("crew-rate")
        ? el : kostenZelle.querySelector("input.crew-rate");
      var zeile = el.closest("#crew-table tr.crew-row");
      if (!feld || !zeile) { loescheAuswahl(); return; }
      var zp = { row: zeile.dataset.row, day: null, feld: feld.dataset.field };
      sel = zeilenBereich(
        (e.shiftKey && sel && sel.feld === zp.feld)
          ? { row: sel.anchor.row, day: null, feld: zp.feld } : zp, zp);
      ziehtZeilen = true;
      zeigeAuswahl();
      feld.focus();
      feld.select();
      return;
    }

    if (!el.classList || !el.classList.contains("crew-in")) return;
    var punkt = { row: el.dataset.row, day: el.dataset.day };

    // Kein Standardverhalten: sonst beginnt der Browser über einer gefüllten Zelle
    // eine Textmarkierung, und das Ziehen bleibt an der ersten Zahl hängen. Der
    // Fokus fällt damit weg und wird gleich selbst gesetzt — ohne das ließe sich in
    // eine Auswahl nichts mehr schreiben.
    e.preventDefault();

    if (e.shiftKey && sel) {
      // Auswahl vom Anker aus erweitern — wie in einer Tabellenkalkulation.
      sel = bereich(sel.anchor, punkt);
      zeigeAuswahl();
      return;
    }
    zieht = true;
    sel = bereich(punkt, punkt);
    zeigeAuswahl();
    el.focus();
    el.select();
  });

  document.addEventListener("mousemove", function (e) {
    var ziel = e.target;
    if (ziehtZeilen) {
      // Über der Zelle statt über dem Feld liegt der Zeiger schnell mal — beides
      // zählt, sonst reißt die Auswahl beim Ziehen ab.
      var td = ziel.closest ? ziel.closest("td.crew-cost") : null;
      var tr = td && td.closest("tr.crew-row");
      if (!tr || !sel) return;
      // Die Spalte bleibt die des Ankers, auch wenn der Zeiger seitlich abkommt —
      // aufgezogen wird nach oben und unten.
      var neuZ = zeilenBereich(sel.anchor,
                               { row: tr.dataset.row, day: null, feld: sel.feld });
      if (!neuZ) return;
      sel = neuZ;
      zeigeAuswahl();
      return;
    }
    if (!zieht) return;
    var el = e.target;
    if (!el.classList || !el.classList.contains("crew-in")) return;
    var neu = bereich(sel ? sel.anchor : { row: el.dataset.row, day: el.dataset.day },
                      { row: el.dataset.row, day: el.dataset.day });
    if (!neu) return;
    sel = neu;
    zeigeAuswahl();
  });

  document.addEventListener("mouseup", function () { zieht = false; ziehtZeilen = false; });

  // ── Navigation ────────────────────────────────────────────────────────────

  function cellsOfRow(tr) {
    return Array.prototype.slice.call(tr.querySelectorAll("input.crew-in"));
  }

  function move(input, dRow, dCol) {
    var tr = input.closest("tr");
    var cells = cellsOfRow(tr);
    var col = cells.indexOf(input);
    if (col < 0) return;

    if (dCol) {
      var next = cells[col + dCol];
      if (next) { focusCell(next); }
      return;
    }
    var rows = Array.prototype.slice.call(
      tr.parentNode.querySelectorAll("tr.crew-row"));
    var idx = rows.indexOf(tr) + dRow;
    if (idx < 0 || idx >= rows.length) return;
    var target = cellsOfRow(rows[idx])[col];
    if (target) focusCell(target);
  }

  function focusCell(el) {
    el.focus();
    el.select();
  }

  function onKey(e) {
    var el = e.target;

    // Hotel, Reisen und Tagessatz lassen sich für die ausgewählten Zeilen auf einmal
    // setzen: Wert tippen, Enter. „Die ganze Crew übernachtet fünf Nächte" ist damit
    // ein Handgriff und nicht acht.
    if (el.classList && el.classList.contains("crew-rate") && e.key === "Enter") {
      e.preventDefault();
      if (!fuelleZeilen(el)) el.blur();   // einzeln: der change-Handler speichert
      return;
    }

    if (!el.classList || !el.classList.contains("crew-in")) return;
    switch (e.key) {
      case "ArrowUp":    e.preventDefault(); loescheAuswahl(); el.blur(); move(el, -1, 0); break;
      case "Enter":
        e.preventDefault();
        var roh = (el.value || "").replace(/\D/g, "");
        if (sel && (sel.rows.length > 1 || sel.from !== sel.to)) {
          if (fuelleAuswahl(roh || 0)) return;   // Auswahl gemeinsam füllen
        }
        el.blur(); move(el, 1, 0);
        break;
      case "ArrowDown":  e.preventDefault(); el.blur(); move(el, 1, 0); break;
      case "Escape":     loescheAuswahl(); break;
      case "ArrowLeft":
        if (el.selectionStart === 0) { e.preventDefault(); el.blur(); move(el, 0, -1); }
        break;
      case "ArrowRight":
        if (el.selectionStart >= el.value.length) { e.preventDefault(); el.blur(); move(el, 0, 1); }
        break;
    }
  }

  // ── Verdrahtung ───────────────────────────────────────────────────────────

  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el.dataset) return;
    if (el.classList.contains("crew-in")) { saveCell(el); }
    else if (el.classList.contains("crew-rate")) {
      // Sind mehrere Zeilen ausgewählt, gilt der Wert für alle — auch wenn man das
      // Feld einfach verlässt statt Enter zu drücken.
      if (!fuelleZeilen(el)) saveField(el);
    }
  });

  document.addEventListener("change", function (e) {
    var el = e.target;
    if (el.name === "date_from" && el.closest("#crew-panel")) syncDates();
  }, true);   // Capture: vor htmx

  document.addEventListener("keydown", onKey);

  document.addEventListener("focusin", function (e) {
    if (e.target.classList && e.target.classList.contains("crew-in")) e.target.select();
  });

  // „Bis" darf nicht vor „Von" liegen — dieselbe Regel wie bei der Projektanlage
  // links (impSyncDates). Läuft in der Capture-Phase, damit htmx danach den schon
  // korrigierten Wert verschickt und nicht den zurückliegenden.
  function addDays(iso, days) {
    var d = new Date(iso + "T00:00:00");
    if (isNaN(d.getTime())) return "";
    d.setDate(d.getDate() + days);
    var m = String(d.getMonth() + 1), t = String(d.getDate());
    return d.getFullYear() + "-" + (m.length < 2 ? "0" + m : m) + "-" + (t.length < 2 ? "0" + t : t);
  }

  function syncDates() {
    var from = document.querySelector('#crew-panel input[name="date_from"]');
    var to   = document.querySelector('#crew-panel input[name="date_to"]');
    if (!from || !to) return;
    var sv = (from.value || "").slice(0, 10), ev = (to.value || "").slice(0, 10);
    to.min = sv;
    if (sv && (!ev || ev < sv)) to.value = addDays(sv, 1);
  }

  // Im Startzustand die Datumsfelder aus der Projektanlage links übernehmen. Der
  // Server kennt die dort getippten Termine noch nicht — sie stehen erst im Entwurf,
  // wenn gespeichert wurde. Ohne das schlägt die Matrix „heute bis heute" vor.
  function prefillRange() {
    if (document.getElementById("crew-table")) return;   // Planung existiert schon
    var from = document.querySelector('#crew-panel input[name="date_from"]');
    var to   = document.querySelector('#crew-panel input[name="date_to"]');
    var s = document.getElementById("imp-start");
    var e = document.getElementById("imp-end");
    if (!from || !to || !s) return;
    if (s.value) from.value = s.value;
    if (e && e.value) to.value = e.value;
  }

  // Nach jedem Panel-Tausch (Zeile hinzu, LV übernommen, Zeitraum geändert)
  // die Anzeige-Schalter wiederherstellen.
  // Beim Tausch des Panels entsteht der Scroll-Bereich neu und fängt wieder links
  // oben an. Wer weit rechts eine Position zuordnet, landet danach am Anfang der
  // Zeitachse und muss sich zurücksuchen — also Stand merken und wiederherstellen.
  var scrollStand = null;

  function merkeScroll() {
    var box = document.querySelector("#crew-panel .crew-scroll");
    scrollStand = {
      x: box ? box.scrollLeft : 0,
      y: box ? box.scrollTop : 0,
      seite: window.pageYOffset || document.documentElement.scrollTop || 0,
    };
  }

  // Wie breit die Kopfspalte wirklich ist, weiß erst der Browser: im automatischen
  // Tabellenlayout entscheidet der Inhalt. Der Anschlag der Marken wird deshalb
  // gemessen — mit einer festen Zahl rutschen sie bis dahin mit.
  function messeKopf() {
    var tab = document.getElementById("crew-table");
    if (!tab) return;
    // Nicht die Breite der Kopfspalte, sondern wo die Zelle mit den Marken wirklich
    // anfängt: das schließt Rahmen und Zellabstände mit ein. Beide Rechtecke wandern
    // beim Scrollen gleich weit, der Abstand bleibt also derselbe.
    var leiste = tab.querySelector("td.crew-menu-bar");
    var kopf = tab.querySelector("tr.crew-row td.crew-head, thead th.crew-head");
    var x = leiste
      ? leiste.getBoundingClientRect().left - tab.getBoundingClientRect().left
      : (kopf ? kopf.getBoundingClientRect().width : 0);
    if (x > 0) tab.style.setProperty("--crew-kopf-ist", Math.round(x) + "px");
  }

  window.addEventListener("resize", messeKopf);

  function stelleScrollHer() {
    if (!scrollStand) return;
    var box = document.querySelector("#crew-panel .crew-scroll");
    if (box) {
      box.scrollLeft = scrollStand.x;
      box.scrollTop = scrollStand.y;
    }
    window.scrollTo(window.pageXOffset || 0, scrollStand.seite);
    scrollStand = null;
  }

  document.body.addEventListener("htmx:beforeSwap", function (e) {
    if (e.detail && e.detail.target && e.detail.target.id === "crew-panel") {
      merkeScroll();
    }
  });

  document.body.addEventListener("htmx:afterSettle", function (e) {
    if (e.detail && e.detail.target && e.detail.target.id === "crew-panel") {
      messeKopf();
      stelleScrollHer();
      prefillRange();
      syncDates();
      if (activePos !== null) setActive(activePos, activeMode);
      zeigeAbschnitt();
      // Frisch angelegter Abschnitt: gleich hineintippen können. Das Titelfeld
      // nimmt sonst erst nach einem Doppelklick Eingaben an (siehe style.css).
      var fresh = document.querySelector('#crew-table input[data-fresh="1"]');
      if (fresh) {
        fresh.classList.add("crew-head-edit");
        fresh.focus();
        fresh.select();
      }
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    prefillRange();
    syncDates();
    messeKopf();
  });
})();
