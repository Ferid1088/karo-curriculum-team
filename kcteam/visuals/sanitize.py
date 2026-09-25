"""Bereinigung frei erzeugter SVGs: nur einfache Zeichenelemente, keine Skripte, keine externen Inhalte."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
ALLOWED_TAGS = {"svg", "g", "rect", "circle", "ellipse", "line", "polyline", "polygon", "path", "text", "tspan",
                "title", "desc", "defs", "marker", "linearGradient", "stop"}
ALLOWED_ATTRS = {"viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
                 "points", "d", "fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap",
                 "stroke-linejoin", "opacity", "fill-opacity", "stroke-opacity", "font-size", "font-weight",
                 "font-family", "text-anchor", "dominant-baseline", "transform", "id", "marker-end", "marker-start",
                 "markerWidth", "markerHeight", "refX", "refY", "orient", "offset", "stop-color", "xmlns",
                 "role", "aria-label", "dx", "dy"}
_URL_REF = re.compile(r"url\(\s*#[A-Za-z0-9_-]+\s*\)")


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def sanitize_svg(svg: str) -> str:
    if re.search(r"<!(DOCTYPE|ENTITY)", svg, re.I):
        raise ValueError("freeform_svg: DOCTYPE/ENTITY nicht erlaubt")
    try:
        root = ET.fromstring(svg.strip())
    except ET.ParseError as exc:
        raise ValueError(f"freeform_svg: kein gültiges SVG ({exc})") from exc
    if _local(root.tag) != "svg" or "viewBox" not in root.attrib:
        raise ValueError("freeform_svg: Wurzel muss <svg> mit viewBox sein")
    for el in root.iter():
        tag = _local(el.tag)
        if tag not in ALLOWED_TAGS:
            raise ValueError(f"freeform_svg: Element <{tag}> nicht erlaubt")
        for attr, val in el.attrib.items():
            name = _local(attr)
            if name not in ALLOWED_ATTRS:
                raise ValueError(f"freeform_svg: Attribut '{name}' nicht erlaubt")
            if "url(" in val and not _URL_REF.fullmatch(val.strip()):
                raise ValueError("freeform_svg: nur interne url(#id)-Verweise erlaubt")
            if "javascript" in val.lower():
                raise ValueError("freeform_svg: Skript-Inhalt nicht erlaubt")
    ET.register_namespace("", SVG_NS)
    return ET.tostring(root, encoding="unicode")


def svg_text_content(svg: str) -> str:
    """Alle sichtbaren Texte eines SVG (für die Inspektor-Prüfung)."""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return ""
    return " | ".join(t.strip() for el in root.iter() for t in [el.text or ""] if t.strip())
