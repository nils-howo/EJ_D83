"""GAEB DA XML parser for phases X83 and X84."""
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
    long_text: str          # Langtext aus GAEB (oft detaillierter als description)
    qty: float
    unit: str
    category_path: list[str]
    unit_price: Optional[float] = None  # filled from X84
    is_alt: bool = False                # Type="A" in GAEB XML

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
class GaebProject:
    name: str
    label: str
    phase: str
    date: str
    currency: str
    items: list[GaebItem] = field(default_factory=list)


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


def _extract_long_text(item_el: ET.Element, ns: dict) -> str:
    """Pull Langtext (detailed description) from DetailTxt element."""
    tag = lambda t: f"{{{ns['g']}}}{t}" if ns else t
    for path in [
        f".//{tag('DetailTxt')}",
        f".//{tag('DescText')}",
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


def _parse_body(body_el: ET.Element, ns: dict, path: list[str],
                oz_prefix: str, items: list[GaebItem]):
    """Recurse through BoQBody collecting items."""
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
                _parse_body(inner_body, ns, new_path, child_oz, items)

        elif local == "Itemlist":
            for item_el in child.findall(tag("Item")):
                item_id = item_el.get("ID", "")
                rno_raw = item_el.get("RNoPart", "")
                rno = int(rno_raw) if rno_raw.isdigit() else 0
                item_oz = (f"{oz_prefix}.{rno_raw.zfill(2)}"
                           if oz_prefix else rno_raw.zfill(2))
                qty_el = item_el.find(tag("Qty"))
                qty = float(qty_el.text) if qty_el is not None and qty_el.text else 0.0
                qu_el = item_el.find(tag("QU"))
                unit = qu_el.text.strip() if qu_el is not None and qu_el.text else ""
                desc      = _extract_outline_text(item_el, ns)
                long_text = _extract_long_text(item_el, ns)
                item_type = item_el.get("Type", "N").strip().upper()
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
                ))


def parse_gaeb(path: str | Path) -> GaebProject:
    """Parse a GAEB X83 (or X84 standalone) file into a GaebProject."""
    tree = ET.parse(path)
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
    for boq in root.findall(f".//{tag('BoQ')}"):
        body = boq.find(tag("BoQBody"))
        if body is not None:
            _parse_body(body, ns, [], "", items)

    return GaebProject(name=name, label=label, phase=phase,
                       date=date, currency=currency, items=items)


def merge_x84_prices(project: GaebProject, x84_path: str | Path) -> None:
    """Overlay unit prices from an X84 file onto an existing parsed project."""
    tree = ET.parse(x84_path)
    root = tree.getroot()
    ns = _detect_ns(root)
    tag = lambda t: f"{{{ns['g']}}}{t}" if ns else t

    price_map: dict[str, float] = {}
    for item_el in root.findall(f".//{tag('Item')}"):
        item_id = item_el.get("ID", "")
        up_el = item_el.find(tag("UP"))
        if up_el is not None and up_el.text:
            try:
                price_map[item_id] = float(up_el.text)
            except ValueError:
                pass

    for item in project.items:
        if item.item_id in price_map:
            item.unit_price = price_map[item.item_id]
