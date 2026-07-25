"""GAEB DA XML parser for phases X83 and X84."""
import io
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import re


@dataclass
class GaebItem:
    item_id: str
    rno_part: int
    oz: str                 # Ordnungszahl (z.B. "01.01.03") aus GAEB-Hierarchie
    description: str
    long_text: str          # Langtext aus GAEB (Klartext, für Matching)
    qty: float
    unit: str
    category_path: list[str]
    unit_price: Optional[float] = None           # filled from X84
    is_alt: bool = False                         # Type="A" in GAEB XML
    long_text_images: list[str] = field(default_factory=list)  # data-URIs aus Langtext

    @property
    def full_position(self) -> str:
        return " > ".join(self.category_path)

    @property
    def match_query(self) -> str:
        """Kombination aus Kurz- und Langtext für bestmögliches Matching."""
        if self.long_text and self.long_text != self.description:
            return f"{self.description} {self.long_text[:300]}"
        return self.description


@dataclass
class GaebRemark:
    """Hinweis-/Informationstext (GAEB <Remark>) — z.B. 'General notes & appearance
    of the lighting'. Gehört zu einer Gruppe (category_path), ist keine Position:
    wird angezeigt, aber nicht nach Easyjob gebucht.

    next_item_id: item_id der Position, die im Dokument direkt auf den Hinweis folgt.
    Damit wird der Hinweis an der richtigen Stelle (vor dieser Position) angezeigt.
    Leer = Hinweis steht am Ende der Gruppe (keine folgende Position)."""
    title: str                      # Kurztext/Überschrift (OutlineText)
    long_text: str                  # Langtext (DetailTxt)
    category_path: list[str]
    images: list[str] = field(default_factory=list)
    next_item_id: str = ""


@dataclass
class GaebProject:
    name: str
    label: str
    phase: str
    date: str
    currency: str
    items: list[GaebItem] = field(default_factory=list)
    remarks: list[GaebRemark] = field(default_factory=list)
    preliminaries: list[GaebRemark] = field(default_factory=list)  # Award-Vorbemerkungen (projektweit)


def _detect_ns(root: ET.Element) -> dict[str, str]:
    """Extract namespace from root element tag."""
    m = re.match(r"\{(.+?)\}", root.tag)
    ns_uri = m.group(1) if m else ""
    return {"g": ns_uri} if ns_uri else {}


def _text_from_span(element: Optional[ET.Element], ns: dict) -> str:
    """Collect all text from nested <span> elements."""
    if element is None:
        return ""
    texts = []
    for span in element.iter(f"{{{ns.get('g', '')}}}span" if ns else "span"):
        if span.text and span.text.strip():
            texts.append(span.text.strip())
    return " ".join(texts)


def _text_from_detail(element: Optional[ET.Element], ns: dict) -> tuple[str, list[str]]:
    """Extract long text (preserving line breaks) and embedded images.

    Returns (plain_text, image_data_uris).
    Finds <p> elements at any depth so intermediate <Text> wrappers are handled.
    """
    if element is None:
        return "", []
    ns_g     = ns.get('g', '')
    p_tag    = f"{{{ns_g}}}p"     if ns_g else "p"
    span_tag = f"{{{ns_g}}}span"  if ns_g else "span"
    img_tag  = f"{{{ns_g}}}image" if ns_g else "image"

    paragraphs = list(element.iter(p_tag))
    if not paragraphs:
        return _text_from_span(element, ns), []

    text_lines: list[str] = []
    images:     list[str] = []

    for p in paragraphs:
        spans  = [c for c in p if c.tag == span_tag]
        imgs   = [c for c in p if c.tag == img_tag]

        if spans:
            texts = [s.text.strip() for s in spans if s.text and s.text.strip()]
            # Aufzählungs-Spans (Strich, Bullet, Nummerierung) → jeder Eintrag eigene Zeile
            _LIST_STARTS = ('-', '•', '*', '·', '–', '—')
            if len(texts) > 1 and any(t.startswith(_LIST_STARTS) for t in texts):
                line = "\n".join(texts)
            else:
                line = " ".join(texts)
            if line:
                text_lines.append(line)
        elif not imgs:
            raw = "".join(p.itertext()).strip()
            if raw:
                text_lines.append(raw)

        for img_el in imgs:
            enc    = img_el.get("Encoding", "").lower()
            mime   = img_el.get("Type", "image/jpeg")
            b64    = (img_el.text or "").strip()
            if b64 and enc == "base64":
                images.append(f"data:{mime};base64,{b64}")

    return "\n".join(text_lines), images


def _extract_outline_text(item_el: ET.Element, ns: dict) -> str:
    """Pull description from CompleteText > OutlineText > OutlTxt."""
    tag = lambda t: f"{{{ns['g']}}}{t}" if ns else t
    for path in [
        f".//{tag('TextOutlTxt')}",
        f".//{tag('OutlTxt')}",
    ]:
        el = item_el.find(path)
        if el is not None:
            txt = _text_from_span(el, ns)
            if txt:
                return txt
            raw = "".join(el.itertext()).strip()
            if raw:
                return raw
    return ""


def _extract_addtext(at_el: ET.Element, ns: dict) -> tuple[str, str, list[str]]:
    """Award-Vorbemerkung (AddText): Titel (OutlineAddText) + Langtext/Bilder
    (DetailAddText). Gibt (title, long_text, images) zurück."""
    tag = lambda t: f"{{{ns['g']}}}{t}" if ns else t
    title = ""
    ot = at_el.find(f".//{tag('OutlineAddText')}")
    if ot is not None:
        title = _text_from_span(ot, ns) or "".join(ot.itertext()).strip()
    long_text, images = "", []
    dt = at_el.find(f".//{tag('DetailAddText')}")
    if dt is not None:
        long_text, images = _text_from_detail(dt, ns)
        if not long_text and not images:
            long_text = "".join(dt.itertext()).strip()
    return title, long_text, images


def _extract_long_text(item_el: ET.Element, ns: dict) -> tuple[str, list[str]]:
    """Pull Langtext + embedded images from DetailTxt element."""
    tag = lambda t: f"{{{ns['g']}}}{t}" if ns else t
    for path in [
        f".//{tag('DetailTxt')}",
        f".//{tag('DescText')}",
    ]:
        el = item_el.find(path)
        if el is not None:
            txt, imgs = _text_from_detail(el, ns)
            if txt or imgs:
                return txt, imgs
            raw = "".join(el.itertext()).strip()
            if raw:
                return raw, []
    return "", []


def _parse_body(body_el: ET.Element, ns: dict, path: list[str],
                oz_prefix: str, items: list[GaebItem],
                remarks: list[GaebRemark]):
    """Recurse through BoQBody collecting items and remarks (info texts)."""
    tag = lambda t: f"{{{ns['g']}}}{t}" if ns else t

    for child in body_el:
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if local == "BoQCtgy":
            # Ordnungszahl der Kategorie aus RNoPart aufbauen
            rno_raw = child.get("RNoPart", "")
            child_oz = (f"{oz_prefix}.{rno_raw.zfill(2)}"
                        if oz_prefix else rno_raw.zfill(2))
            # category label
            lbl_el = child.find(f".//{tag('LblTx')}")
            lbl = _text_from_span(lbl_el, ns) if lbl_el is not None else ""
            if not lbl:
                lbl = "".join(lbl_el.itertext()).strip() if lbl_el is not None else ""
            new_path = path + [lbl] if lbl else path[:]
            inner_body = child.find(tag("BoQBody"))
            if inner_body is not None:
                _parse_body(inner_body, ns, new_path, child_oz, items, remarks)

        elif local == "Itemlist":
            # In Dokumentreihenfolge: Hinweise (Remark) puffern und an die folgende
            # Position binden, damit sie an der richtigen Stelle angezeigt werden.
            pending: list[dict] = []
            for el in child:
                el_local = el.tag.split("}")[-1] if "}" in el.tag else el.tag

                if el_local == "Remark":
                    r_title        = _extract_outline_text(el, ns)
                    r_long, r_imgs = _extract_long_text(el, ns)
                    if r_title or r_long or r_imgs:
                        pending.append({"title": r_title, "long_text": r_long, "images": r_imgs})

                elif el_local == "Item":
                    item_el = el
                    item_id = item_el.get("ID", "")
                    rno_raw = item_el.get("RNoPart", "")
                    rno = int(rno_raw) if rno_raw.isdigit() else 0
                    item_oz = (f"{oz_prefix}.{rno_raw.zfill(2)}"
                               if oz_prefix else rno_raw.zfill(2))
                    qty_el = item_el.find(tag("Qty"))
                    qty = float(qty_el.text) if qty_el is not None and qty_el.text else 0.0
                    qu_el = item_el.find(tag("QU"))
                    unit = qu_el.text.strip() if qu_el is not None and qu_el.text else ""
                    desc            = _extract_outline_text(item_el, ns)
                    long_text, imgs = _extract_long_text(item_el, ns)
                    item_type       = item_el.get("Type", "N").strip().upper()
                    # Fallback: wenn kein Type-Attribut gesetzt, Beschreibungstext prüfen
                    is_alt = item_type == "A" or (
                        item_type == "N" and bool(
                            re.match(r'^(alternative|alternativposition|alternativ)\b',
                                     (desc or "").strip(), re.IGNORECASE)
                        )
                    )
                    items.append(GaebItem(
                        item_id=item_id,
                        rno_part=rno,
                        oz=item_oz,
                        description=desc,
                        long_text=long_text,
                        qty=qty,
                        unit=unit,
                        category_path=path[:],
                        is_alt=is_alt,
                        long_text_images=imgs,
                    ))
                    # gepufferte Hinweise gehören vor diese Position
                    for pr in pending:
                        remarks.append(GaebRemark(
                            title=pr["title"], long_text=pr["long_text"],
                            category_path=path[:], images=pr["images"],
                            next_item_id=item_id,
                        ))
                    pending = []

            # Hinweise nach der letzten Position (keine folgende Position)
            for pr in pending:
                remarks.append(GaebRemark(
                    title=pr["title"], long_text=pr["long_text"],
                    category_path=path[:], images=pr["images"], next_item_id="",
                ))


def parse_gaeb(source: str | Path | bytes) -> GaebProject:
    """Parse a GAEB X83 (or X84 standalone) file into a GaebProject."""
    tree = ET.parse(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
    root = tree.getroot()
    ns = _detect_ns(root)
    tag = lambda t: f"{{{ns['g']}}}{t}" if ns else t

    # project info
    name = root.findtext(f".//{tag('NamePrj')}") or ""
    label = root.findtext(f".//{tag('LblPrj')}") or ""
    date = root.findtext(f".//{tag('Date')}") or ""
    phase = root.findtext(f".//{tag('DP')}") or ""
    currency = root.findtext(f".//{tag('Cur')}") or "EUR"

    items: list[GaebItem] = []
    remarks: list[GaebRemark] = []
    for boq in root.findall(f".//{tag('BoQ')}"):
        body = boq.find(tag("BoQBody"))
        if body is not None:
            _parse_body(body, ns, [], "", items, remarks)

    # Projektweite Vorbemerkungen (Award > AddText): Nachhaltigkeit, Timing, …
    preliminaries: list[GaebRemark] = []
    award = root.find(f".//{tag('Award')}")
    if award is not None:
        for at in award.findall(tag("AddText")):
            p_title, p_long, p_imgs = _extract_addtext(at, ns)
            if p_title or p_long or p_imgs:
                preliminaries.append(GaebRemark(
                    title=p_title, long_text=p_long, category_path=[], images=p_imgs,
                ))

    return GaebProject(name=name, label=label, phase=phase,
                       date=date, currency=currency, items=items,
                       remarks=remarks, preliminaries=preliminaries)
