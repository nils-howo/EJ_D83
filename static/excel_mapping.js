/* Mapping-Dialog für Excel-LVs: Farb-Pinsel statt Dropdowns.
 *
 * Der komplette Färbezustand liegt im data-layout des Containers und geht beim
 * Absenden als layout_json an den Server. Spalten- und Kopfzeilen-Änderungen
 * verschieben die Zeilenklassifikation, deshalb rechnet die der Server neu
 * (/api/import/excel/repreview). Zeilen-Overrides sind direkte Zuweisungen und
 * werden sofort lokal angezeigt.
 *
 * Alle Listener hängen delegiert am document — der Dialog wird per htmx komplett
 * ausgetauscht, eigene Listener am Container würden das nicht überleben.
 */
(function () {
  'use strict';

  var SINGLE    = ['desc', 'oz', 'unit', 'price', 'flag'];   // Rollen mit genau einer Spalte
  var ROW_KINDS = ['header', 'job', 'main', 'grp', 'pos', 'note', 'skip'];
  var COUNTED   = ['pos', 'job', 'main', 'note'];   // Zeilentypen mit Zähler

  var brush    = null;   // {mode:'col'|'row', value:'desc'|'grp'|…}
  var dragging = false;
  var busy     = false;

  function root()   { return document.getElementById('xl-mapping'); }

  // Blätter, die alle Zeilen zeigen sollen. Reine Anzeigesache — steht deshalb nicht
  // im Layout (das als Vorlagen-Profil gespeichert wird), sondern in einem Datenattribut.
  function showAll() {
    var r = root();
    var v = r ? (r.dataset.showall || '') : '';
    return v ? v.split('|') : [];
  }

  function setShowAll(list) {
    var r = root();
    if (r) { r.dataset.showall = list.join('|'); }
  }
  function layout() { var r = root(); return r ? JSON.parse(r.dataset.layout) : null; }
  function store(l) { var r = root(); if (r) { r.dataset.layout = JSON.stringify(l); } }

  function sheetOf(l, name) {
    for (var i = 0; i < l.sheets.length; i++) {
      if (l.sheets[i].name === name) { return l.sheets[i]; }
    }
    return null;
  }

  function attrEsc(s) { return String(s).replace(/"/g, '&quot;'); }

  function flashPalette() {
    var r = root();
    var p = r && r.querySelector('.xl-palette');
    if (!p) { return; }
    p.classList.remove('xl-flash');
    void p.offsetWidth;              // Reflow erzwingen, damit die Animation neu startet
    p.classList.add('xl-flash');
  }

  // ── Pinsel wählen ─────────────────────────────────────────────────────────
  document.addEventListener('click', function (e) {
    var sw = e.target.closest('.xl-swatch');
    if (!sw || !root() || !root().contains(sw)) { return; }
    if (sw.disabled || sw.classList.contains('xl-swatch-off')) { return; }
    e.preventDefault();
    var same = brush && brush.mode === sw.dataset.brush && brush.value === sw.dataset.value;
    root().querySelectorAll('.xl-swatch').forEach(function (b) {
      b.classList.remove('xl-active');
    });
    if (same) {
      brush = null;
      document.body.classList.remove('xl-painting');
      return;
    }
    brush = { mode: sw.dataset.brush, value: sw.dataset.value };
    sw.classList.add('xl-active');
    document.body.classList.add('xl-painting');
  });

  // ── Spalte einfärben ──────────────────────────────────────────────────────
  function paintColumn(sheetName, col, role, reset) {
    var l  = layout();
    var sl = sheetOf(l, sheetName);
    if (!sl) { return false; }

    if (reset) {
      // Sheet-Eintrag löschen → der Server erkennt dieses Blatt komplett neu
      l.sheets = l.sheets.filter(function (s) { return s.name !== sheetName; });
      store(l);
      return true;
    }

    // Spalte überall entfernen — eine Spalte hat genau eine Rolle
    var r = sl.roles;
    SINGLE.forEach(function (k) { if (r[k] === col) { r[k] = 0; } });
    r.ref = (r.ref || []).filter(function (c) { return c !== col; });
    var hadQty = (r.qty || []).some(function (q) { return q.col === col; });
    r.qty = (r.qty || []).filter(function (q) { return q.col !== col; });

    if (SINGLE.indexOf(role) >= 0) {
      r[role] = col;
    } else if (role === 'ref') {
      r.ref.push(col);
      r.ref.sort(function (a, b) { return a - b; });
    } else if (role === 'qty') {
      if (!hadQty) {
        r.qty.push({ col: col, label: '', job_name: '', active: true, values: 0 });
      }
      r.qty.sort(function (a, b) { return a.col - b.col; });
    }
    // role === 'ignore' → nur entfernen, nichts setzen
    store(l);
    return true;
  }

  document.addEventListener('click', function (e) {
    var th = e.target.closest('.xl-colhead');
    if (!th) { return; }
    var sheet = th.closest('.xl-sheet');
    if (!sheet) { return; }
    if (!brush || brush.mode !== 'col') { flashPalette(); return; }
    if (paintColumn(sheet.dataset.sheet, parseInt(th.dataset.col, 10), brush.value, e.altKey)) {
      repreview();
    }
  });

  // ── Zeile einfärben (mit Ziehen über mehrere Zeilen) ──────────────────────
  function countDelta(sheet, kind, delta) {
    var box = sheet.querySelector('.xl-counts');
    if (!box || COUNTED.indexOf(kind) < 0) { return; }
    var n = Math.max(0, (parseInt(box.dataset[kind], 10) || 0) + delta);
    box.dataset[kind] = n;
    var el = box.querySelector('.c-' + kind);
    if (el) { el.textContent = n; }
  }

  function paintRow(th, alt) {
    var tr    = th.closest('tr');
    var sheet = th.closest('.xl-sheet');
    if (!tr || !sheet) { return; }
    var row = th.dataset.row;
    var l   = layout();
    var sl  = sheetOf(l, sheet.dataset.sheet);
    if (!sl) { return; }
    sl.row_overrides = sl.row_overrides || {};

    if (alt) {                                   // zurück auf die Erkennung
      if (!(row in sl.row_overrides)) { return; }
      delete sl.row_overrides[row];
      store(l);
      repreview();
      return;
    }
    if (brush.value === 'header') {              // Kopfzeile verschiebt den Datenbereich
      sl.header_row    = parseInt(row, 10);
      sl.row_overrides = {};
      store(l);
      repreview();
      return;
    }

    var prev = null;
    ROW_KINDS.forEach(function (k) {
      if (tr.classList.contains('xl-row-' + k)) { prev = k; }
    });
    if (prev === brush.value) { return; }

    sl.row_overrides[row] = brush.value;
    store(l);
    ROW_KINDS.forEach(function (k) { tr.classList.remove('xl-row-' + k); });
    tr.classList.add('xl-row-' + brush.value);
    countDelta(sheet, prev, -1);
    countDelta(sheet, brush.value, 1);
  }

  document.addEventListener('mousedown', function (e) {
    var th = e.target.closest('.xl-rowhead');
    if (!th) { return; }
    if (!brush || brush.mode !== 'row') { flashPalette(); return; }
    e.preventDefault();
    dragging = !e.altKey && brush.value !== 'header';
    paintRow(th, e.altKey);
  });

  document.addEventListener('mouseover', function (e) {
    if (!dragging || !brush) { return; }
    var th = e.target.closest('.xl-rowhead');
    if (th) { paintRow(th, false); }
  });

  document.addEventListener('mouseup', function () { dragging = false; });

  // ── Blätter, Jobnamen, Zurücksetzen ──────────────────────────────────────
  document.addEventListener('change', function (e) {
    var cb = e.target.closest('.xl-sheet-toggle');
    if (!cb) { return; }
    var l  = layout();
    var sl = sheetOf(l, cb.dataset.sheet);
    if (!sl) { return; }
    sl.enabled = cb.checked;
    store(l);
    var tab = cb.closest('.xl-tab');
    if (tab) { tab.classList.toggle('xl-tab-on', cb.checked); }
    var sheet = root().querySelector('.xl-sheet[data-sheet="' + attrEsc(cb.dataset.sheet) + '"]');
    if (sheet) { sheet.classList.toggle('xl-sheet-off', !cb.checked); }
  });

  document.addEventListener('change', function (e) {
    var rb = e.target.closest('input[name="xl-sheet-mode"]');
    if (!rb || !rb.checked) { return; }
    var l = layout();
    l.sheet_mode = rb.value;
    store(l);
    repreview();
  });

  document.addEventListener('input', function (e) {
    var inp = e.target.closest('.xl-job-name');
    if (!inp) { return; }
    var l  = layout();
    var sl = sheetOf(l, inp.dataset.sheet);
    if (!sl) { return; }
    var col = parseInt(inp.dataset.col, 10);
    (sl.roles.qty || []).forEach(function (q) {
      if (q.col === col) { q.job_name = inp.value; }
    });
    store(l);
  });

  document.addEventListener('click', function (e) {
    var b = e.target.closest('.xl-showall, .xl-showless');
    if (!b || !root() || !root().contains(b)) { return; }
    e.preventDefault();
    var name = b.dataset.sheet;
    var list = showAll().filter(function (n) { return n !== name; });
    if (b.classList.contains('xl-showall')) { list.push(name); }
    setShowAll(list);
    repreview();
  });

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.xl-reset');
    if (!btn || !root() || !root().contains(btn)) { return; }
    e.preventDefault();
    var l = layout();
    store({ sheets: [], fingerprint: l.fingerprint, label: l.label });
    repreview();
  });

  // ── Absenden ─────────────────────────────────────────────────────────────
  // Bewusst eigenes fetch statt htmx: die Antwort ersetzt #import-groups und damit
  // dieses Formular. Ein hx-on::after-request am Formular feuert dann nicht mehr
  // zuverlässig — die Datei-Anzeige blieb auf „Zuordnung prüfen" stehen.
  document.addEventListener('submit', function (e) {
    var form = e.target.closest('.xl-apply');
    if (!form) { return; }
    e.preventDefault();
    if (busy) { return; }
    busy = true;

    var btn    = document.getElementById('xl-apply-btn');
    var msg    = document.getElementById('xl-apply-msg');
    var target = document.getElementById('import-groups');
    var label  = (form.querySelector('input[name="label"]') || {}).value || '';
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Wird eingelesen …'; }

    var body = new FormData();
    body.append('layout_json', JSON.stringify(layout()));
    body.append('label', label);

    fetch('/api/import/excel/apply', { method: 'POST', body: body })
      .then(function (r) {
        if (window.impAuthLost && window.impAuthLost(r)) { return null; }
        return r.text();
      })
      .then(function (html) {
        if (html === null || !target) { return; }
        target.innerHTML = html;
        if (typeof htmx !== 'undefined') { htmx.process(target); }
        // Fehler? Dann bleibt der Dialog-Status „Zuordnung prüfen" richtig.
        if (html.indexOf('error-msg') >= 0) { return; }
        brush = null;
        document.body.classList.remove('xl-painting');
        if (typeof impExcelApplied === 'function') { impExcelApplied(); }
      })
      .catch(function (err) {
        if (msg) { msg.innerHTML = '<span style="color:#c62828">' + err.message + '</span>'; }
      })
      .finally(function () {
        busy = false;
        if (btn && btn.isConnected) {
          btn.disabled = false;
          btn.textContent = 'Einlesen & Matching starten';
        }
      });
  });

  // ── Oberflächenzustand über den Austausch retten ─────────────────────────
  // Der Repreview ersetzt den ganzen Dialog. Ohne das hier klappen aufgeklappte
  // Blätter wieder zu und die Tabelle springt zurück auf Spalte A — mitten im
  // Zuordnen ist das der störendste Teil.
  function captureUi() {
    var r = root();
    var st = { open: [], scroll: {}, pageY: window.scrollY };
    if (!r) { return st; }
    r.querySelectorAll('.xl-sheet').forEach(function (d) {
      var name = d.dataset.sheet;
      if (d.open) { st.open.push(name); }
      var w = d.querySelector('.xl-grid-wrap');
      if (w && (w.scrollLeft || w.scrollTop)) {
        st.scroll[name] = [w.scrollLeft, w.scrollTop];
      }
    });
    return st;
  }

  function restoreUi(st) {
    var r = root();
    if (!r || !st) { return; }
    r.querySelectorAll('.xl-sheet').forEach(function (d) {
      var name = d.dataset.sheet;
      d.open = st.open.indexOf(name) >= 0;
      var pos = st.scroll[name];
      var w = d.querySelector('.xl-grid-wrap');
      if (w && pos) { w.scrollLeft = pos[0]; w.scrollTop = pos[1]; }
    });
    if (typeof st.pageY === 'number') { window.scrollTo(0, st.pageY); }
  }

  // ── Repreview: Klassifikation serverseitig neu rechnen ───────────────────
  function repreview() {
    if (busy) { return; }
    busy = true;
    var target = document.getElementById('import-groups');
    var body   = new FormData();
    var ui     = captureUi();
    body.append('layout_json', JSON.stringify(layout()));
    body.append('show_all', showAll().join('|'));
    if (target) { target.classList.add('xl-loading'); }

    fetch('/api/import/excel/repreview', { method: 'POST', body: body })
      .then(function (r) {
        if (window.impAuthLost && window.impAuthLost(r)) { return null; }
        return r.text();
      })
      .then(function (html) {
        if (html === null || !target) { return; }
        target.innerHTML = html;
        if (typeof htmx !== 'undefined') { htmx.process(target); }
        restoreUi(ui);
        if (brush) {                    // Pinsel nach dem Austausch wieder markieren
          var sel = '.xl-swatch[data-brush="' + brush.mode +
                    '"][data-value="' + brush.value + '"]';
          var sw = target.querySelector(sel);
          if (sw) {
            sw.classList.add('xl-active');
          } else {
            brush = null;               // Pinsel gibt es nicht mehr
            document.body.classList.remove('xl-painting');
          }
        }
      })
      .catch(function (err) {
        if (target) {
          target.insertAdjacentHTML('afterbegin',
            '<div class="error-msg">Vorschau fehlgeschlagen: ' + err.message + '</div>');
        }
      })
      .finally(function () {
        busy = false;
        if (target) { target.classList.remove('xl-loading'); }
      });
  }
})();
