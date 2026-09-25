"""Referenz-Renderer: Visual-Spec -> SVG.

Das SVG wird beim Speichern erzeugt und in der Datenbank abgelegt, damit Karo es ohne eigene
Zeichenlogik anzeigen kann. Farben: farbenblind-sichere Palette (Okabe-Ito), weißer Hintergrund,
feste Schriftgrößen ab 13 px, alt-Text als <title> für Screenreader.
"""
from __future__ import annotations

import math
from fractions import Fraction
from xml.sax.saxutils import escape, quoteattr

from pydantic import TypeAdapter

from . import spec as S

W = 520
COLORS = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00", "#56B4E9"]
LIGHT = ["#CCE3F0", "#FAE8C2", "#C2E8DC", "#F2DDEA", "#F6D3BF", "#DDEFFB"]
INK, MUTED, GRID, BG, ACCENT = "#1F2933", "#5B6472", "#D5DAE1", "#FFFFFF", "#D55E00"
FONT = "Helvetica, Arial, sans-serif"

_adapter = TypeAdapter(S.VisualSpec)


def parse_spec(data: dict):
    return _adapter.validate_python(data)


# --------------------------------------------------------------------------- Bausteine
class Canvas:
    def __init__(self, title: str | None, width: float = W):
        self.parts: list[str] = []
        self.w = float(width)
        self.y0 = 0
        if title:
            lines = wrap_px(title, self.w - 32, 17, bold=True)
            for i, ln in enumerate(lines):
                self.text(self.w / 2, 26 + i * 21, ln, size=17, weight="bold", anchor="middle")
            self.y0 = 40 + 21 * (len(lines) - 1)

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, t, size=14, anchor="start", weight="normal", color=INK, baseline="auto", italic=False):
        style = ' font-style="italic"' if italic else ""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" fill="{color}" '
                 f'text-anchor="{anchor}" dominant-baseline="{baseline}" font-family="{FONT}"{style}>'
                 f'{escape(str(t))}</text>')

    def rect(self, x, y, w, h, fill="none", stroke=INK, sw=1.5, rx=0, dash=None, opacity=None):
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        extra += f' fill-opacity="{opacity}"' if opacity is not None else ""
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}"{extra}/>')

    def line(self, x1, y1, x2, y2, stroke=INK, sw=1.5, dash=None, arrow=False):
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        extra += ' marker-end="url(#arrow)"' if arrow else ""
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" '
                 f'stroke-width="{sw}" stroke-linecap="round"{extra}/>')

    def circle(self, cx, cy, r, fill="none", stroke=INK, sw=1.5):
        self.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')

    def path(self, d, fill="none", stroke=INK, sw=1.5, arrow=False, dash=None):
        extra = ' marker-end="url(#arrow)"' if arrow else ""
        extra += f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" stroke-linejoin="round"{extra}/>')

    def finish(self, height: float, alt: str) -> str:
        h = int(math.ceil(height))
        w = int(math.ceil(self.w))
        defs = ('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{INK}"/></marker></defs>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
                f'style="max-width:100%;height:auto" role="img" aria-label={quoteattr(alt)}><title>{escape(alt)}</title>{defs}'
                f'<rect x="0" y="0" width="{w}" height="{h}" rx="12" fill="{BG}"/>' + "".join(self.parts) + "</svg>")


# Zeichenbreiten von Helvetica/Arial in em (gerundet). Genauer als „Zeichen × Faktor“: „mmm“ ist doppelt so
# breit wie „iii“, und genau daran lief Text früher aus Kästen und Tabellenzellen.
_EM = {**{ch: 0.28 for ch in "iljI.,:;'|!·"}, **{ch: 0.34 for ch in "frt()[]{}/\\-"},
       **{ch: 0.5 for ch in "sczJ"}, **{ch: 0.84 for ch in "mwMW"}, " ": 0.28, "–": 0.56, "—": 1.0, "%": 0.89,
       "…": 1.0, "→": 1.0, "×": 0.58, "−": 0.58}


def _char_em(ch: str) -> float:
    if ch in _EM:
        return _EM[ch]
    if ch.isdigit():
        return 0.56
    if ch.isupper():
        return 0.7
    return 0.56


def text_width(t: str, size: float, bold: bool = False) -> float:
    """Geschätzte Breite in px (mit kleinem Sicherheitsaufschlag für andere Schriften)."""
    return sum(_char_em(ch) for ch in str(t)) * size * (1.1 if bold else 1.04)


def wrap_px(t: str, max_px: float, size: float, bold: bool = False) -> list[str]:
    """Umbruch nach Pixelbreite. Zu lange Wörter werden mit Bindestrich getrennt – abgeschnitten wird nie."""
    max_px = max(max_px, size * 2)
    words, lines, cur = str(t).split(), [], ""
    for w in words:
        while text_width(w, size, bold) > max_px:        # Wort passt allein nicht in eine Zeile
            k = len(w)
            while k > 1 and text_width(w[:k] + "-", size, bold) > max_px:
                k -= 1
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:k] + "-")
            w = w[k:]
        cand = f"{cur} {w}".strip()
        if text_width(cand, size, bold) <= max_px:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def wrap(t: str, max_chars: int) -> list[str]:
    words, lines, cur = str(t).split(), [], ""
    for w in words:
        if len(cur) + len(w) + (1 if cur else 0) <= max_chars:
            cur = f"{cur} {w}".strip()
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def fmt_num(v: float, as_fraction: bool = False) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    if as_fraction:
        f = Fraction(v).limit_denominator(100)
        return f"{f.numerator}/{f.denominator}"
    return f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def nice_step(span: float, target: int = 10) -> float:
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


# --------------------------------------------------------------------------- Mathematik
def _fraction_bar(v: S.FractionBar) -> str:
    c = Canvas(v.title)
    x0, bw, bh, gap = 70, 360, 44, 34
    y = c.y0 + 20
    for i, b in enumerate(v.bars):
        if b.label:
            c.text(x0, y - 6, b.label, size=13, color=MUTED)
        pw = bw / b.denominator
        for k in range(b.denominator):
            c.rect(x0 + k * pw, y, pw, bh, fill=COLORS[i % 6] if k < b.numerator else BG, sw=1.5)
        c.rect(x0, y, bw, bh, sw=2.5)
        if v.show_fraction_labels:
            c.text(x0 + bw + 22, y + bh / 2, f"{b.numerator}/{b.denominator}", size=20, weight="bold",
                   baseline="central")
        y += bh + gap
    return c.finish(y - gap + 24, v.alt)


def _wedge(cx, cy, r, a0, a1) -> str:
    x0, y0 = cx + r * math.sin(a0), cy - r * math.cos(a0)
    x1, y1 = cx + r * math.sin(a1), cy - r * math.cos(a1)
    large = 1 if (a1 - a0) > math.pi else 0
    return f"M{cx:.1f},{cy:.1f} L{x0:.1f},{y0:.1f} A{r:.1f},{r:.1f} 0 {large} 1 {x1:.1f},{y1:.1f} Z"


def _fraction_circle(v: S.FractionCircle) -> str:
    c = Canvas(v.title)
    n = len(v.circles)
    r = 64 if n <= 3 else 54
    slot = W / n
    cy = c.y0 + 20 + r
    for i, fc in enumerate(v.circles):
        cx = slot * i + slot / 2
        if fc.denominator == 1:
            c.circle(cx, cy, r, fill=COLORS[i % 6] if fc.numerator else BG, sw=2.5)
        else:
            step = 2 * math.pi / fc.denominator
            for k in range(fc.denominator):
                c.path(_wedge(cx, cy, r, k * step, (k + 1) * step),
                       fill=COLORS[i % 6] if k < fc.numerator else BG, sw=1.5)
            c.circle(cx, cy, r, sw=2.5)
        c.text(cx, cy + r + 28, f"{fc.numerator}/{fc.denominator}", size=20, weight="bold", anchor="middle")
        if fc.label:
            c.text(cx, cy + r + 48, fc.label, size=13, anchor="middle", color=MUTED)
    return c.finish(cy + r + (60 if any(x.label for x in v.circles) else 44), v.alt)


def _number_line(v: S.NumberLine) -> str:
    c = Canvas(v.title)
    a, b, step = S.parse_number(v.start), S.parse_number(v.end), S.parse_number(v.major_step)
    x0, x1 = 40, W - 40
    has_jumps = bool(v.jumps)
    y = c.y0 + (95 if has_jumps else 55)
    X = lambda val: x0 + (val - a) / (b - a) * (x1 - x0)
    frac = v.label_format == "fraction"
    c.line(x0 - 12, y, x1 + 16, y, sw=2, arrow=True)
    n_major = int(round((b - a) / step))
    for i in range(n_major + 1):
        val = a + i * step
        c.line(X(val), y - 10, X(val), y + 10, sw=2)
        c.text(X(val), y + 30, fmt_num(val, frac), size=14, anchor="middle")
        if v.minor_divisions and i < n_major:
            for k in range(1, v.minor_divisions):
                mv = val + k * step / v.minor_divisions
                c.line(X(mv), y - 5, X(mv), y + 5, sw=1.2)
    for j in v.jumps:
        s, e = X(S.parse_number(j.start)), X(S.parse_number(j.end))
        h = min(70, max(28, abs(e - s) * 0.45))
        c.path(f"M{s:.1f},{y - 4:.1f} Q{(s + e) / 2:.1f},{y - h * 2:.1f} {e:.1f},{y - 4:.1f}",
               stroke=COLORS[1], sw=2.2, arrow=True)
        if j.label:
            c.text((s + e) / 2, y - h - 8, j.label, size=14, anchor="middle", weight="bold", color=INK)
    for m in v.marks:
        mx = X(S.parse_number(m.value))
        if m.question:
            c.line(mx, y + 4, mx, y + 14, stroke=COLORS[1])
            c.rect(mx - 14, y + 14, 28, 26, fill=LIGHT[1], stroke=COLORS[1], rx=6)
            c.text(mx, y + 27, "?", size=17, weight="bold", anchor="middle", baseline="central")
        c.circle(mx, y, 7 if m.highlight else 5, fill=ACCENT if m.highlight else COLORS[0], stroke=BG, sw=2)
        if m.label:
            c.text(mx, y + (58 if m.question else 52), m.label, size=13, anchor="middle", color=ACCENT if m.highlight else MUTED,
                   weight="bold" if m.highlight else "normal")
    extra = 66 if any(m.label for m in v.marks) else (52 if any(m.question for m in v.marks) else 48)
    return c.finish(y + extra, v.alt)


def _place_value_chart(v: S.PlaceValueChart) -> str:
    c = Canvas(v.title)
    n = len(v.columns)
    cw = min(62, (W - 40) / n)
    x0 = (W - cw * n) / 2
    y = c.y0 + 14
    rh = 44
    for i, col in enumerate(v.columns):
        hl = i in v.highlight_columns
        c.rect(x0 + i * cw, y, cw, rh, fill=COLORS[0] if hl else LIGHT[0], sw=1.5)
        c.text(x0 + i * cw + cw / 2, y + rh / 2, col, size=16, weight="bold", anchor="middle",
               baseline="central", color=BG if hl else INK)
    for r, row in enumerate(v.rows):
        ry = y + rh * (r + 1)
        for i, cell in enumerate(row):
            c.rect(x0 + i * cw, ry, cw, rh, fill=LIGHT[0] if i in v.highlight_columns else BG, sw=1.5)
            c.text(x0 + i * cw + cw / 2, ry + rh / 2, cell, size=20, anchor="middle", baseline="central")
    if v.decimal_after is not None and 0 <= v.decimal_after < n - 1:
        xd = x0 + (v.decimal_after + 1) * cw
        c.line(xd, y - 4, xd, y + rh * (len(v.rows) + 1) + 4, stroke=ACCENT, sw=4)
    return c.finish(y + rh * (len(v.rows) + 1) + 20, v.alt)


def _area_model(v: S.AreaModel) -> str:
    c = Canvas(v.title)
    cell = min(360 / v.cols, 300 / v.rows, 40)
    gw, gh = cell * v.cols, cell * v.rows
    x0 = (W - gw) / 2 + (10 if v.row_label else 0)
    y0 = c.y0 + (34 if v.col_label else 14)
    if v.col_label:
        c.text(x0 + gw / 2, y0 - 12, v.col_label, size=14, anchor="middle", color=MUTED)
    if v.row_label:
        c.add(f'<text x="{x0 - 14:.1f}" y="{y0 + gh / 2:.1f}" font-size="14" fill="{MUTED}" text-anchor="middle" '
              f'font-family="{FONT}" transform="rotate(-90 {x0 - 14:.1f} {y0 + gh / 2:.1f})">{escape(v.row_label)}</text>')
    for reg in v.regions:
        c.rect(x0 + reg.col_start * cell, y0 + reg.row_start * cell, (reg.col_end - reg.col_start) * cell,
               (reg.row_end - reg.row_start) * cell, fill=COLORS[reg.color], stroke="none", opacity=0.45)
    for r in range(v.rows + 1):
        c.line(x0, y0 + r * cell, x0 + gw, y0 + r * cell, stroke=MUTED, sw=1)
    for k in range(v.cols + 1):
        c.line(x0 + k * cell, y0, x0 + k * cell, y0 + gh, stroke=MUTED, sw=1)
    c.rect(x0, y0, gw, gh, sw=2.5)
    y = y0 + gh + 24
    for reg in v.regions:
        if reg.label:
            c.rect(40, y - 12, 16, 16, fill=COLORS[reg.color], stroke="none", opacity=0.6)
            c.text(64, y + 1, reg.label, size=14, baseline="central")
            y += 24
    return c.finish(y + 4, v.alt)


def _coordinate_plane(v: S.CoordinatePlane) -> str:
    c = Canvas(v.title)
    (xa, xb), (ya, yb) = v.x_range, v.y_range
    size = 380
    sx = size / (xb - xa)
    sy = size / (yb - ya)
    s = min(sx, sy)
    pw, ph = (xb - xa) * s, (yb - ya) * s
    ox, oy = (W - pw) / 2, c.y0 + 16
    X = lambda x: ox + (x - xa) * s
    Y = lambda y: oy + (yb - y) * s
    step = nice_step(max(xb - xa, yb - ya), 12)
    if v.grid:
        g = math.ceil(xa / step) * step
        while g <= xb + 1e-9:
            c.line(X(g), oy, X(g), oy + ph, stroke=GRID, sw=1)
            g += step
        g = math.ceil(ya / step) * step
        while g <= yb + 1e-9:
            c.line(ox, Y(g), ox + pw, Y(g), stroke=GRID, sw=1)
            g += step
    ax_y = Y(0) if ya <= 0 <= yb else oy + ph
    ax_x = X(0) if xa <= 0 <= xb else ox
    c.line(ox - 6, ax_y, ox + pw + 10, ax_y, sw=1.8, arrow=True)
    c.line(ax_x, oy + ph + 6, ax_x, oy - 10, sw=1.8, arrow=True)
    c.text(ox + pw + 8, ax_y - 10, v.x_label, size=14, weight="bold", italic=True)
    c.text(ax_x + 10, oy - 4, v.y_label, size=14, weight="bold", italic=True)
    g = math.ceil(xa / step) * step
    while g <= xb + 1e-9:
        if abs(g) > 1e-9:
            c.line(X(g), ax_y - 4, X(g), ax_y + 4, sw=1.5)
            c.text(X(g), ax_y + 18, fmt_num(g), size=12, anchor="middle", color=MUTED)
        g += step
    g = math.ceil(ya / step) * step
    while g <= yb + 1e-9:
        if abs(g) > 1e-9:
            c.line(ax_x - 4, Y(g), ax_x + 4, Y(g), sw=1.5)
            c.text(ax_x - 8, Y(g), fmt_num(g), size=12, anchor="end", baseline="central", color=MUTED)
        g += step
    for i, fn in enumerate(v.functions):
        f = S.compile_expression(fn.expression)
        segs, cur = [], []
        for k in range(301):
            x = xa + (xb - xa) * k / 300
            y = f(x)
            if y is None or not (ya - 0.01 <= y <= yb + 0.01):
                if len(cur) > 1:
                    segs.append(cur)
                cur = []
                continue
            cur.append((X(x), Y(y)))
        if len(cur) > 1:
            segs.append(cur)
        for sg in segs:
            c.path("M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in sg), stroke=COLORS[i % 6], sw=2.6)
        if fn.label and segs:
            pts = [p for sg in segs for p in sg]
            lx, ly = pts[int(len(pts) * 0.8)]
            lx = min(lx + 10, ox + pw - text_width(fn.label, 14) - 4)
            c.text(lx, min(max(ly + 4, oy + 14), oy + ph - 6), fn.label, size=14, weight="bold",
                   baseline="central", color=COLORS[i % 6])
    for sg in v.segments:
        c.line(X(sg.start[0]), Y(sg.start[1]), X(sg.end[0]), Y(sg.end[1]), stroke=COLORS[2], sw=2.4,
               dash="6 5" if sg.dashed else None)
        if sg.label:
            c.text((X(sg.start[0]) + X(sg.end[0])) / 2 + 6, (Y(sg.start[1]) + Y(sg.end[1])) / 2 - 6, sg.label,
                   size=13, color=COLORS[2], weight="bold")
    for p in v.points:
        c.circle(X(p.x), Y(p.y), 5.5, fill=ACCENT, stroke=BG, sw=2)
        if p.label:
            c.text(X(p.x) + 9, Y(p.y) - 9, p.label, size=14, weight="bold")
    return c.finish(oy + ph + 32, v.alt)


def _geometry(v: S.Geometry) -> str:
    c = Canvas(v.title)
    s = 38
    ox, oy = (W - 10 * s) / 2, c.y0 + 14
    X = lambda x: ox + x * s
    Y = lambda y: oy + (10 - y) * s
    if v.show_grid:
        for k in range(11):
            c.line(X(k), Y(0), X(k), Y(10), stroke=GRID, sw=1)
            c.line(X(0), Y(k), X(10), Y(k), stroke=GRID, sw=1)
    for sh in v.shapes:
        dash = "7 5" if sh.dashed else None
        pts = sh.points
        if sh.kind == "polygon":
            fill = LIGHT[sh.fill] if sh.fill is not None else "none"
            d = "M" + " L".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in pts) + " Z"
            c.path(d, fill=fill, stroke=INK, sw=2.2, dash=dash)
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            for i, lab in enumerate(sh.vertex_labels[: len(pts)]):
                px, py = pts[i]
                dx, dy = px - cx, py - cy
                n = math.hypot(dx, dy) or 1
                c.text(X(px + dx / n * 0.45), Y(py + dy / n * 0.45), lab, size=15, weight="bold",
                       anchor="middle", baseline="central")
            for i, lab in enumerate(sh.side_labels[: len(pts)]):
                (ax, ay), (bx, by) = pts[i], pts[(i + 1) % len(pts)]
                mx, my = (ax + bx) / 2, (ay + by) / 2
                dx, dy = mx - cx, my - cy
                n = math.hypot(dx, dy) or 1
                c.text(X(mx + dx / n * 0.75), Y(my + dy / n * 0.75), lab, size=14, anchor="middle",
                       baseline="central", color=COLORS[0], weight="bold")
        elif sh.kind == "circle":
            (px, py) = pts[0]
            fill = LIGHT[sh.fill] if sh.fill is not None else "none"
            c.add(f'<circle cx="{X(px):.1f}" cy="{Y(py):.1f}" r="{sh.radius * s:.1f}" fill="{fill}" stroke="{INK}" '
                  f'stroke-width="2.2"' + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")
            c.circle(X(px), Y(py), 3, fill=INK)
        elif sh.kind in ("segment", "ray"):
            (ax, ay), (bx, by) = pts[0], pts[1]
            if sh.kind == "ray":
                dx, dy = bx - ax, by - ay
                n = math.hypot(dx, dy) or 1
                t = 30
                bx, by = ax + dx / n * t, ay + dy / n * t
                bx, by = max(0, min(10, bx)), max(0, min(10, by))
            c.line(X(ax), Y(ay), X(bx), Y(by), sw=2.2, dash=dash, arrow=sh.kind == "ray")
            for i, lab in enumerate(sh.vertex_labels[:2]):
                px, py = pts[i]
                c.text(X(px), Y(py) - 12, lab, size=15, weight="bold", anchor="middle")
        elif sh.kind == "point":
            px, py = pts[0]
            c.circle(X(px), Y(py), 4.5, fill=INK)
        elif sh.kind == "angle":
            (ax, ay), (vx, vy), (bx, by) = pts[0], pts[1], pts[2]
            c.line(X(vx), Y(vy), X(ax), Y(ay), sw=2.2)
            c.line(X(vx), Y(vy), X(bx), Y(by), sw=2.2)
            a1 = math.atan2(ay - vy, ax - vx)
            a2 = math.atan2(by - vy, bx - vx)
            delta = (a2 - a1) % (2 * math.pi)
            if delta > math.pi:
                a1, a2, delta = a2, a1, 2 * math.pi - delta
            r = 0.9
            if abs(math.degrees(delta) - 90) < 0.6:
                u = (math.cos(a1) * 0.5, math.sin(a1) * 0.5)
                w = (math.cos(a2) * 0.5, math.sin(a2) * 0.5)
                d = (f"M{X(vx + u[0]):.1f},{Y(vy + u[1]):.1f} L{X(vx + u[0] + w[0]):.1f},{Y(vy + u[1] + w[1]):.1f} "
                     f"L{X(vx + w[0]):.1f},{Y(vy + w[1]):.1f}")
                c.path(d, stroke=ACCENT, sw=2)
            else:
                p1 = (vx + r * math.cos(a1), vy + r * math.sin(a1))
                p2 = (vx + r * math.cos(a2), vy + r * math.sin(a2))
                c.path(f"M{X(p1[0]):.1f},{Y(p1[1]):.1f} A{r * s:.1f},{r * s:.1f} 0 0 0 {X(p2[0]):.1f},{Y(p2[1]):.1f}",
                       fill="none", stroke=ACCENT, sw=2.2)
            mid = a1 + delta / 2
            if sh.label:
                c.text(X(vx + 1.45 * math.cos(mid)), Y(vy + 1.45 * math.sin(mid)), sh.label, size=14,
                       weight="bold", anchor="middle", baseline="central", color=ACCENT)
            for i, lab in enumerate(sh.vertex_labels[:3]):
                px, py = pts[i]
                c.text(X(px) + 8, Y(py) - 8, lab, size=15, weight="bold")
        if sh.label and sh.kind not in ("angle",):
            px, py = pts[0]
            c.text(X(px) + 10, Y(py) + 20, sh.label, size=14, color=MUTED)
    return c.finish(oy + 10 * s + 18, v.alt)


def _weights(c: Canvas, items: list[str], cx: float, base_y: float) -> None:
    per_row = 4
    for i, it in enumerate(items):
        row, col = divmod(i, per_row)
        n_in_row = min(per_row, len(items) - row * per_row)
        x = cx + (col - (n_in_row - 1) / 2) * 38
        y = base_y - 18 - row * 36
        is_var = any(ch.isalpha() for ch in it)
        if is_var:
            c.rect(x - 16, y - 16, 32, 32, fill=LIGHT[0], stroke=COLORS[0], sw=2, rx=5)
        else:
            c.circle(x, y, 16, fill=LIGHT[1], stroke=COLORS[1], sw=2)
        c.text(x, y, it, size=14, weight="bold", anchor="middle", baseline="central", italic=is_var)


def _balance_scale(v: S.BalanceScale) -> str:
    c = Canvas(v.title)
    cx = W / 2
    top = c.y0 + 40
    tilt = 0 if v.balanced else (14 if (v.heavier or "left") == "left" else -14)
    arm = 170
    lx, ly = cx - arm, top + tilt
    rx, ry = cx + arm, top - tilt
    c.path(f"M{cx - 50:.1f},{top + 170:.1f} L{cx + 50:.1f},{top + 170:.1f} L{cx:.1f},{top:.1f} Z",
           fill=LIGHT[2], stroke=INK, sw=2)
    c.line(lx, ly, rx, ry, sw=4)
    c.circle(cx, top, 6, fill=INK)
    drop = 26 + 36 * math.ceil(max(len(v.left), len(v.right)) / 4)
    for px, py, items in ((lx, ly, v.left), (rx, ry, v.right)):
        c.line(px - 70, py + drop, px, py, sw=1.2, stroke=MUTED)
        c.line(px + 70, py + drop, px, py, sw=1.2, stroke=MUTED)
        c.path(f"M{px - 80:.1f},{py + drop:.1f} L{px + 80:.1f},{py + drop:.1f} L{px + 64:.1f},{py + drop + 12:.1f} "
               f"L{px - 64:.1f},{py + drop + 12:.1f} Z", fill=GRID, stroke=INK, sw=2)
        _weights(c, items, px, py + drop)
    sign = "=" if v.balanced else ("≠")
    c.text(cx, top + 204, sign, size=26, weight="bold", anchor="middle")
    return c.finish(top + 224, v.alt)


def _bar_chart(v: S.BarChart) -> str:
    c = Canvas(v.title)
    x0, x1 = 70, W - 30
    y0 = c.y0 + 20
    h = 240
    vmax = max(v.values) or 1
    step = nice_step(vmax, 5)
    top = math.ceil(vmax / step) * step
    Y = lambda val: y0 + h - val / top * h
    g = 0.0
    while g <= top + 1e-9:
        c.line(x0, Y(g), x1, Y(g), stroke=GRID, sw=1)
        c.text(x0 - 8, Y(g), fmt_num(g), size=12, anchor="end", baseline="central", color=MUTED)
        g += step
    n = len(v.categories)
    slot = (x1 - x0) / n
    bw = min(56, slot * 0.62)
    for i, (cat, val) in enumerate(zip(v.categories, v.values)):
        bx = x0 + slot * i + (slot - bw) / 2
        col = ACCENT if i in v.highlight else COLORS[0]
        c.rect(bx, Y(val), bw, y0 + h - Y(val), fill=col, stroke="none", rx=3)
        c.text(bx + bw / 2, Y(val) - 8, fmt_num(val), size=13, anchor="middle", weight="bold")
        for li, line in enumerate(wrap(cat, max(6, int(slot / 8)))[:2]):
            c.text(bx + bw / 2, y0 + h + 20 + li * 16, line, size=13, anchor="middle")
    c.line(x0, y0 + h, x1, y0 + h, sw=1.8)
    if v.y_label:
        c.add(f'<text x="18" y="{y0 + h / 2:.1f}" font-size="13" fill="{MUTED}" text-anchor="middle" '
              f'font-family="{FONT}" transform="rotate(-90 18 {y0 + h / 2:.1f})">{escape(v.y_label)}</text>')
    return c.finish(y0 + h + 58, v.alt)


# --------------------------------------------------------------------------- Sprache
def _flow_boxes(c: Canvas, boxes: list[tuple[str, str | None, int | None]], y: float, size=18) -> float:
    """Wortboxen mit Zeilenumbruch. boxes: (text, unterschrift, farbindex). Gibt die Endhöhe zurück."""
    x, pad, gap = 30, 10, 8
    line_h = 70
    for text, sub, col in boxes:
        if col is None and not any(ch.isalnum() for ch in text):
            c.text(x - gap + 2, y + 18, text, size=size, baseline="central")
            x += text_width(text, size)
            continue
        w = max(text_width(text, size) + 2 * pad, text_width(sub or "", 11) + 8)
        if x + w > W - 30:
            x, y = 30, y + line_h
        if col is not None:
            c.rect(x, y, w, 36, fill=LIGHT[col], stroke=COLORS[col], sw=2, rx=7)
        c.text(x + w / 2, y + 18, text, size=size, anchor="middle", baseline="central")
        if sub:
            c.text(x + w / 2, y + 52, sub, size=11, anchor="middle", color=COLORS[col] if col is not None else MUTED,
                   weight="bold")
        x += w + gap
    return y + line_h


def _sentence_parts(v: S.SentenceParts) -> str:
    c = Canvas(v.title)
    roles: dict[str, int] = {}
    boxes = []
    for t in v.tokens:
        col = None
        if t.role:
            col = roles.setdefault(t.role, len(roles) % 6)
        boxes.append((t.text, t.role, col))
    end = _flow_boxes(c, boxes, c.y0 + 16)
    return c.finish(end + 4, v.alt)


KIND_COLOR = {"stamm": 0, "praefix": 1, "suffix": 2, "endung": 3, "fuge": 5, "sonstiges": 4}
KIND_NAME = {"stamm": "Wortstamm", "praefix": "Vorsilbe", "suffix": "Nachsilbe", "endung": "Endung",
             "fuge": "Fugenelement", "sonstiges": "Baustein"}


def _word_parts(v: S.WordParts) -> str:
    c = Canvas(v.title)
    y = c.y0 + 16
    used = []
    for word in v.words:
        x = 40
        for p in word.parts:
            col = KIND_COLOR[p.kind]
            w = text_width(p.text, 22) + 16
            c.rect(x, y, w, 42, fill=LIGHT[col], stroke=COLORS[col], sw=2, rx=6)
            c.text(x + w / 2, y + 21, p.text, size=22, anchor="middle", baseline="central")
            x += w + 3
            if p.kind not in used:
                used.append(p.kind)
        y += 58
    x = 40
    for k in used:
        col = KIND_COLOR[k]
        c.rect(x, y, 14, 14, fill=LIGHT[col], stroke=COLORS[col], sw=2, rx=3)
        c.text(x + 20, y + 7, KIND_NAME[k], size=13, baseline="central")
        x += text_width(KIND_NAME[k], 13) + 44
    return c.finish(y + 30, v.alt)


def _syllables(v: S.Syllables) -> str:
    c = Canvas(v.title)
    y = c.y0 + 34
    arc_colors = [COLORS[0], ACCENT]
    for word in v.words:
        x = 40
        for i, syl in enumerate(word.syllables):
            w = text_width(syl, 28) + 4
            c.text(x + w / 2, y, syl, size=28, anchor="middle", color=arc_colors[i % 2])
            c.path(f"M{x + 3:.1f},{y + 10:.1f} Q{x + w / 2:.1f},{y + 34:.1f} {x + w - 3:.1f},{y + 10:.1f}",
                   stroke=arc_colors[i % 2], sw=2.6)
            x += w + 2
        y += 70
    return c.finish(y - 20, v.alt)


# --------------------------------------------------------------------------- Sachfächer / allgemein
NODE_PAD_X, NODE_PAD_Y, NODE_LINE = 12, 9, 17
NODE_SIZE, EDGE_SIZE = 14, 12


def _node_layout(label: str, max_w: float = 156, min_w: float = 92) -> tuple[float, float, list[str]]:
    """Größe eines Kastens so, dass der ganze Text hineinpasst (nie abschneiden)."""
    lines = wrap_px(label, max_w - 2 * NODE_PAD_X, NODE_SIZE)
    w = max(min_w, max(text_width(ln, NODE_SIZE) for ln in lines) + 2 * NODE_PAD_X)
    return w, 2 * NODE_PAD_Y + NODE_LINE * len(lines), lines


def _draw_node(c: Canvas, cx: float, cy: float, box: tuple[float, float, list[str]], col: int) -> None:
    w, h, lines = box
    c.rect(cx - w / 2, cy - h / 2, w, h, fill=LIGHT[col], stroke=COLORS[col], sw=2, rx=10)
    for i, ln in enumerate(lines):
        c.text(cx, cy - (len(lines) - 1) * NODE_LINE / 2 + i * NODE_LINE, ln, size=NODE_SIZE, anchor="middle",
               baseline="central")


def _node_box(c: Canvas, cx: float, cy: float, label: str, w=128, col=0) -> tuple[float, float]:
    box = _node_layout(label, max_w=max(w, 92), min_w=min(w, 92))
    _draw_node(c, cx, cy, box, col)
    return box[0], box[1]


def _overlaps(a, b, pad=2.0) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _place_label(text: str, candidates, blocked: list, width: float) -> tuple[float, float, tuple]:
    """Erster freier Platz aus `candidates` (x, y) für eine Beschriftung – nicht auf Kästen, nicht auf anderen
    Beschriftungen, nicht über den Rand."""
    tw, th = text_width(text, EDGE_SIZE) + 8, EDGE_SIZE + 6
    best = None
    for x, y in candidates:
        x = min(max(x, tw / 2 + 4), width - tw / 2 - 4)
        r = (x - tw / 2, y - th / 2, x + tw / 2, y + th / 2)
        if best is None:
            best = (x, y, r)
        if not any(_overlaps(r, b) for b in blocked):
            return x, y, r
    return best


def _draw_label(c: Canvas, x: float, y: float, rect: tuple, text: str) -> None:
    """Beschriftung auf einem hellen Schild: Linien darunter stören das Lesen nicht."""
    c.rect(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1], fill=BG, stroke="none", sw=0, rx=4,
           opacity=0.94)
    c.text(x, y, text, size=EDGE_SIZE, anchor="middle", baseline="central", color=MUTED, italic=True)


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def _cycle(v: S.Cycle) -> str:
    n = len(v.nodes)
    boxes = [_node_layout(label, max_w=140) for label in v.nodes]
    bw = max(b[0] for b in boxes)
    bh = max(b[1] for b in boxes)
    # Radius so groß, dass sich benachbarte Kästen nicht berühren
    R = max(120.0, (max(bw, bh) + 28) / (2 * math.sin(math.pi / n)))
    width = max(W, 2 * R + bw + 40)
    c = Canvas(v.title, width)
    cx, cy = width / 2, c.y0 + 24 + R + bh / 2
    ang = [(-math.pi / 2) + 2 * math.pi * i / n for i in range(n)]
    pos = [(cx + R * math.cos(a), cy + R * math.sin(a)) for a in ang]
    blocked = [(x - b[0] / 2, y - b[1] / 2, x + b[0] / 2, y + b[1] / 2) for (x, y), b in zip(pos, boxes)]
    for i in range(n):
        a0, a1 = ang[i], ang[(i + 1) % n] + (2 * math.pi if i == n - 1 else 0)
        pad = min(0.45, (max(bw, bh) / 2 + 10) / R)
        s, e = a0 + pad, a1 - pad
        x0, y0 = cx + R * math.cos(s), cy + R * math.sin(s)
        x1, y1 = cx + R * math.cos(e), cy + R * math.sin(e)
        c.path(f"M{x0:.1f},{y0:.1f} A{R:.1f},{R:.1f} 0 0 1 {x1:.1f},{y1:.1f}", stroke=MUTED, sw=2.2, arrow=True)
    labels = []
    for i in range(n):
        if i < len(v.arrow_labels) and v.arrow_labels[i]:
            a0, a1 = ang[i], ang[(i + 1) % n] + (2 * math.pi if i == n - 1 else 0)
            mid = (a0 + a1) / 2
            cands = [(cx + (R + d) * math.cos(mid), cy + (R + d) * math.sin(mid)) for d in (0, 24, -24, 40, -40)]
            x, y, r = _place_label(v.arrow_labels[i], cands, blocked, width)
            blocked.append(r)
            labels.append((x, y, r, v.arrow_labels[i]))
    for i, b in enumerate(boxes):
        _draw_node(c, *pos[i], b, i % 6)
    for x, y, r, t in labels:
        _draw_label(c, x, y, r, t)
    if v.center_label:
        inner = max(80, 2 * (R - max(bw, bh) / 2) - 24)
        for i, ln in enumerate(wrap_px(v.center_label, inner, 15, bold=True)[:3]):
            c.text(cx, cy - 9 + i * 18, ln, size=15, weight="bold", anchor="middle", baseline="central")
    bottom = max(y + b[1] / 2 for (_, y), b in zip(pos, boxes))
    bottom = max([bottom] + [r[3] for *_, r, _t in labels])
    return c.finish(bottom + 20, v.alt)


def _flow_layers(v: S.FlowDiagram) -> tuple[dict[str, int], list[list[str]]]:
    level = {n.id: 0 for n in v.nodes}
    for _ in range(len(v.nodes)):
        for e in v.edges:
            level[e.target] = max(level[e.target], level[e.source] + 1)
    layers: list[list[str]] = [[] for _ in range(max(level.values()) + 1)]
    for n in v.nodes:
        layers[level[n.id]].append(n.id)
    preds: dict[str, list[str]] = {n.id: [] for n in v.nodes}
    succs: dict[str, list[str]] = {n.id: [] for n in v.nodes}
    for e in v.edges:
        preds[e.target].append(e.source)
        succs[e.source].append(e.target)
    # Reihenfolge je Schicht nach dem Mittel der Nachbarn (weniger Kreuzungen)
    for _ in range(2):
        for L in range(1, len(layers)):
            idx = {nid: i for lay in layers for i, nid in enumerate(lay)}
            layers[L].sort(key=lambda nid: sum(idx[p] for p in preds[nid]) / len(preds[nid]) if preds[nid]
                           else idx[nid])
        for L in range(len(layers) - 2, -1, -1):
            idx = {nid: i for lay in layers for i, nid in enumerate(lay)}
            layers[L].sort(key=lambda nid: sum(idx[s] for s in succs[nid]) / len(succs[nid]) if succs[nid]
                           else idx[nid])
    return level, layers


def _ports(center: float, span: float, k: int, n: int) -> float:
    if n <= 1:
        return center
    step = min(16.0, span / (n - 1))
    return center + (k - (n - 1) / 2) * step


def _flow_diagram(v: S.FlowDiagram) -> str:
    level, layers = _flow_layers(v)
    horizontal = v.direction == "horizontal"
    boxes = {n.id: _node_layout(n.label, max_w=150 if horizontal else 168) for n in v.nodes}
    edge_lw = [text_width(e.label, EDGE_SIZE) + 12 if e.label else 0 for e in v.edges]
    pos: dict[str, tuple[float, float]] = {}
    M = 18
    if horizontal:
        col_w = [max(boxes[n][0] for n in lay) for lay in layers]
        gaps = []
        for L in range(len(layers) - 1):
            need = max([lw for e, lw in zip(v.edges, edge_lw) if level[e.source] == L] + [0])
            gaps.append(max(56, need + 30))
        width = 2 * M + sum(col_w) + sum(gaps)
        if width > 820 and len(layers) > 1:          # zu breit: untereinander ist lesbarer als winzig
            return _flow_diagram(v.model_copy(update={"direction": "vertical"}))
        width = max(W, width)
        c = Canvas(v.title, width)
        layer_h = [sum(boxes[n][1] for n in lay) + 26 * (len(lay) - 1) for lay in layers]
        H = max(layer_h)
        top = c.y0 + 16
        x = (width - (sum(col_w) + sum(gaps))) / 2
        for L, lay in enumerate(layers):
            y = top + (H - layer_h[L]) / 2
            for nid in lay:
                pos[nid] = (x + col_w[L] / 2, y + boxes[nid][1] / 2)
                y += boxes[nid][1] + 26
            x += col_w[L] + (gaps[L] if L < len(gaps) else 0)
        height = top + H + 22
    else:
        row_w = [sum(boxes[n][0] for n in lay) + 30 * (len(lay) - 1) for lay in layers]
        width = max(W, max(row_w) + 2 * M)
        c = Canvas(v.title, width)
        row_h = [max(boxes[n][1] for n in lay) for lay in layers]
        y = c.y0 + 16
        for L, lay in enumerate(layers):
            x = (width - row_w[L]) / 2
            for nid in lay:
                pos[nid] = (x + boxes[nid][0] / 2, y + row_h[L] / 2)
                x += boxes[nid][0] + 30
            has_label = any(e.label and level[e.source] == L for e in v.edges)
            y += row_h[L] + (74 if has_label else 56)
        height = y - (74 if any(e.label and level[e.source] == len(layers) - 1 for e in v.edges) else 56) + 22

    def rect_of(nid):
        (x, y), (w, h, _) = pos[nid], boxes[nid]
        return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)

    # Anschlusspunkte verteilen, damit mehrere Pfeile nicht in einem Punkt zusammenlaufen
    out_e: dict[str, list[int]] = {}
    in_e: dict[str, list[int]] = {}
    for i, e in enumerate(v.edges):
        out_e.setdefault(e.source, []).append(i)
        in_e.setdefault(e.target, []).append(i)
    axis = 1 if horizontal else 0
    for d in (out_e, in_e):
        for nid, lst in d.items():
            lst.sort(key=lambda i: pos[v.edges[i].target if d is out_e else v.edges[i].source][axis])
    blocked = [rect_of(n.id) for n in v.nodes]
    curves, placed = [], []
    for i, e in enumerate(v.edges):
        (sx, sy), (tx, ty) = pos[e.source], pos[e.target]
        (sw_, sh_, _), (tw_, th_, _) = boxes[e.source], boxes[e.target]
        ko, no = out_e[e.source].index(i), len(out_e[e.source])
        ki, ni = in_e[e.target].index(i), len(in_e[e.target])
        if horizontal:
            p0 = (sx + sw_ / 2, _ports(sy, sh_ - 14, ko, no))
            p3 = (tx - tw_ / 2 - 3, _ports(ty, th_ - 14, ki, ni))
            dx = max(24.0, (p3[0] - p0[0]) / 2)
            p1, p2 = (p0[0] + dx, p0[1]), (p3[0] - dx, p3[1])
        else:
            p0 = (_ports(sx, sw_ - 24, ko, no), sy + sh_ / 2)
            p3 = (_ports(tx, tw_ - 24, ki, ni), ty - th_ / 2 - 3)
            dy = max(20.0, (p3[1] - p0[1]) / 2)
            p1, p2 = (p0[0], p0[1] + dy), (p3[0], p3[1] - dy)
        curves.append((p0, p1, p2, p3))
        c.path(f"M{p0[0]:.1f},{p0[1]:.1f} C{p1[0]:.1f},{p1[1]:.1f} {p2[0]:.1f},{p2[1]:.1f} {p3[0]:.1f},{p3[1]:.1f}",
               stroke=MUTED, sw=2, arrow=True)
    for i, e in enumerate(v.edges):
        if not e.label:
            continue
        cands = [_bezier(*curves[i], t) for t in (0.5, 0.4, 0.6, 0.3, 0.7)]
        off = [(x, y + dy) for x, y in cands[:3] for dy in (-12, 12)] if horizontal else \
            [(x + dx, y) for x, y in cands[:3] for dx in (-40, 40)]
        x, y, r = _place_label(e.label, cands + off, blocked, width)
        blocked.append(r)
        placed.append((x, y, r, e.label))
    for n in v.nodes:
        _draw_node(c, *pos[n.id], boxes[n.id], level[n.id] % 6)
    for x, y, r, t in placed:
        _draw_label(c, x, y, r, t)
    height = max([height] + [r[3] + 12 for *_, r, _t in placed])
    return c.finish(height, v.alt)


def _year(y: int) -> str:
    return f"{-y} v. Chr." if y < 0 else str(y)


def _timeline(v: S.Timeline) -> str:
    c = Canvas(v.title)
    x0, x1 = 40, W - 40
    y = c.y0 + 40 + (24 * len(v.periods) if v.periods else 0) + 60
    X = lambda yr: x0 + (yr - v.start) / (v.end - v.start) * (x1 - x0)
    for i, p in enumerate(v.periods):
        py = c.y0 + 20 + i * 24
        c.rect(X(p.start), py, X(p.end) - X(p.start), 20, fill=LIGHT[i % 6], stroke=COLORS[i % 6], sw=1.5, rx=4)
        c.text((X(p.start) + X(p.end)) / 2, py + 10, p.label, size=12, anchor="middle", baseline="central")
    c.line(x0 - 10, y, x1 + 14, y, sw=2.5, arrow=True)
    step = nice_step(v.end - v.start, 6)
    t = math.ceil(v.start / step) * step
    while t <= v.end:
        c.line(X(t), y - 6, X(t), y + 6, sw=1.5)
        c.text(X(t), y + 22, _year(int(t)), size=12, anchor="middle", color=MUTED)
        t += step
    for i, e in enumerate(sorted(v.events, key=lambda e: e.year)):
        ex = X(e.year)
        up = i % 2 == 0
        ly = y - 30 if up else y + 50
        c.line(ex, y, ex, ly + (8 if up else -14), stroke=MUTED, sw=1, dash="3 3")
        c.circle(ex, y, 6, fill=ACCENT, stroke=BG, sw=2)
        lines = wrap(f"{_year(e.year)}: {e.label}", 22)[:2]
        half = max(text_width(ln, 12) for ln in lines) / 2
        tx = min(max(ex, half + 8), W - half - 8)
        for li, ln in enumerate(lines):
            c.text(tx, ly + li * 15 - (15 if up else 0), ln, size=12, anchor="middle", weight="bold" if li == 0 else "normal")
    return c.finish(y + 90, v.alt)


def _table(v: S.Table) -> str:
    """Spaltenbreiten nach dem Text; lange Zellen werden umbrochen statt über den Rand zu laufen."""
    hl = {tuple(x) for x in v.highlight_cells}
    ncol = len(v.headers)
    grid = [[(h, True) for h in v.headers]] + [[(cell, (r, i) in hl) for i, cell in enumerate(row)]
                                               for r, row in enumerate(v.rows)]
    pad, avail = 10, W - 32
    for size in (14, 13, 12):
        natural = [max(text_width(t, size, bold) for t, bold in (row[i] for row in grid)) + 2 * pad
                   for i in range(ncol)]
        minimum = [max(max(text_width(w, size, bold) for w in (t.split() or [""])) for t, bold in
                       (row[i] for row in grid)) + 2 * pad for i in range(ncol)]
        minimum = [max(m, 40) for m in minimum]
        if sum(minimum) <= avail:
            break
    if sum(natural) <= avail:
        extra = avail - sum(natural)
        widths = [n + extra * n / sum(natural) for n in natural]
    elif sum(minimum) <= avail:
        slack = [n - m for n, m in zip(natural, minimum)]
        widths = [m + (avail - sum(minimum)) * s / (sum(slack) or 1) for m, s in zip(minimum, slack)]
    else:
        widths = minimum                                # breiter als üblich statt unlesbar
    total = sum(widths)
    width = max(W, total + 32)
    c = Canvas(v.title, width)
    line_h = size + 4
    y = c.y0 + 12
    x0 = (width - total) / 2
    for r, row in enumerate(grid):
        cells = [wrap_px(t, widths[i] - 2 * pad, size, bold) for i, (t, bold) in enumerate(row)]
        rh = max(34, 16 + line_h * max(len(ls) for ls in cells))
        x = x0
        for i, ((t, bold), lines) in enumerate(zip(row, cells)):
            fill = LIGHT[0] if r == 0 else (LIGHT[1] if bold else BG)
            c.rect(x, y, widths[i], rh, fill=fill, sw=1.2)
            for k, ln in enumerate(lines):
                c.text(x + widths[i] / 2, y + rh / 2 - (len(lines) - 1) * line_h / 2 + k * line_h, ln, size=size,
                       weight="bold" if bold else "normal", anchor="middle", baseline="central")
            x += widths[i]
        y += rh
    return c.finish(y + 16, v.alt)


def _labeled_diagram(v: S.LabeledDiagram) -> str:
    c = Canvas(v.title)
    dh = {"wide": 170, "square": 250, "tall": 330}[v.aspect]
    dx0, dw = 150, 220
    dy0 = c.y0 + 16
    X = lambda x: dx0 + x / 100 * dw
    Y = lambda y: dy0 + y / 100 * dh
    anchors = []
    for i, p in enumerate(v.parts):
        fill = LIGHT[p.fill] if p.fill is not None else "none"
        stroke = COLORS[p.fill] if p.fill is not None else INK
        if p.shape == "label":
            ax, ay = X(p.x), Y(p.y)
        elif p.shape == "ellipse":
            c.add(f'<ellipse cx="{X(p.x):.1f}" cy="{Y(p.y):.1f}" rx="{p.w / 200 * dw:.1f}" ry="{p.h / 200 * dh:.1f}" '
                  f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
            ax, ay = X(p.x), Y(p.y)
        elif p.shape == "circle":
            c.circle(X(p.x), Y(p.y), p.r / 100 * dw, fill=fill, stroke=stroke, sw=2)
            ax, ay = X(p.x), Y(p.y)
        elif p.shape == "rect":
            c.rect(X(p.x), Y(p.y), p.w / 100 * dw, p.h / 100 * dh, fill=fill, stroke=stroke, sw=2, rx=4)
            ax, ay = X(p.x + p.w), Y(p.y + p.h * 0.3)
        else:
            pts = " L".join(f"{X(px):.1f},{Y(py):.1f}" for px, py in p.points)
            closed = " Z" if p.shape == "polygon" else ""
            c.path("M" + pts + closed, fill=fill if p.shape == "polygon" else "none", stroke=stroke,
                   sw=2 if p.shape == "polygon" else 3)
            ax = sum(X(px) for px, _ in p.points) / len(p.points)
            ay = sum(Y(py) for _, py in p.points) / len(p.points)
        if p.anchor:
            ax, ay = X(p.anchor[0]), Y(p.anchor[1])
        if p.label and (not v.hide_labels or v.number_parts):
            anchors.append((i, ax, ay, p))
    numbers = {id(p): n for n, (_, _, _, p) in enumerate(anchors, start=1)}
    left = sorted([a for a in anchors if a[1] < dx0 + dw / 2], key=lambda a: a[2])
    right = sorted([a for a in anchors if a[1] >= dx0 + dw / 2], key=lambda a: a[2])
    for side, group in (("left", left), ("right", right)):
        last = -1e9
        for i, ax, ay, p in group:
            ly = max(ay, last + 26)
            last = ly
            lx = dx0 - 14 if side == "left" else dx0 + dw + 14
            c.line(ax, ay, lx, ly, stroke=MUTED, sw=1.2)
            c.circle(ax, ay, 2.5, fill=INK, stroke="none")
            text = str(numbers[id(p)]) if v.number_parts else p.label
            if v.number_parts:
                cx = lx - 12 if side == "left" else lx + 12
                c.circle(cx, ly, 11, fill=BG, stroke=INK, sw=1.5)
                c.text(cx, ly, text, size=13, weight="bold", anchor="middle", baseline="central")
            else:
                c.text(lx + (-4 if side == "left" else 4), ly, text, size=14, weight="bold",
                       anchor="end" if side == "left" else "start", baseline="central")
    bottom = max([dy0 + dh] + [a[2] for a in anchors]) + 22
    return c.finish(bottom + 6, v.alt)


def _freeform_svg(v: S.FreeformSVG) -> str:
    import re
    svg = v.svg
    m = re.search(r'viewBox="\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)\s*"', svg)
    if m and " width=" not in svg.split(">", 1)[0]:
        vw, vh = float(m[1]), float(m[2])
        svg = svg.replace("<svg ", f'<svg width="{W}" height="{W * vh / vw:.0f}" role="img" '
                          f'aria-label={quoteattr(v.alt)} ', 1)
    return svg


RENDERERS = {
    "fraction_bar": _fraction_bar, "fraction_circle": _fraction_circle, "number_line": _number_line,
    "place_value_chart": _place_value_chart, "area_model": _area_model, "coordinate_plane": _coordinate_plane,
    "geometry": _geometry, "balance_scale": _balance_scale, "bar_chart": _bar_chart,
    "sentence_parts": _sentence_parts, "word_parts": _word_parts, "syllables": _syllables, "cycle": _cycle,
    "flow_diagram": _flow_diagram, "timeline": _timeline, "table": _table, "labeled_diagram": _labeled_diagram,
    "freeform_svg": _freeform_svg,
}


_TEXT_RE = None


def layout_problems(svg: str) -> list[str]:
    """Findet Beschriftungen, die über den Bildrand laufen oder sich überdecken (geschätzte Textmaße).
    Der Integrator gibt das als Rückmeldung an den Visual-Didaktiker: kürzer formulieren."""
    import re
    global _TEXT_RE
    _TEXT_RE = _TEXT_RE or re.compile(r'<text x="([-\d.]+)" y="([-\d.]+)" font-size="([\d.]+)" '
                                      r'font-weight="(\w+)"[^>]*text-anchor="(\w+)"[^>]*>([^<]*)</text>')
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    if not m:
        return []
    vw, vh = float(m[1]), float(m[2])
    from xml.sax.saxutils import unescape
    boxes = []
    for x, y, size, weight, anchor, t in _TEXT_RE.findall(svg):
        t = unescape(t)
        if not t.strip():
            continue
        x, y, size = float(x), float(y), float(size)
        w = text_width(t, size, weight == "bold") / 1.04
        x0 = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
        boxes.append((x0, y - size * 0.55, x0 + w, y + size * 0.45, t))
    probs = []
    for b in boxes:
        if b[0] < -1 or b[2] > vw + 1 or b[1] < -2 or b[3] > vh + 2:
            probs.append(f"„{b[4]}“ läuft über den Bildrand")
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            ix = min(a[2], b[2]) - max(a[0], b[0])
            iy = min(a[3], b[3]) - max(a[1], b[1])
            if ix > 2 and iy > 3:
                probs.append(f"„{a[4]}“ und „{b[4]}“ überdecken sich")
    return probs[:6]


def render(visual) -> str:
    """Nimmt eine Spec (Pydantic-Objekt oder dict) und gibt SVG zurück."""
    if isinstance(visual, dict):
        visual = parse_spec(visual)
    return RENDERERS[visual.type](visual)
