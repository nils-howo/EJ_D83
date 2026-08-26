"""Gruppen-Identität: Ordnungszahl + Label, nie das Label allein.

In GAEB-LVs heißen Titel in mehreren Abschnitten gleich — im polis-LV steht
"Lichttechnik" in 02.01, 03.01, 05.01 und 06.01. Solange die Gruppierung nur am
Label hing, brach die ganze Kette:

  * die Hauptgruppen-Liste warf alle vier in eine Gruppe (02.01.10 … 06.01.30) und
    ließ die restlichen ganz verschwinden,
  * die Job-Zuordnung konnte sie nicht trennen (ein Klick zog alle vier mit),
  * beim Buchen landeten die Positionen aller vier im Job der ersten,
  * nachlaufende Hinweistexte landeten in der erstbesten gleichnamigen Gruppe.

Dieser Test hält alle vier Stellen fest — inklusive der EJ-Gruppenbezeichnungen
und der OZ-Rückauflösung, über die der Export später die Preise findet.

    .venv/Scripts/python.exe tests/test_group_identity.py
"""
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from gaeb_parser import GaebItem, GaebProject, GaebRemark, parse_gaeb
from routes.import_ import (_all_positions, _grp_key, _grp_of,
                            _import_gaeb_groups, _item_group_parts,
                            _migrate_group_jobs)
import glob as _glob
import xml.etree.ElementTree as _ET

POLIS = "infos/260823_LV_polis Convention 2027_TECHNIK_v11.x83"

_fails: list[str] = []
_oks = 0


def check(cond, msg: str) -> None:
    global _oks
    if cond:
        _oks += 1
    else:
        _fails.append(msg)
        print(f"  FAIL  {msg}")


def _mk(item_id: str, oz: str, path: list[str], desc: str = "Pos") -> GaebItem:
    return GaebItem(item_id=item_id, rno_part=10, oz=oz, description=desc,
                    long_text="", qty=1.0, unit="St", category_path=path)


def test_gleichnamige_titel_bleiben_getrennt():
    """polis-LV: "Lichttechnik" 4x — jede Gruppe für sich, keine Position doppelt."""
    print("\n=== polis-LV: gleichnamige Titel bleiben getrennt")
    p = parse_gaeb(POLIS)
    for level, erwartet in ((0, 34), (1, 12)):
        groups = _import_gaeb_groups(p, level=level, alt_active={})
        check(len(groups) == erwartet,
              f"level={level}: {erwartet} Gruppen erwartet, sind {len(groups)}")

        ozs = []
        for g in groups:
            ozs += [b["primary"]["oz"] for b in g["blocks"]]
            for s in g["sub"]:
                ozs += [b["primary"]["oz"] for b in s["blocks"]]
        check(len(ozs) == len(p.items),
              f"level={level}: {len(p.items)} Positionen erwartet, sind {len(ozs)}")
        check(len(set(ozs)) == len(ozs), f"level={level}: Positionen doppelt einsortiert")

        keys = [_grp_of(g) for g in groups]
        check(len(set(keys)) == len(keys),
              f"level={level}: Gruppenschlüssel nicht eindeutig: {keys}")

    g0 = _import_gaeb_groups(p, level=0, alt_active={})
    licht = [g for g in g0 if g["name"] == "Lichttechnik"]
    nums = [g["num"] for g in licht]
    print(f"  Lichttechnik: {len(licht)} Gruppen · {[(g['num'], g['count']) for g in licht]}")
    check(len(licht) == 4, f"4x Lichttechnik erwartet, sind {len(licht)}")
    check(nums == ["02.01", "03.01", "05.01", "06.01"],
          f"Ordnungszahlen 02.01/03.01/05.01/06.01 erwartet, sind {nums}")
    # Genau der gemeldete Fehler: eine Gruppe, die von 02.01.10 bis 06.01.30 reichte
    for g in licht:
        ozs = [b["primary"]["oz"] for b in g["blocks"]]
        check(all(o.startswith(g["num"] + ".") for o in ozs),
              f"Gruppe {g['num']} enthält fremde Positionen: {ozs}")


def test_schluessel_traegt_die_nummer():
    print("\n=== Schlüssel = Ordnungszahl|Label")
    check(_grp_key("02.01", "Lichttechnik") != _grp_key("03.01", "Lichttechnik"),
          "gleiche Labels mit verschiedener OZ müssen verschiedene Schlüssel haben")
    check(_grp_key("", "Rigging") == "Rigging",
          "ohne Ordnungszahl bleibt das Label der Schlüssel")
    # _item_group_parts und _import_gaeb_groups müssen sich einig sein — sonst
    # schreibt _assign_jobs Schlüssel, die die Anzeige nie nachschlägt.
    p = parse_gaeb(POLIS)
    for level in (0, 1):
        aus_gruppen = {_grp_of(g) for g in _import_gaeb_groups(p, level, {})}
        aus_items = set()
        for i in p.items:
            gp = _item_group_parts(i, level)
            aus_items.add(_grp_key(gp["hg_num"], gp["hg_label"]))
        check(aus_gruppen == aus_items,
              f"level={level}: _item_group_parts weicht ab, "
              f"nur in Gruppen {sorted(aus_gruppen - aus_items)[:3]}, "
              f"nur in Items {sorted(aus_items - aus_gruppen)[:3]}")
    print("  level 0/1: Schlüssel aus Items und Gruppen identisch")


def test_jobs_lassen_sich_einzeln_zuweisen():
    """Ein Job auf 02.01 darf 03.01/05.01/06.01 nicht mitziehen."""
    print("\n=== Job-Zuordnung trennt gleichnamige Gruppen")
    p = parse_gaeb(POLIS)
    groups = _import_gaeb_groups(p, level=0, alt_active={})
    licht = [g for g in groups if g["name"] == "Lichttechnik"]

    # Das macht die UI: der Formularwert der angeklickten Gruppe → lid 2
    group_jobs = {_grp_of(licht[0]): 2}
    zugeordnet = [(g["num"], group_jobs.get(_grp_of(g), 1)) for g in licht]
    print(f"  Klick auf 02.01 ergibt {zugeordnet}")
    check(zugeordnet == [("02.01", 2), ("03.01", 1), ("05.01", 1), ("06.01", 1)],
          f"nur 02.01 darf in Job 2 liegen, ist {zugeordnet}")

    # Und der Buchungspfad: oz → Gruppenschlüssel → lid
    oz_to_hg_key = {}
    for g in groups:
        for pos in _all_positions(g.get("blocks", [])):
            oz_to_hg_key[pos["oz"]] = _grp_of(g)
    check(len(oz_to_hg_key) == len(p.items),
          f"jede Position braucht einen Gruppenschlüssel, "
          f"sind {len(oz_to_hg_key)} von {len(p.items)}")
    lids = {oz: group_jobs.get(k, 1) for oz, k in oz_to_hg_key.items()}
    in_job2 = sorted(oz for oz, l in lids.items() if l == 2)
    check(all(oz.startswith("02.01.") for oz in in_job2),
          f"nur Positionen aus 02.01 dürfen in Job 2 gebucht werden, sind {in_job2}")
    check(len(in_job2) == 7, f"7 Positionen in 02.01 erwartet, sind {len(in_job2)}")
    print(f"  gebucht in Job 2: {len(in_job2)} Positionen · {in_job2[0]}..{in_job2[-1]}")


def test_ej_bezeichnungen_eindeutig():
    """Die EJ-Gruppenbezeichnung "[OZ] Name" ist der Schlüssel, über den der Export
    die Kosten wiederfindet (cap_to_gid bzw. oz_to_group). Zwei Gruppen mit
    derselben Bezeichnung im selben Job — eine überschreibt die andere."""
    print("\n=== EJ-Gruppenbezeichnungen sind eindeutig")
    dateien = sorted(set(glob.glob("infos/*.x8*") + glob.glob("infos/*.X8*")
                         + glob.glob("infos/*.D83")))
    geprueft = 0
    for f in dateien:
        try:
            p = parse_gaeb(f)
        except Exception:
            continue
        if not p.items:
            continue
        geprueft += 1
        kurz = os.path.basename(f)[:30]
        for level, mode in ((0, "positions"), (1, "groups")):
            groups = _import_gaeb_groups(p, level=level, alt_active={})
            hg_caps = [f'[{g["num"]}] {g["name"]}' if g.get("num") else g["name"]
                       for g in groups]
            dop = {c[:40] for c in hg_caps if hg_caps.count(c) > 1}
            check(not dop, f"{kurz} {mode}: doppelte HG-Bezeichnung {dop}")
            if mode == "groups":
                for g in groups:
                    caps = [f'[{s["num"]}] {s["name"]}' if s.get("num") else s["name"]
                            for s in g["sub"]]
                    d2 = {c[:40] for c in caps if caps.count(c) > 1}
                    check(not d2, f"{kurz}: doppelte Untergruppe in {g['num']}: {d2}")
            else:
                # positions-Modus: eine EJ-Gruppe je Position, Bezeichnung trägt die OZ
                caps = []
                for g in groups:
                    for pos in _all_positions(g.get("blocks", [])):
                        caps.append(f'[{pos["oz"]}] {pos["desc"]}')
                    for s in g.get("sub", []):
                        for pos in _all_positions(s.get("blocks", [])):
                            caps.append(f'[{pos["oz"]}] {pos["desc"]}')
                d3 = {c[:40] for c in caps if caps.count(c) > 1}
                check(not d3, f"{kurz}: doppelte Positionsgruppe {d3}")
    print(f"  {geprueft} GAEB-Dateien geprüft, beide Modi")
    check(geprueft >= 8, f"zu wenige GAEB-Dateien geprüft: {geprueft}")

    # Excel-Seite: gleiche Kette, gleicher Export. Dort tragen die Labels ihre
    # Herkunftskoordinate, es darf also erst gar keine Gruppe ohne Ordnungszahl geben
    # — sonst wäre die EJ-Bezeichnung das nackte Label und wieder kollisionsgefährdet.
    import excel_parser as _xl
    for f in ["PAG_NOC2026_LV Veranstaltungstechnik_V1.xlsx",
              "20260731 Vector NHX 2026 offen.xlsx",
              "LOS2_LK_Technik_JPK_CMD.xlsx",
              "LOS1_GOE Ausschreibung_Technik 2026_Preisblatt_V2.1.xlsx"]:
        data = open(os.path.join("infos", f), "rb").read()
        p = _xl.parse_excel(data, _xl.probe_workbook(data).layout)
        kurz = f[:30]
        for level in (0, 1):
            groups = _import_gaeb_groups(p, level=level, alt_active={})
            keys = [_grp_of(g) for g in groups]
            check(len(set(keys)) == len(keys), f"{kurz} level={level}: Schlüssel doppelt")
            ohne = [g["name"][:30] for g in groups if not g.get("num")]
            check(not ohne, f"{kurz} level={level}: Gruppe ohne Ordnungszahl: {ohne}")
        print(f"  {kurz:32} Excel · Schlüssel eindeutig, alle mit OZ")


def test_export_findet_ueber_oz_zurueck():
    """Der Export löst Caption → OZ → Position auf (_export_costs.oz_to_group).
    Jede Positions-OZ muss aus ihrer EJ-Bezeichnung eindeutig rückgewinnbar sein."""
    print("\n=== Export: OZ aus der EJ-Bezeichnung rückgewinnbar")
    p = parse_gaeb(POLIS)
    groups = _import_gaeb_groups(p, level=0, alt_active={})
    oz_to_group = {}
    kollision = []
    for g in groups:
        for pos in _all_positions(g.get("blocks", [])):
            cap = f'[{pos["oz"]}] {pos["desc"]}'
            oz = cap[1:cap.index("]")].strip()      # genau wie _export_costs
            if oz in oz_to_group:
                kollision.append(oz)
            oz_to_group[oz] = pos["item_id"]
    check(not kollision, f"OZ mehrfach vergeben: {kollision[:5]}")
    check(len(oz_to_group) == len(p.items),
          f"{len(p.items)} Positionen erwartet, rückgewinnbar sind {len(oz_to_group)}")
    print(f"  {len(oz_to_group)} von {len(p.items)} Positionen eindeutig auflösbar")


def test_elternebenen_werden_mitgeliefert():
    """Die Ebenen über der HG sind die Orientierung, seit gleichnamige Titel
    mehrfach in der Liste stehen. Ihre Ordnungszahl muss die echte Elternnummer
    sein — also ein Präfix der HG-Nummer, eine Ebene kürzer je Schritt nach außen."""
    print("\n=== Elternebenen über der Hauptgruppe")
    p = parse_gaeb(POLIS)
    g0 = _import_gaeb_groups(p, level=0, alt_active={})
    licht = [g for g in g0 if g["name"] == "Lichttechnik"]
    eltern = [(g["num"], [(x["num"], x["name"][:22]) for x in g["parents"]]) for g in licht]
    for num, par in eltern:
        print(f"  [{num}] Lichttechnik  unter  {par}")
    check([p_[0][0] for _, p_ in eltern] == ["02", "03", "05", "06"],
          f"Elternnummern 02/03/05/06 erwartet, sind {[p_[0][0] for _, p_ in eltern]}")
    check(len({tuple(p_) for _, p_ in eltern}) == 4,
          "die vier Lichttechnik-Gruppen müssen an ihrer Elternebene unterscheidbar sein")

    # Über alle Dateien: Elternnummer ist ein echtes Präfix, Kette lückenlos
    dateien = sorted(set(glob.glob("infos/*.x8*") + glob.glob("infos/*.X8*")
                         + glob.glob("infos/*.D83")))
    tief = 0
    for f in dateien:
        try:
            pr = parse_gaeb(f)
        except Exception:
            continue
        if not pr.items:
            continue
        for level in (0, 1):
            for g in _import_gaeb_groups(pr, level=level, alt_active={}):
                pars = g["parents"]
                if len(pars) > 1:
                    tief += 1
                nums = [x["num"] for x in pars if x["num"]]
                for a, b in zip(nums, nums[1:]):
                    check(b.startswith(a + "."),
                          f"{os.path.basename(f)[:26]}: {b} ist kein Kind von {a}")
                if nums and g.get("num"):
                    check(g["num"].startswith(nums[-1] + "."),
                          f"{os.path.basename(f)[:26]}: HG {g['num']} passt nicht "
                          f"unter Elternebene {nums[-1]}")
    print(f"  Prüfung über alle GAEB-Dateien · {tief} Gruppen mit mehrstufigem Elternpfad")
    check(tief > 0, "keine Datei mit mehr als einer Ebene über der HG geprüft")


def test_excel_folgt_derselben_hg_regel():
    """Excel und D83 müssen dieselbe Hierarchie-Regel benutzen.

    Excel flachte früher auf eine Ebene ab: die ÄUSSERSTE Gruppe wurde Hauptgruppe,
    alles darunter zum Hinweis herabgestuft. Damit hatte derselbe LV-Aufbau je
    Quelldatei eine andere Gliederung — feine Gruppen wie "LC-Displays" verschwanden
    in Hinweisen, während sie aus einer X83 als Gruppe ankamen. Jetzt gilt überall:
    tiefste Ebene = Hauptgruppe, alles darüber = Elternpfad.
    """
    print("\n=== Excel folgt derselben HG-Regel wie D83")
    import excel_parser as _xl
    from gaeb_parser import GaebProject as _GP

    def regel_haelt(pr, level: int, quelle: str) -> None:
        """HG-Label und Elternpfad müssen sich exakt aus category_path ergeben."""
        for g in _import_gaeb_groups(pr, level=level, alt_active={}):
            for blk in g["blocks"]:
                iid = blk["primary"]["item_id"]
                it = next(i for i in pr.items if i.item_id == iid)
                cp = list(it.category_path)
                tief = cp[-1] if level == 0 or len(cp) < 2 else cp[-2]
                check(g["name"] == tief,
                      f"{quelle} level={level}: HG {g['name']!r} ist nicht die "
                      f"tiefste Ebene von {cp}")
                oben = cp[:-1] if (level == 0 or len(cp) < 2) else cp[:-2]
                check([x["name"] for x in g["parents"]] == oben,
                      f"{quelle} level={level}: Elternpfad "
                      f"{[x['name'] for x in g['parents']]} != {oben}")
                break                      # eine Position je Gruppe genügt

    for f in ["PAG_NOC2026_LV Veranstaltungstechnik_V1.xlsx",
              "LOS2_LK_Technik_JPK_CMD.xlsx"]:
        data = open(os.path.join("infos", f), "rb").read()
        pr = _xl.parse_excel(data, _xl.probe_workbook(data).layout)
        tiefe = max(len(i.category_path) for i in pr.items)
        check(tiefe >= 3, f"{f[:26]}: volle Kette erwartet, tiefster Pfad {tiefe}")
        for level in (0, 1):
            regel_haelt(pr, level, f[:22])
        print(f"  {f[:30]:32} Tiefe {tiefe} · Regel hält in beiden Modi")

    pg = parse_gaeb(POLIS)
    for level in (0, 1):
        regel_haelt(pg, level, "polis.x83")
    print("  polis.x83                        Regel hält in beiden Modi")

    # Und die Reihenfolge: Excel folgt der Datei, GAEB der Ordnungszahl. Ein
    # Excel-LV mit nicht-monotonen Positionsnummern darf die Liste nicht verdrehen.
    data = open(os.path.join("infos", "LOS2_LK_Technik_JPK_CMD.xlsx"), "rb").read()
    pr = _xl.parse_excel(data, _xl.probe_workbook(data).layout)
    doks, seen = [], set()
    for i in pr.items:
        k = tuple(i.category_path)
        if k not in seen:
            seen.add(k); doks.append(k[-1] if k else "")
    ist = [g["name"] for g in _import_gaeb_groups(pr, level=0, alt_active={})]
    check(ist == doks, f"Excel-Gruppen müssen der Datei folgen; erste Abweichung bei "
                       f"{next((n for n, (a, b) in enumerate(zip(doks, ist)) if a != b), None)}")
    print(f"  LOS2: {len(ist)} Gruppen in Dateireihenfolge")


def test_hinweise_landen_in_ihrer_gruppe():
    print("\n=== Nachlaufende Hinweise gehen in die richtige Gruppe")
    p = GaebProject(name="t", label="", phase="", date="", currency="EUR",
                    items=[_mk("a", "02.01.10", ["Konferenz 1", "Lichttechnik"]),
                           _mk("b", "03.01.10", ["Konferenz 2", "Lichttechnik"])],
                    remarks=[GaebRemark(title="R-K1", long_text="x",
                                        category_path=["Konferenz 1", "Lichttechnik"]),
                             GaebRemark(title="R-K2", long_text="y",
                                        category_path=["Konferenz 2", "Lichttechnik"])])
    erwartet = [("02.01", ["R-K1"]), ("03.01", ["R-K2"])]
    for level in (0, 1):
        groups = _import_gaeb_groups(p, level=level, alt_active={})
        ist = []
        for g in groups:
            if g["remarks"]:
                ist.append((g["num"], [r["title"] for r in g["remarks"]]))
            for s in g["sub"]:
                if s["remarks"]:
                    ist.append((s["num"], [r["title"] for r in s["remarks"]]))
        print(f"  level={level}: {ist}")
        check(ist == erwartet, f"level={level}: {erwartet} erwartet, ist {ist}")


def test_kein_hinweis_geht_verloren():
    """Jeder nicht-leere <Remark> muss gelesen UND angezeigt werden.

    Gelesen wurden vorher nur Remarks innerhalb von <Itemlist>. In GAEB darf ein
    <Remark> aber auch direkt unter <BoQBody> stehen — als Einleitung eines
    Abschnitts, vor der ersten Untergruppe. Über die Beispiel-LVs waren das 33
    verschluckte Hinweise, u.a. "Das Kapitel STROM kann auch unabhängig von der
    restlichen Ausschreibung vergeben werden" (polis, Abschnitt 11).

    Leere <Remark>-Elemente (Tender (8).x84 hat 25 davon) zählen nicht: da ist
    nichts anzuzeigen.
    """
    print("\n=== Kein Hinweis geht verloren")

    def gezeigt(groups):
        """Hinweise, die in der Gruppenliste wirklich gerendert werden.

        Drei Töpfe: ``lead_remarks`` über der Kopfzeile (Abschnitts-Einleitungen),
        ``remarks`` am Gruppenende und die Hinweise an den Positionen. Wer einen
        davon vergisst, zählt zu wenig — genau so fiel auf, dass die
        Abschnitts-Einleitungen einen eigenen Topf bekommen haben.
        """
        n = 0
        for g in groups:
            n += len(g.get("lead_remarks") or []) + len(g.get("remarks") or [])
            for blk in g.get("blocks", []):
                for pos in _all_positions([blk]):
                    n += len(pos.get("remarks") or [])
            for sub in g.get("sub", []):
                n += len(sub.get("lead_remarks") or []) + len(sub.get("remarks") or [])
                for blk in sub.get("blocks", []):
                    for pos in _all_positions([blk]):
                        n += len(pos.get("remarks") or [])
        return n

    dateien = sorted(set(_glob.glob("infos/*.x8*") + _glob.glob("infos/*.X8*")
                         + _glob.glob("infos/*.D83")))
    geprueft = 0
    for f in dateien:
        try:
            wurzel = _ET.parse(f).getroot()
        except Exception:
            continue
        raum = wurzel.tag.split("}")[0][1:] if "}" in wurzel.tag else ""
        ns = "{" + raum + "}" if raum else ""
        roh = list(wurzel.iter(ns + "Remark"))
        if not roh:
            continue
        leer = sum(1 for x in roh if not "".join(x.itertext()).strip())
        try:
            pr = parse_gaeb(f)
        except Exception:
            continue
        geprueft += 1
        kurz = os.path.basename(f)[:30]
        check(len(pr.remarks) == len(roh) - leer,
              f"{kurz}: {len(roh) - leer} Hinweise mit Text, gelesen {len(pr.remarks)}")
        for level in (0, 1):
            check(gezeigt(_import_gaeb_groups(pr, level, {})) == len(pr.remarks),
                  f"{kurz} level={level}: {len(pr.remarks)} Hinweise gelesen, "
                  f"angezeigt {gezeigt(_import_gaeb_groups(pr, level, {}))}")
    print(f"  {geprueft} Dateien: alle Hinweise gelesen und in beiden Modi angezeigt")

    # Der konkret gemeldete Fall
    pp = parse_gaeb(POLIS)
    strom = [r for r in pp.remarks if r.category_path == ["STROM"]]
    check(len(strom) == 1,
          f"Abschnitts-Hinweis unter STROM erwartet, sind {len(strom)}")
    if strom:
        check("unabhängig" in strom[0].long_text,
              f"Text des STROM-Hinweises unerwartet: {strom[0].long_text[:60]!r}")
        check(bool(strom[0].next_item_id),
              "der Hinweis muss an die folgende Position gebunden sein")
        check(strom[0].is_section_lead,
              "Abschnitts-Einleitung muss als is_section_lead erkannt sein")
        # Abschnitts-Einleitungen sitzen ÜBER der Kopfzeile der Hauptgruppe, nicht
        # zwischen den Positionen — dort wirkten sie wie ein Kommentar zur ersten.
        for level, num_soll in ((0, "11.01"), (1, "11")):
            treffer = [(g["num"], r.get("scope"))
                       for g in _import_gaeb_groups(pp, level, {})
                       for r in (g.get("lead_remarks") or [])
                       if "unabhängig" in r["long_text"]]
            check(treffer == [(num_soll, "[11] STROM")],
                  f"level={level}: STROM-Hinweis über HG {num_soll} mit Herkunft "
                  f"'[11] STROM' erwartet, ist {treffer}")
        # und NICHT mehr an einer Position
        an_pos = [p_["oz"] for g in _import_gaeb_groups(pp, 0, {})
                  for blk in g["blocks"] for p_ in _all_positions([blk])
                  if any("unabhängig" in r["long_text"]
                         for r in (p_.get("remarks") or []))]
        check(not an_pos,
              f"Abschnitts-Einleitung darf nicht an einer Position hängen: {an_pos}")
        print("  polis Abschnitt 11: Hinweis sitzt über der HG, Herkunft '[11] STROM'")

    # Jeder angezeigte Hinweis muss sagen, wozu er gehört
    for level in (0, 1):
        ohne = []
        for g in _import_gaeb_groups(pp, level, {}):
            for topf in ("lead_remarks", "remarks"):
                ohne += [r["title"] for r in (g.get(topf) or []) if not r.get("scope")]
            for blk in g.get("blocks", []):
                for pos in _all_positions([blk]):
                    ohne += [r["title"] for r in (pos.get("remarks") or [])
                             if not r.get("scope")]
            for sub in g.get("sub", []):
                for topf in ("lead_remarks", "remarks"):
                    ohne += [r["title"] for r in (sub.get(topf) or [])
                             if not r.get("scope")]
                for blk in sub.get("blocks", []):
                    for pos in _all_positions([blk]):
                        ohne += [r["title"] for r in (pos.get("remarks") or [])
                                 if not r.get("scope")]
        check(not ohne, f"level={level}: Hinweise ohne Herkunftsangabe: {ohne[:3]}")
    print("  jeder Hinweis trägt eine Herkunftsangabe")


def test_nachlaufender_hinweis_findet_seine_gruppe():
    """Ein nachlaufender Hinweis auf Abschnittsebene braucht ein Auffangnetz.

    Sein Pfad ist ('STROM',), die Gruppen heißen aber ('STROM', 'Verstromung …').
    Ohne Präfix-Treffer wäre er still verschwunden.
    """
    print("\n=== Nachlaufender Hinweis auf Abschnittsebene")
    p = GaebProject(name="t", label="", phase="", date="", currency="EUR",
                    items=[_mk("a", "11.01.10", ["STROM", "Verstromung"]),
                           _mk("b", "11.02.10", ["STROM", "Erdung"])],
                    remarks=[GaebRemark(title="Kapitel STROM", long_text="x",
                                        category_path=["STROM"], next_item_id=""),
                             GaebRemark(title="Nur Erdung", long_text="y",
                                        category_path=["STROM", "Erdung"],
                                        next_item_id="")])
    for level, erwartet in ((0, [("11.01", ["Kapitel STROM"]), ("11.02", ["Nur Erdung"])]),
                            (1, [("11", ["Kapitel STROM"]), ("11.02", ["Nur Erdung"])])):
        ist = []
        for g in _import_gaeb_groups(p, level, {}):
            if g["remarks"]:
                ist.append((g["num"], [r["title"] for r in g["remarks"]]))
            for sub in g["sub"]:
                if sub["remarks"]:
                    ist.append((sub["num"], [r["title"] for r in sub["remarks"]]))
        print(f"  level={level}: {ist}")
        check(ist == erwartet, f"level={level}: {erwartet} erwartet, ist {ist}")


def test_altentwurf_wird_umgeschluesselt():
    """Entwürfe von vor der Umstellung trugen reine Labels als Schlüssel."""
    print("\n=== Alt-Entwurf: Label-Schlüssel werden migriert")

    class _SS:
        pass

    p = parse_gaeb(POLIS)
    ss = _SS()
    ss.d83_groups = _import_gaeb_groups(p, level=0, alt_active={})
    ss.d83_group_jobs = {"Lichttechnik": 2, "GROSSBANNER AUSSENBEREICH": 3}
    _migrate_group_jobs(ss)
    keys = ss.d83_group_jobs
    check(all("|" in k for k in keys), f"Schlüssel nicht migriert: {list(keys)[:3]}")
    licht = sorted(k for k, v in keys.items() if v == 2)
    einzeln = sum(1 for v in keys.values() if v == 3)
    print(f"  Lichttechnik=2 trifft {len(licht)} Gruppen, GROSSBANNER=3 trifft {einzeln}")
    check(len(licht) == 4, f"altes Label galt für alle 4 Gruppen, sind {len(licht)}")
    check(einzeln == 1, "eindeutiges Label muss genau eine Gruppe treffen")

    # Ein bereits migrierter Stand darf nicht ein zweites Mal angefasst werden
    vorher = dict(ss.d83_group_jobs)
    _migrate_group_jobs(ss)
    check(ss.d83_group_jobs == vorher, "zweiter Migrationslauf hat den Stand verändert")


test_gleichnamige_titel_bleiben_getrennt()
test_schluessel_traegt_die_nummer()
test_jobs_lassen_sich_einzeln_zuweisen()
test_ej_bezeichnungen_eindeutig()
test_export_findet_ueber_oz_zurueck()
test_elternebenen_werden_mitgeliefert()
test_excel_folgt_derselben_hg_regel()
test_hinweise_landen_in_ihrer_gruppe()
test_kein_hinweis_geht_verloren()
test_nachlaufender_hinweis_findet_seine_gruppe()
test_altentwurf_wird_umgeschluesselt()

print("\n" + "=" * 60)
print(f"{_oks} Prüfungen ok, {len(_fails)} fehlgeschlagen")
for m in _fails:
    print("  -", m)
sys.exit(1 if _fails else 0)
