"""Der Visual-Katalog: rahmenunabhängige Beschreibungen visueller Darstellungen.

Jede Darstellung ist ein JSON-Objekt mit `type` und Parametern. Karo kann sie
- direkt als vorgerendertes SVG anzeigen (liegt in der Datenbank), oder
- aus denselben Daten eigene, interaktive Komponenten bauen (React, Flutter, …).

Keine Videos, keine Pixelbilder. Alle Werte sind prüfbar.
"""
from __future__ import annotations

import ast
import math
import re
from fractions import Fraction
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from ..answers import AnswerSpec, Distractor

# --------------------------------------------------------------------------- Hilfen
_FRAC = re.compile(r"^\s*(-?\d+)\s*/\s*(\d+)\s*$")
_MIXED = re.compile(r"^\s*(-?\d+)\s+(\d+)\s*/\s*(\d+)\s*$")


def parse_number(v: float | int | str) -> float:
    """Zahl, Bruch ('3/4') oder gemischte Zahl ('1 1/2')."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    if m := _MIXED.match(s):
        whole, n, d = int(m[1]), int(m[2]), int(m[3])
        if d == 0:
            raise ValueError(f"Nenner 0 in '{v}'")
        return whole + (n / d if whole >= 0 else -n / d)
    if m := _FRAC.match(s):
        if int(m[2]) == 0:
            raise ValueError(f"Nenner 0 in '{v}'")
        return float(Fraction(int(m[1]), int(m[2])))
    try:
        return float(s)
    except ValueError as exc:
        raise ValueError(f"'{v}' ist keine Zahl und kein Bruch") from exc


_ALLOWED_FUNCS = {"sin": math.sin, "cos": math.cos, "tan": math.tan, "sqrt": math.sqrt, "abs": abs,
                  "log": math.log10, "ln": math.log, "exp": math.exp}
_ALLOWED_NAMES = {"pi": math.pi, "e": math.e}


def compile_expression(expr: str):
    """Sichere Funktionsterme in x (z. B. '2*x+1', 'x**2 - 3', 'sqrt(x)')."""
    src = expr.replace("^", "**").replace(",", ".")
    tree = ast.parse(src, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Load,
                             ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd, ast.Mod)):
            continue
        if isinstance(node, ast.Name) and (node.id == "x" or node.id in _ALLOWED_NAMES or node.id in _ALLOWED_FUNCS):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_FUNCS:
            continue
        raise ValueError(f"Nicht erlaubter Ausdruck im Funktionsterm '{expr}'")
    code = compile(tree, "<term>", "eval")

    def f(x: float) -> float | None:
        try:
            y = eval(code, {"__builtins__": {}}, {"x": x, **_ALLOWED_NAMES, **_ALLOWED_FUNCS})  # noqa: S307
            return float(y) if math.isfinite(y) else None
        except (ValueError, ZeroDivisionError, OverflowError, TypeError):
            return None
    return f


Label = Annotated[str, Field(max_length=80)]


class _Base(BaseModel):
    title: str | None = Field(None, max_length=100)
    alt: str = Field(min_length=5, max_length=400,
                     description="Textbeschreibung für Screenreader und für die Inspektor-Prüfung")


# --------------------------------------------------------------------------- Mathematik
class FractionPart(BaseModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=1, le=24)
    label: Label | None = None

    @model_validator(mode="after")
    def _proper(self):
        if self.numerator > self.denominator:
            raise ValueError(f"{self.numerator}/{self.denominator}: Zähler größer als Nenner – "
                             "unechte Brüche als mehrere Streifen/Kreise darstellen")
        return self


class FractionBar(_Base):
    """Bruchstreifen: jeder Streifen = ein Ganzes, in `denominator` Teile geteilt, `numerator` gefärbt."""
    type: Literal["fraction_bar"]
    bars: list[FractionPart] = Field(min_length=1, max_length=6)
    show_fraction_labels: bool = True


class FractionCircle(_Base):
    """Kreisteile (Pizza/Torte): jeder Kreis = ein Ganzes."""
    type: Literal["fraction_circle"]
    circles: list[FractionPart] = Field(min_length=1, max_length=4)


class NumberLineMark(BaseModel):
    value: float | int | str
    label: Label | None = None
    highlight: bool = False
    question: bool = Field(False, description="als '?' zeigen (Aufgabe)")


class NumberLineJump(BaseModel):
    start: float | int | str
    end: float | int | str
    label: Label | None = None


class NumberLine(_Base):
    """Zahlenstrahl mit Markierungen und Sprüngen (Bögen)."""
    type: Literal["number_line"]
    start: float | int | str
    end: float | int | str
    major_step: float | int | str
    minor_divisions: int = Field(0, ge=0, le=12, description="Unterteilung zwischen zwei Hauptstrichen")
    label_format: Literal["number", "fraction"] = "number"
    marks: list[NumberLineMark] = Field(default_factory=list, max_length=12)
    jumps: list[NumberLineJump] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _check(self):
        a, b, step = parse_number(self.start), parse_number(self.end), parse_number(self.major_step)
        if b <= a:
            raise ValueError("number_line: end muss größer als start sein")
        if step <= 0 or (b - a) / step > 30:
            raise ValueError("number_line: major_step muss > 0 sein und höchstens 30 Hauptstriche ergeben")
        for m in self.marks:
            if not a <= parse_number(m.value) <= b:
                raise ValueError(f"number_line: Markierung {m.value} liegt außerhalb von {self.start}..{self.end}")
        for j in self.jumps:
            for v in (j.start, j.end):
                if not a <= parse_number(v) <= b:
                    raise ValueError(f"number_line: Sprung-Endpunkt {v} liegt außerhalb des Strahls")
        return self


class PlaceValueChart(_Base):
    """Stellenwerttafel, z. B. T H Z E , z h."""
    type: Literal["place_value_chart"]
    columns: list[Annotated[str, Field(max_length=6)]] = Field(min_length=2, max_length=12)
    rows: list[list[Annotated[str, Field(max_length=3)]]] = Field(min_length=1, max_length=6)
    decimal_after: int | None = Field(None, description="Komma nach dieser Spalte (0-basiert)")
    highlight_columns: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        for r in self.rows:
            if len(r) != len(self.columns):
                raise ValueError("place_value_chart: jede Zeile braucht genau so viele Einträge wie Spalten ('' für leer)")
        return self


class GridRegion(BaseModel):
    row_start: int = Field(ge=0)
    row_end: int = Field(ge=1, description="exklusiv")
    col_start: int = Field(ge=0)
    col_end: int = Field(ge=1, description="exklusiv")
    color: int = Field(0, ge=0, le=5, description="Farbindex der Palette")
    label: Label | None = None


class AreaModel(_Base):
    """Rasterfeld / Flächenmodell (Multiplikation, Bruch von Bruch, Prozent auf 10×10)."""
    type: Literal["area_model"]
    rows: int = Field(ge=1, le=20)
    cols: int = Field(ge=1, le=20)
    regions: list[GridRegion] = Field(default_factory=list, max_length=6)
    row_label: Label | None = None
    col_label: Label | None = None

    @model_validator(mode="after")
    def _check(self):
        for r in self.regions:
            if r.row_end > self.rows or r.col_end > self.cols or r.row_start >= r.row_end or r.col_start >= r.col_end:
                raise ValueError("area_model: Region liegt außerhalb des Rasters oder ist leer")
        return self


class PlanePoint(BaseModel):
    x: float
    y: float
    label: Label | None = None


class PlaneSegment(BaseModel):
    start: tuple[float, float]
    end: tuple[float, float]
    label: Label | None = None
    dashed: bool = False


class PlaneFunction(BaseModel):
    expression: str = Field(max_length=60, description="Term in x, z. B. '2*x+1' oder 'x^2-3'")
    label: Label | None = None

    @field_validator("expression")
    @classmethod
    def _expr(cls, v: str) -> str:
        compile_expression(v)
        return v


class CoordinatePlane(_Base):
    """Koordinatensystem mit Punkten, Strecken und Funktionsgraphen."""
    type: Literal["coordinate_plane"]
    x_range: tuple[float, float] = (-5, 5)
    y_range: tuple[float, float] = (-5, 5)
    grid: bool = True
    x_label: Label = "x"
    y_label: Label = "y"
    points: list[PlanePoint] = Field(default_factory=list, max_length=12)
    segments: list[PlaneSegment] = Field(default_factory=list, max_length=12)
    functions: list[PlaneFunction] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _check(self):
        (x0, x1), (y0, y1) = self.x_range, self.y_range
        if x1 <= x0 or y1 <= y0 or (x1 - x0) > 100 or (y1 - y0) > 100:
            raise ValueError("coordinate_plane: ungültiger Bereich (max. 100 Einheiten)")
        for p in self.points:
            if not (x0 <= p.x <= x1 and y0 <= p.y <= y1):
                raise ValueError(f"coordinate_plane: Punkt ({p.x}|{p.y}) liegt außerhalb des Bereichs")
        return self


class GeoShape(BaseModel):
    kind: Literal["polygon", "circle", "segment", "angle", "point", "ray"]
    points: list[tuple[float, float]] = Field(default_factory=list,
                                              description="Koordinaten im Bereich 0..10; angle: [Schenkelpunkt, Scheitel, Schenkelpunkt]")
    radius: float | None = None
    label: Label | None = None
    vertex_labels: list[Annotated[str, Field(max_length=4)]] = Field(default_factory=list)
    side_labels: list[Label] = Field(default_factory=list)
    dashed: bool = False
    fill: int | None = Field(None, ge=0, le=5)

    @model_validator(mode="after")
    def _check(self):
        need = {"polygon": 3, "circle": 1, "segment": 2, "angle": 3, "point": 1, "ray": 2}[self.kind]
        if len(self.points) < need:
            raise ValueError(f"geometry: {self.kind} braucht mindestens {need} Punkte")
        if self.kind == "circle" and not self.radius:
            raise ValueError("geometry: circle braucht radius")
        for x, y in self.points:
            if not (-0.01 <= x <= 10.01 and -0.01 <= y <= 10.01):
                raise ValueError("geometry: Koordinaten müssen zwischen 0 und 10 liegen")
        return self


class Geometry(_Base):
    """Geometrische Figuren auf einer 10×10-Zeichenfläche."""
    type: Literal["geometry"]
    shapes: list[GeoShape] = Field(min_length=1, max_length=10)
    show_grid: bool = False


class BalanceScale(_Base):
    """Waage für Gleichungen: Gewichte links und rechts."""
    type: Literal["balance_scale"]
    left: list[Annotated[str, Field(max_length=8)]] = Field(min_length=1, max_length=10)
    right: list[Annotated[str, Field(max_length=8)]] = Field(min_length=1, max_length=10)
    balanced: bool = True
    heavier: Literal["left", "right"] | None = Field(None, description="nur wenn balanced=false")


class BarChart(_Base):
    """Säulendiagramm."""
    type: Literal["bar_chart"]
    categories: list[Label] = Field(min_length=1, max_length=10)
    values: list[float]
    y_label: Label | None = None
    highlight: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        if len(self.values) != len(self.categories):
            raise ValueError("bar_chart: gleich viele Werte wie Kategorien")
        if any(v < 0 for v in self.values):
            raise ValueError("bar_chart: nur Werte >= 0")
        return self


# --------------------------------------------------------------------------- Sprache
class SentenceToken(BaseModel):
    text: Annotated[str, Field(max_length=40)]
    role: Annotated[str, Field(max_length=30)] | None = Field(None, description="z. B. Subjekt, Prädikat, Objekt")


class SentenceParts(_Base):
    """Satzglieder farbig markiert (mit Legende)."""
    type: Literal["sentence_parts"]
    tokens: list[SentenceToken] = Field(min_length=1, max_length=20)


class WordPart(BaseModel):
    text: Annotated[str, Field(max_length=20)]
    kind: Literal["praefix", "stamm", "suffix", "endung", "fuge", "sonstiges"]


class WordEntry(BaseModel):
    parts: list[WordPart] = Field(min_length=1, max_length=6)


class WordParts(_Base):
    """Wortbausteine: Präfix, Stamm, Suffix, Endung."""
    type: Literal["word_parts"]
    words: list[WordEntry] = Field(min_length=1, max_length=6)


class SyllableWord(BaseModel):
    syllables: list[Annotated[str, Field(max_length=12)]] = Field(min_length=1, max_length=8)


class Syllables(_Base):
    """Silbenbögen unter Wörtern."""
    type: Literal["syllables"]
    words: list[SyllableWord] = Field(min_length=1, max_length=6)


# --------------------------------------------------------------------------- Sachfächer / allgemein
class Cycle(_Base):
    """Kreislauf (Wasserkreislauf, Lebenszyklus …)."""
    type: Literal["cycle"]
    nodes: list[Label] = Field(min_length=3, max_length=8)
    arrow_labels: list[Label] = Field(default_factory=list, description="optional, je Pfeil von Knoten i zu i+1")
    center_label: Label | None = None


class FlowNode(BaseModel):
    id: Annotated[str, Field(max_length=20)]
    label: Label


class FlowEdge(BaseModel):
    source: str
    target: str
    label: Label | None = None


class FlowDiagram(_Base):
    """Ablauf-/Ursache-Wirkungs-Diagramm (gerichtet, ohne Kreise)."""
    type: Literal["flow_diagram"]
    nodes: list[FlowNode] = Field(min_length=2, max_length=12)
    edges: list[FlowEdge] = Field(min_length=1, max_length=16)
    direction: Literal["horizontal", "vertical"] = "vertical"

    @model_validator(mode="after")
    def _check(self):
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("flow_diagram: doppelte Knoten-IDs")
        for e in self.edges:
            if e.source not in ids or e.target not in ids:
                raise ValueError(f"flow_diagram: Kante {e.source}->{e.target} verweist auf unbekannten Knoten")
        from ..integrator import find_cycle
        if find_cycle([(e.source, e.target) for e in self.edges]):
            raise ValueError("flow_diagram: Kreis gefunden – für Kreisläufe den Typ 'cycle' verwenden")
        return self


class TimelineEvent(BaseModel):
    year: int
    label: Label


class TimelinePeriod(BaseModel):
    start: int
    end: int
    label: Label


class Timeline(_Base):
    """Zeitstrahl mit Ereignissen und Epochen (negative Jahre = v. Chr.)."""
    type: Literal["timeline"]
    start: int
    end: int
    events: list[TimelineEvent] = Field(default_factory=list, max_length=12)
    periods: list[TimelinePeriod] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _check(self):
        if self.end <= self.start:
            raise ValueError("timeline: end muss nach start liegen")
        for e in self.events:
            if not self.start <= e.year <= self.end:
                raise ValueError(f"timeline: Ereignis {e.year} liegt außerhalb")
        for p in self.periods:
            if not (self.start <= p.start < p.end <= self.end):
                raise ValueError(f"timeline: Epoche {p.label} ungültig")
        return self


class Table(_Base):
    """Einfache Tabelle (Wertetabelle, Konjugation, Vergleich)."""
    type: Literal["table"]
    headers: list[Label] = Field(min_length=1, max_length=8)
    rows: list[list[Annotated[str, Field(max_length=40)]]] = Field(min_length=1, max_length=12)
    highlight_cells: list[tuple[int, int]] = Field(default_factory=list, description="(zeile, spalte), 0-basiert")

    @model_validator(mode="after")
    def _check(self):
        for r in self.rows:
            if len(r) != len(self.headers):
                raise ValueError("table: jede Zeile braucht so viele Zellen wie Überschriften")
        return self


class DiagramPart(BaseModel):
    id: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,24}$")]
    shape: Literal["ellipse", "circle", "rect", "polygon", "line", "label"] = Field(
        description="'label' = nur Beschriftung, die auf den Punkt (x, y) zeigt, z. B. auf eine Fläche")
    x: float = Field(0, ge=0, le=100, description="Mitte (ellipse/circle) bzw. linke obere Ecke (rect), 0..100")
    y: float = Field(0, ge=0, le=100)
    w: float | None = Field(None, gt=0, le=100, description="Breite (ellipse/rect)")
    h: float | None = Field(None, gt=0, le=100, description="Höhe (ellipse/rect)")
    r: float | None = Field(None, gt=0, le=50, description="Radius (circle)")
    points: list[tuple[float, float]] = Field(default_factory=list, description="polygon/line, Koordinaten 0..100")
    fill: int | None = Field(None, ge=0, le=5, description="Farbindex; leer = nur Umriss")
    label: Label | None = Field(None, description="Beschriftung mit Hinweislinie")
    anchor: tuple[float, float] | None = Field(None, description="Punkt, auf den die Hinweislinie zeigt (0..100); "
                                               "Standard: Mitte bzw. bei rect der rechte Rand")

    @model_validator(mode="after")
    def _check(self):
        if self.shape in ("ellipse", "rect") and not (self.w and self.h):
            raise ValueError(f"labeled_diagram: Teil '{self.id}' ({self.shape}) braucht w und h")
        if self.shape == "circle" and not self.r:
            raise ValueError(f"labeled_diagram: Teil '{self.id}' (circle) braucht r")
        if self.shape == "polygon" and len(self.points) < 3:
            raise ValueError(f"labeled_diagram: Teil '{self.id}' (polygon) braucht mind. 3 Punkte")
        if self.shape == "line" and len(self.points) < 2:
            raise ValueError(f"labeled_diagram: Teil '{self.id}' (line) braucht mind. 2 Punkte")
        for px, py in self.points:
            if not (0 <= px <= 100 and 0 <= py <= 100):
                raise ValueError(f"labeled_diagram: Teil '{self.id}' hat Punkte außerhalb von 0..100")
        return self


class LabeledDiagram(_Base):
    """Beschriftete Schemazeichnung (Zelle, Blüte, Auge, Stromkreis, Vulkan …) aus einfachen Formen.
    Mit number_parts=true werden statt der Beschriftungen Nummern gezeigt (für Aufgaben wie 'Welche Nummer ist …?')."""
    type: Literal["labeled_diagram"]
    parts: list[DiagramPart] = Field(min_length=1, max_length=24)
    aspect: Literal["wide", "square", "tall"] = "square"
    number_parts: bool = False
    hide_labels: bool = False

    @model_validator(mode="after")
    def _check(self):
        ids = [p.id for p in self.parts]
        if len(ids) != len(set(ids)):
            raise ValueError("labeled_diagram: doppelte Teil-IDs")
        return self


class FreeformSVG(_Base):
    """Notlösung, wenn kein Katalogtyp passt: schlichtes, bereinigtes SVG (wird streng geprüft)."""
    type: Literal["freeform_svg"]
    svg: str = Field(max_length=20000)

    @field_validator("svg")
    @classmethod
    def _safe(cls, v: str) -> str:
        from .sanitize import sanitize_svg
        return sanitize_svg(v)


VisualSpec = Annotated[
    Union[FractionBar, FractionCircle, NumberLine, PlaceValueChart, AreaModel, CoordinatePlane, Geometry,
          BalanceScale, BarChart, SentenceParts, WordParts, Syllables, Cycle, FlowDiagram, Timeline, Table,
          LabeledDiagram, FreeformSVG],
    Field(discriminator="type"),
]

CATALOG_TYPES = ["fraction_bar", "fraction_circle", "number_line", "place_value_chart", "area_model",
                 "coordinate_plane", "geometry", "balance_scale", "bar_chart", "sentence_parts", "word_parts",
                 "syllables", "cycle", "flow_diagram", "timeline", "table", "labeled_diagram", "freeform_svg"]


# --------------------------------------------------------------------------- Ausgabe des Visual-Didaktikers
class VisualStep(BaseModel):
    visual: VisualSpec
    caption: str = Field(min_length=3, max_length=300, description="kurzer, kindgerechter Erklärsatz zu diesem Schritt")


class VisualExplanation(BaseModel):
    key: str = Field(description="V1, V2, …")
    purpose: str = Field(max_length=200, description="was diese Erklärung zeigen soll")
    level: Literal["below", "target"] = "target"
    for_misconception: str | None = Field(None, description="Key der Fehlvorstellung (F1, …), falls sie gezielt aufgelöst wird")
    steps: list[VisualStep] = Field(min_length=1, max_length=8)


class VisualItem(BaseModel):
    use: Literal["diagnostic", "practice", "exit"]
    answer: AnswerSpec | None = Field(None, description="Pflicht bei diagnostic/exit")
    distractors: list[Distractor] = Field(default_factory=list)
    prompt: str
    solution: str
    level: Literal["below", "target", "above"]
    grade: int = Field(ge=1, le=13)
    interaction: Literal["view", "select", "mark", "drag", "input"] = "view"
    visual: VisualSpec


class VisualSet(BaseModel):
    concept_id: str
    visual_need: Literal["essential", "helpful", "none"]
    rationale: str = Field(max_length=400, description="warum diese Darstellung(en) für dieses Konzept")
    explanations: list[VisualExplanation] = Field(default_factory=list, max_length=6)
    visual_items: list[VisualItem] = Field(default_factory=list, max_length=8)
