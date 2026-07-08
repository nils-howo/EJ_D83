"""Matching-Routen: /, Upload, Match, EJ-Dialog, Bundle-Bearbeitung."""
import asyncio
import json
import tempfile
from html import escape
from itertools import groupby
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from easyjob_api import EjLiveClient
from gaeb_parser import GaebItem, GaebProject, merge_x84_prices, parse_gaeb
from matcher import (
    HIGH_SCORE,
    LOW_SCORE,
    TRAVERSE_STANDARD_LENGTH_M,
    Article,
    MatchResult,
    Resource,
    UnifiedMatcher,
    auto_learn_bundle,
    is_kalkulations_position,
    is_motor_position,
    load_articles_db,
    load_resources_db,
    make_article_from_ej,
    parse_traverse_info,
    traverse_piece_count,
)
from state import (
    GUI_MAPPINGS_PATH,
    HAENGEPUNKT_NR,
    TRAIN_MAPPINGS_PATH,
    S,
    MatchProgress,
    templates,
)

router = APIRouter()


# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

def _fmt_eur(v: float) -> str:
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _score_css(score: float) -> str:
    if score >= HIGH_SCORE: return "score-high"
    if score >= LOW_SCORE:  return "score-mid"
    return "score-low"


def _score_border(score: float) -> str:
    if score >= HIGH_SCORE: return "#1a7a1a"
    if score >= LOW_SCORE:  return "#b36b00"
    return "#cc2222"


def _path_key(item: GaebItem) -> str:
    return " > ".join(item.category_path) if item.category_path else "Ohne Kategorie"


def _bundle_cost(bundle: list) -> float:
    total = 0.0
    for e in bundle:
        m = e["matchable"]
        if isinstance(m, Article) and m.mietpreis:
            total += e["qty"] * m.mietpreis
        elif isinstance(m, Resource) and m.tagessatz:
            total += e["qty"] * m.tagessatz
    return total


def _calc_metrics() -> dict:
    if not S.project:
        return {}
    total = len(S.project.items)
    matched = confident = art_count = res_count = 0
    cost_mat = cost_pers = 0.0
    for it in S.project.items:
        bundle = S.bundles.get(it.item_id, [])
        mr     = S.matches.get(it.item_id)
        if bundle:
            matched += 1
            for e in bundle:
                m = e["matchable"]
                if isinstance(m, Article):
                    art_count += 1
                    if m.mietpreis: cost_mat += e["qty"] * m.mietpreis
                elif isinstance(m, Resource):
                    res_count += 1
                    if m.tagessatz: cost_pers += e["qty"] * m.tagessatz
        if mr and mr.score >= HIGH_SCORE and bundle:
            confident += 1
    return dict(
        total=total, matched=matched, confident=confident,
        art_count=art_count, res_count=res_count,
        cost_mat=cost_mat, cost_pers=cost_pers,
    )


def _item_view(item: GaebItem) -> dict:
    mr     = S.matches.get(item.item_id, MatchResult(None, 0, "none", False))
    bundle = S.bundles.setdefault(item.item_id, [])
    score  = mr.score
    cost   = _bundle_cost(bundle)
    oz     = getattr(item, "oz", "")
    lt     = getattr(item, "long_text", "") or ""
    lt_snippet = (lt[:180] + "…" if len(lt) > 180 else lt) \
        if lt.strip() and lt.strip() != item.description else ""

    lfm_hint = ""
    if bundle and bundle[0].get("lfm_converted"):
        lfm_hint = f" → {bundle[0]['qty']:g} Stk à {TRAVERSE_STANDARD_LENGTH_M:g} m"

    breakdown_rows = []
    bd = mr.breakdown or {}
    if bd.get("scores"):
        total_s = 0.0
        for val, lbl in bd["scores"]:
            if lbl == "Fuzzy-Score":
                breakdown_rows.append({"val": f"{val:.0f}", "lbl": lbl})
                total_s = float(val)
            else:
                sign = "+" if float(val) >= 0 else ""
                breakdown_rows.append({"val": f"{sign}{val:.0f}", "lbl": lbl})
                total_s += float(val)
        breakdown_rows.append({"val": f"{min(total_s,99):.0f}", "lbl": "Gesamt", "bold": True})

    bd_json = ""
    if bd:
        _export = {
            "position":        item.description[:80],
            "langtext":        lt[:400] if lt.strip() else None,
            "matched_article": getattr(mr.matched, "bezeichnung", "")[:60],
            "score":           score,
            **{k: v for k, v in bd.items() if k != "scores"},
            "scores":          [[round(float(v), 1), l] for v, l in bd.get("scores", [])],
        }
        bd_json = json.dumps(_export, ensure_ascii=False, indent=2)

    bundle_entries = []
    for bi, entry in enumerate(bundle):
        m = entry["matchable"]
        qty = entry["qty"]
        is_fremd = isinstance(m, Article) and m.mietinventar == 0
        preis_str = ""
        kosten = 0.0
        if isinstance(m, Article):
            preis_str = f"{m.mietpreis:.2f} €/Stk" if m.mietpreis else "—"
            kosten = qty * m.mietpreis if m.mietpreis else 0
        else:
            preis_str = f"{m.tagessatz:.2f} €/Tag" if m.tagessatz else "—"
            kosten = qty * m.tagessatz if m.tagessatz else 0
        bundle_entries.append({
            "idx":       bi,
            "id":        m.display_id,
            "name":      m.display_name,
            "type":      m.display_type,
            "qty":       qty,
            "preis_str": preis_str,
            "kosten":    _fmt_eur(kosten) if kosten else "",
            "is_fremd":  is_fremd,
        })

    return {
        "item":            item,
        "item_id":         item.item_id,
        "oz":              oz,
        "description":     item.description,
        "qty":             item.qty,
        "unit":            item.unit,
        "lt_snippet":      lt_snippet,
        "lfm_hint":        lfm_hint,
        "score":           score,
        "score_css":       _score_css(score),
        "score_border":    _score_border(score),
        "cost_str":        _fmt_eur(cost) if cost else "—",
        "method":          mr.method,
        "bd":              bd,
        "bd_json":         bd_json,
        "breakdown_rows":  breakdown_rows,
        "bundle":          bundle_entries,
        "has_bundle":      bool(bundle),
        "is_kalkpos":      mr.method == "kalkpos",
        "has_ej":          bool(S.ej_client),
    }


def _positions_data() -> list:
    if not S.project:
        return []
    item_order = {id(it): i for i, it in enumerate(S.project.items)}
    seen_cats: dict[str, int] = {}
    for it in S.project.items:
        k = _path_key(it)
        if k not in seen_cats:
            seen_cats[k] = len(seen_cats)

    sorted_items = sorted(
        S.project.items,
        key=lambda it: (seen_cats[_path_key(it)], item_order[id(it)])
    )

    result = []
    for cat_path, group_iter in groupby(sorted_items, key=_path_key):
        group = list(group_iter)
        first_oz = getattr(group[0], "oz", "")
        oz_parts = first_oz.split(".") if first_oz else []
        cat_oz = ".".join(oz_parts[:-1]) if len(oz_parts) > 1 else (oz_parts[0] if oz_parts else "")

        blocks = []
        gi = 0
        while gi < len(group):
            it = group[gi]
            if it.is_alt:
                blocks.append({"has_alt": False, "primary": _item_view(it)})
                gi += 1
            elif gi + 1 < len(group) and group[gi + 1].is_alt:
                alt = group[gi + 1]
                alt_key = f"{it.item_id}|{alt.item_id}"
                chosen = S.alt_active.get(alt_key, "primary")
                render_primary = chosen in ("primary", "both")
                render_alt     = chosen in ("alt", "both")
                blocks.append({
                    "has_alt":        True,
                    "alt_key":        alt_key,
                    "alt_choice":     chosen,
                    "primary":        _item_view(it),
                    "alt":            _item_view(alt),
                    "render_primary": render_primary,
                    "render_alt":     render_alt,
                })
                gi += 2
            else:
                blocks.append({"has_alt": False, "primary": _item_view(it)})
                gi += 1

        result.append({
            "cat_path": cat_path,
            "cat_oz":   cat_oz,
            "label":    f"[{cat_oz}]  {cat_path}" if cat_oz else cat_path,
            "blocks":   blocks,
        })
    return result


def _learn_bundle(item_id: str, description: str) -> None:
    """Speichert den aktuellen Bundle-Zustand als gelerntes Mapping."""
    bundle = S.bundles.get(item_id, [])
    numbers = [
        e["matchable"].nummer
        for e in bundle
        if hasattr(e["matchable"], "nummer") and e["matchable"].nummer.strip()
    ]
    auto_learn_bundle(description, numbers, GUI_MAPPINGS_PATH)
    if S.matcher:
        S.matcher.add_learned_bundle(description, numbers)


def _run_matching(projekt: GaebProject, matcher: UnifiedMatcher,
                  progress: MatchProgress) -> tuple[dict, dict]:
    matches: dict[str, MatchResult] = {}
    bundles: dict[str, list] = {}
    hp_art: Optional[Article] = None
    hp_idx = matcher._num_to_idx.get(HAENGEPUNKT_NR)
    if hp_idx is not None:
        hp_art = matcher._pool[hp_idx]

    progress.total = len(projekt.items)
    progress.done  = 0

    for i, item in enumerate(projekt.items):
        if is_kalkulations_position(item.description):
            matches[item.item_id] = MatchResult(None, 0, "kalkpos", False)
            progress.done = i + 1
            continue

        lt = getattr(item, "long_text", None)
        results = matcher.match(
            item.description, limit=1,
            category_path=item.category_path,
            qty=item.qty, unit=item.unit,
            long_text=lt,
        )
        if results:
            mr, art = results[0]
            matches[item.item_id] = mr
            if isinstance(art, Article):
                ti = parse_traverse_info(item.description)
                piece_len = (ti.length_m if ti and ti.length_m else None) or TRAVERSE_STANDARD_LENGTH_M
                pieces = traverse_piece_count(item.qty, item.unit, piece_len)
                if pieces is not None:
                    bundles[item.item_id] = [{"matchable": art, "qty": pieces, "lfm_converted": True}]
                else:
                    bundles[item.item_id] = [{"matchable": art, "qty": item.qty, "lfm_converted": False}]
                if is_motor_position(item.description) and hp_art:
                    bundles[item.item_id].append({"matchable": hp_art, "qty": item.qty, "lfm_converted": False})
            else:
                bundles[item.item_id] = [{"matchable": art, "qty": item.qty, "lfm_converted": False}]

            # Gelernte Zusatzartikel anhängen
            for extra_num in matcher.get_bundle_extras(item.description):
                eidx = matcher._num_to_idx.get(extra_num)
                if eidx is not None:
                    bundles[item.item_id].append({
                        "matchable": matcher._pool[eidx],
                        "qty": item.qty,
                        "lfm_converted": False,
                    })

        progress.done = i + 1

    return matches, bundles


# ─── Routen ───────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "S":         S,
        "metrics":   _calc_metrics(),
        "positions": _positions_data(),
    })


@router.post("/api/upload/x83", response_class=HTMLResponse)
async def upload_x83(file: UploadFile = File(...)):
    S.x83_bytes = await file.read()
    S.x83_name  = file.filename or "x83"
    return f'<span class="file-ok">✓ {escape(S.x83_name)}</span>'


@router.post("/api/upload/x84", response_class=HTMLResponse)
async def upload_x84(file: UploadFile = File(...)):
    S.x84_bytes = await file.read()
    S.x84_name  = file.filename or "x84"
    return f'<span class="file-ok">✓ {escape(S.x84_name)}</span>'



@router.post("/api/settings/mappings", response_class=HTMLResponse)
async def save_mapping_toggles(
    use_train: str = Form(""),
    use_gui:   str = Form(""),
):
    S.use_train_mappings = (use_train == "1")
    S.use_gui_mappings   = (use_gui   == "1")
    labels = []
    if S.use_train_mappings: labels.append("Training")
    if S.use_gui_mappings:   labels.append("GUI")
    text = "Aktiv: " + ", ".join(labels) if labels else "Alle deaktiviert"
    return f'<span class="save-ok">✓ {text}</span>'


@router.post("/api/match/start", response_class=HTMLResponse)
async def match_start(request: Request):
    if not S.x83_bytes:
        return '<p class="error-msg">Bitte zuerst eine X83-Datei hochladen.</p>'
    if S.progress.running:
        return '<p class="error-msg">Matching läuft bereits.</p>'

    S.progress = MatchProgress()
    S.progress.running = True

    async def _run():
        try:
            loop = asyncio.get_event_loop()
            with tempfile.NamedTemporaryFile(suffix=".x83", delete=False) as f:
                f.write(S.x83_bytes)
                tmp_path = Path(f.name)
            project = await loop.run_in_executor(None, parse_gaeb, tmp_path)
            tmp_path.unlink(missing_ok=True)

            if S.x84_bytes:
                with tempfile.NamedTemporaryFile(suffix=".x84", delete=False) as f:
                    f.write(S.x84_bytes)
                    tmp84 = Path(f.name)
                await loop.run_in_executor(None, merge_x84_prices, project, tmp84)
                tmp84.unlink(missing_ok=True)

            articles  = await loop.run_in_executor(None, load_articles_db)
            resources = await loop.run_in_executor(None, load_resources_db)
            matcher   = UnifiedMatcher(articles, resources)

            if not S.use_train_mappings or not S.use_gui_mappings:
                keep = [
                    (k, n, s)
                    for k, n, s in zip(matcher._res_keys, matcher._res_nums, matcher._res_sources)
                    if (s == "train" and S.use_train_mappings)
                    or (s == "gui"   and S.use_gui_mappings)
                ]
                if keep:
                    ks, ns, ss = zip(*keep)
                    matcher._res_keys    = list(ks)
                    matcher._res_nums    = list(ns)
                    matcher._res_sources = list(ss)
                else:
                    matcher._res_keys = matcher._res_nums = matcher._res_sources = []

            ej_client = None
            try:
                ej_client = await loop.run_in_executor(
                    None, lambda: EjLiveClient(S.ej_url, S.ej_user, S.ej_pass)
                )
            except Exception:
                pass

            matches, bundles = await loop.run_in_executor(
                None, _run_matching, project, matcher, S.progress
            )

            S.project    = project
            S.matcher    = matcher
            S.matches    = matches
            S.bundles    = bundles
            S.ej_client  = ej_client
            S.alt_active = {}
            S.ej_cache   = {}
        except Exception as ex:
            S.progress.error = str(ex)
        finally:
            S.progress.running = False

    asyncio.ensure_future(_run())
    return templates.TemplateResponse(request, "partials/progress.html", {})


@router.get("/api/match/progress", response_class=HTMLResponse)
async def match_progress(request: Request):
    p = S.progress
    if p.running or (p.total == 0 and not p.error):
        pct = int(100 * p.done / p.total) if p.total else 0
        return templates.TemplateResponse(request, "partials/progress.html", {
            "done": p.done, "total": p.total, "pct": pct,
        })
    if p.error:
        return f'<p class="error-msg">Fehler: {escape(p.error)}</p>'
    return templates.TemplateResponse(request, "partials/main_content.html", {
        "S": S, "metrics": _calc_metrics(), "positions": _positions_data(),
    })


@router.get("/api/positions", response_class=HTMLResponse)
async def get_positions(request: Request):
    return templates.TemplateResponse(request, "partials/positions.html", {
        "positions": _positions_data(), "S": S,
    })


@router.get("/api/metrics", response_class=HTMLResponse)
async def get_metrics(request: Request):
    return templates.TemplateResponse(request, "partials/metrics.html", {
        "metrics": _calc_metrics(),
    })


@router.get("/api/ej/dialog/{item_id}", response_class=HTMLResponse)
async def ej_dialog(item_id: str, request: Request):
    if not S.project:
        raise HTTPException(404, "Kein Projekt geladen")
    item = next((it for it in S.project.items if it.item_id == item_id), None)
    if not item:
        raise HTTPException(404, "Position nicht gefunden")

    suggestions: list[dict] = []
    suggestions_error: str = ""
    if S.matcher:
        try:
            _m = S.matcher
            _desc, _cat, _qty, _unit, _lt = (
                item.description, item.category_path,
                float(item.qty), item.unit, item.long_text,
            )
            loop = asyncio.get_event_loop()
            top = await loop.run_in_executor(
                None,
                lambda: _m.match(_desc, limit=5, category_path=_cat,
                                  qty=_qty, unit=_unit, long_text=_lt),
            )
            for mr, art in top:
                raw_score = mr.score if isinstance(mr.score, (int, float)) else 0
                suggestions.append({
                    "nummer":      getattr(art, "nummer", ""),
                    "bezeichnung": getattr(art, "bezeichnung", ""),
                    "kategorie":   getattr(art, "warengruppe", ""),
                    "inv":         str(getattr(art, "mietinventar", "")),
                    "score":       min(int(raw_score), 100),
                    "_raw_json":   json.dumps({
                        "Number":      getattr(art, "nummer", ""),
                        "Caption":     getattr(art, "bezeichnung", ""),
                        "IdStockType": 0,
                    }),
                })
        except Exception as _exc:
            suggestions_error = str(_exc)

    return templates.TemplateResponse(request, "partials/ej_dialog.html", {
        "item_id":           item_id,
        "item_desc":         item.description,
        "default_q":         item.description[:60],
        "default_qty":       float(item.qty),
        "results":           [],
        "suggestions":       suggestions,
        "suggestions_error": suggestions_error,
    })


@router.get("/api/ej/search/{item_id}", response_class=HTMLResponse)
async def ej_search(item_id: str, request: Request, q: str = ""):
    if not S.ej_client or not q.strip():
        return templates.TemplateResponse(request, "partials/ej_results.html", {
            "results": [], "item_id": item_id
        })
    ck = q.strip().lower()
    if ck not in S.ej_cache:
        loop = asyncio.get_event_loop()
        S.ej_cache[ck] = await loop.run_in_executor(
            None, lambda: S.ej_client.search(q, limit=40)
        )
    raw = S.ej_cache.get(ck, [])
    results = []
    for r in raw:
        num = str(r.get("Number", ""))
        inv = ""
        if S.matcher:
            lidx = S.matcher._num_to_idx.get(num)
            if lidx is not None:
                inv = str(S.matcher._pool[lidx].mietinventar)
        results.append({
            "nummer":      num,
            "bezeichnung": r.get("Caption", ""),
            "kategorie":   r.get("Category", ""),
            "inv":         inv,
            "_raw_json":   json.dumps(r),
        })
    return templates.TemplateResponse(request, "partials/ej_results.html", {
        "results": results, "item_id": item_id
    })


@router.get("/api/ej/add-dialog/{item_id}", response_class=HTMLResponse)
async def ej_add_dialog(item_id: str, request: Request):
    if not S.project:
        raise HTTPException(404)
    item = next((it for it in S.project.items if it.item_id == item_id), None)
    if not item:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "partials/ej_add_dialog.html", {
        "item_id":     item_id,
        "item_desc":   item.description,
        "default_qty": float(item.qty),
        "results":     [],
    })


@router.get("/api/ej/add-search/{item_id}", response_class=HTMLResponse)
async def ej_add_search(item_id: str, request: Request, q: str = ""):
    if not S.ej_client or not q.strip():
        return templates.TemplateResponse(request, "partials/ej_add_results.html", {
            "results": [], "item_id": item_id
        })
    ck = q.strip().lower()
    if ck not in S.ej_cache:
        loop = asyncio.get_event_loop()
        S.ej_cache[ck] = await loop.run_in_executor(
            None, lambda: S.ej_client.search(q, limit=40)
        )
    raw = S.ej_cache.get(ck, [])
    results = []
    for r in raw:
        num = str(r.get("Number", ""))
        inv = ""
        if S.matcher:
            lidx = S.matcher._num_to_idx.get(num)
            if lidx is not None:
                inv = str(S.matcher._pool[lidx].mietinventar)
        results.append({
            "nummer":      num,
            "bezeichnung": r.get("Caption", ""),
            "kategorie":   r.get("Category", ""),
            "inv":         inv,
            "_raw_json":   json.dumps(r),
        })
    return templates.TemplateResponse(request, "partials/ej_add_results.html", {
        "results": results, "item_id": item_id
    })


@router.get("/api/ej/lookup")
async def ej_lookup_by_num(num: str = ""):
    if not S.ej_client or not num.strip():
        return JSONResponse({"IdStockType": 0, "Caption": ""})
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, lambda: S.ej_client.search(num.strip(), limit=5))
        for r in (results or []):
            if str(r.get("Number", "")).strip() == num.strip():
                return JSONResponse({"IdStockType": r.get("IdStockType", 0), "Caption": r.get("Caption", "")})
        return JSONResponse({"IdStockType": 0, "Caption": ""})
    except Exception:
        return JSONResponse({"IdStockType": 0, "Caption": ""})


@router.get("/api/ej/related/{ej_id}", response_class=HTMLResponse)
async def ej_related(ej_id: int, request: Request, name: str = ""):
    if not S.ej_client:
        return ""
    loop = asyncio.get_event_loop()
    refs = await loop.run_in_executor(None, S.ej_client.get_references, ej_id)
    required = [r for r in refs if not r["IsOptional"]]
    optional = [r for r in refs if r["IsOptional"]]
    if not required and not optional:
        return ""
    return templates.TemplateResponse(request, "partials/ej_related.html", {
        "required": required, "optional": optional, "article_name": name,
    })


@router.post("/api/position/{item_id}/add-ej", response_class=HTMLResponse)
async def add_ej_article(
    item_id:    str,
    request:    Request,
    ej_num:     str   = Form(default=""),
    raw_json:   str   = Form(default=""),
    qty:        float = Form(default=1.0),
    extra_nums: str   = Form(default=""),
    extra_qtys: str   = Form(default=""),
):
    if not S.project:
        raise HTTPException(400)
    if not ej_num or not raw_json:
        return '<p class="error-msg">Bitte einen Artikel auswählen.</p>'
    item = next((it for it in S.project.items if it.item_id == item_id), None)
    if not item:
        raise HTTPException(404)

    raw_item = json.loads(raw_json)
    ej_id    = raw_item.get("IdStockType")
    is_local = bool(S.matcher and S.matcher._num_to_idx.get(ej_num))
    details  = None
    if not is_local and ej_id and S.ej_client:
        loop    = asyncio.get_event_loop()
        details = await loop.run_in_executor(None, S.ej_client.get_details, ej_id)

    art    = make_article_from_ej(raw_item, details, S.matcher)
    bundle = S.bundles.setdefault(item_id, [])
    bundle.append({"matchable": art, "qty": qty, "lfm_converted": False})

    nums     = [n.strip() for n in extra_nums.split(",") if n.strip()]
    qtys_raw = [q.strip() for q in extra_qtys.split(",") if q.strip()]
    for i, extra_num in enumerate(nums):
        extra_qty = float(qtys_raw[i]) if i < len(qtys_raw) else 1.0
        idx = S.matcher._num_to_idx.get(extra_num) if S.matcher else None
        if idx is not None:
            extra_art = S.matcher._pool[idx]
            has_price = (isinstance(extra_art, Article) and extra_art.mietpreis) or \
                        (isinstance(extra_art, Resource) and extra_art.tagessatz)
            if has_price:
                bundle.append({"matchable": extra_art, "qty": extra_qty, "lfm_converted": False})

    _learn_bundle(item_id, item.description)
    return templates.TemplateResponse(request, "partials/main_content.html", {
        "S": S, "metrics": _calc_metrics(), "positions": _positions_data(),
    })


@router.post("/api/position/{item_id}/set-match", response_class=HTMLResponse)
async def set_match(
    item_id:    str,
    request:    Request,
    ej_num:     str   = Form(default=""),
    raw_json:   str   = Form(default=""),
    qty:        float = Form(default=1.0),
    extra_nums: str   = Form(default=""),
    extra_qtys: str   = Form(default=""),
):
    if not S.project:
        raise HTTPException(400, "Kein Projekt geladen")
    if not ej_num or not raw_json:
        return '<p class="error-msg">Bitte zuerst einen Artikel auswählen.</p>'
    item = next((it for it in S.project.items if it.item_id == item_id), None)
    if not item:
        raise HTTPException(404)

    raw_item = json.loads(raw_json)
    ej_id    = raw_item.get("IdStockType")
    is_local = bool(S.matcher and S.matcher._num_to_idx.get(ej_num))
    details  = None
    if not is_local and ej_id and S.ej_client:
        loop    = asyncio.get_event_loop()
        details = await loop.run_in_executor(None, S.ej_client.get_details, ej_id)

    art = make_article_from_ej(raw_item, details, S.matcher)
    S.matches[item_id] = MatchResult(matched=art, score=99.0, method="manual", confident=True)
    bundle = [{"matchable": art, "qty": qty, "lfm_converted": False}]

    nums     = [n.strip() for n in extra_nums.split(",") if n.strip()]
    qtys_raw = [q.strip() for q in extra_qtys.split(",") if q.strip()]
    for i, extra_num in enumerate(nums):
        extra_qty = float(qtys_raw[i]) if i < len(qtys_raw) else 1.0
        if not S.matcher:
            continue
        idx = S.matcher._num_to_idx.get(extra_num)
        if idx is not None:
            extra_art = S.matcher._pool[idx]
            has_price = (isinstance(extra_art, Article) and extra_art.mietpreis) or \
                        (isinstance(extra_art, Resource) and extra_art.tagessatz)
            if has_price:
                bundle.append({"matchable": extra_art, "qty": extra_qty, "lfm_converted": False})

    S.bundles[item_id] = bundle
    _learn_bundle(item_id, item.description)
    return templates.TemplateResponse(request, "partials/main_content.html", {
        "S": S, "metrics": _calc_metrics(), "positions": _positions_data(),
    })


@router.post("/api/position/{item_id}/remove/{idx}", response_class=HTMLResponse)
async def remove_bundle_item(item_id: str, idx: int, request: Request):
    bundle = S.bundles.get(item_id, [])
    if 0 <= idx < len(bundle):
        bundle.pop(idx)
    if S.project:
        item = next((it for it in S.project.items if it.item_id == item_id), None)
        if item:
            _learn_bundle(item_id, item.description)
    return templates.TemplateResponse(request, "partials/main_content.html", {
        "S": S, "metrics": _calc_metrics(), "positions": _positions_data(),
    })


@router.post("/api/position/{item_id}/set-qty/{idx}", response_class=HTMLResponse)
async def set_bundle_qty(item_id: str, idx: int, request: Request, qty: float = Form(...)):
    bundle = S.bundles.get(item_id, [])
    if 0 <= idx < len(bundle):
        bundle[idx]["qty"] = qty
    return templates.TemplateResponse(request, "partials/main_content.html", {
        "S": S, "metrics": _calc_metrics(), "positions": _positions_data(),
    })



@router.get("/api/local-search")
async def local_search(q: str = "", limit: int = 8):
    if not S.matcher or not q.strip():
        return []
    cands = S.matcher.search(q, limit=limit, only_articles=True)
    return [{"nummer": c.display_id, "name": c.display_name} for c in cands]


@router.post("/api/alt/{alt_key}", response_class=HTMLResponse)
async def set_alt(alt_key: str, request: Request, choice: str = Form(...)):
    S.alt_active[alt_key] = choice
    return templates.TemplateResponse(request, "partials/main_content.html", {
        "S": S, "metrics": _calc_metrics(), "positions": _positions_data(),
    })
