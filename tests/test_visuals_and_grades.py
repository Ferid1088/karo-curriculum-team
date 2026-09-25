"""Visual-Katalog, Renderer, Sicherheit der freien SVGs, Klassenauswahl und Nachrüsten von Visuals."""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from kcteam.config import load_config
from kcteam.db import DB
from kcteam.pipeline import Pipeline
from kcteam.providers import make_provider
from kcteam.visuals.examples import EXAMPLES
from kcteam.visuals.render import parse_spec, render
from kcteam.visuals.sanitize import sanitize_svg
from kcteam.visuals.spec import CATALOG_TYPES, compile_expression

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")


# ---------------- Katalog & Renderer ----------------
def test_every_catalog_type_has_an_example_that_renders():
    assert set(EXAMPLES) == set(CATALOG_TYPES)
    for name, spec in EXAMPLES.items():
        svg = render(spec)
        root = ET.fromstring(svg)               # gültiges XML
        assert root.tag.endswith("svg"), name
        assert "viewBox" in root.attrib, name
        assert root.attrib.get("width"), f"{name}: Breite fehlt"


def test_alt_text_is_embedded_for_screenreaders():
    svg = render(EXAMPLES["fraction_bar"])
    assert "<title>" in svg and 'role="img"' in svg


@pytest.mark.parametrize("bad", [
    '<svg viewBox="0 0 10 10"><script>alert(1)</script></svg>',
    '<svg viewBox="0 0 10 10" onload="alert(1)"><rect/></svg>',
    '<svg viewBox="0 0 10 10"><image href="https://example.com/x.png"/></svg>',
    '<svg viewBox="0 0 10 10"><rect fill="url(https://evil/x)"/></svg>',
    '<!DOCTYPE svg [<!ENTITY a "b">]><svg viewBox="0 0 1 1"/>',
    '<svg><rect/></svg>',
])
def test_freeform_svg_is_sanitized(bad):
    with pytest.raises(ValueError):
        sanitize_svg(bad)


def test_semantic_validation():
    with pytest.raises(ValidationError):   # Zähler > Nenner
        parse_spec({"type": "fraction_bar", "alt": "falscher Bruch", "bars": [{"numerator": 5, "denominator": 4}]})
    with pytest.raises(ValidationError):   # Markierung außerhalb
        parse_spec({"type": "number_line", "alt": "Strahl 0 bis 1", "start": 0, "end": 1, "major_step": 1,
                    "marks": [{"value": "3/2"}]})
    with pytest.raises(ValidationError):   # Kreis im Ablaufdiagramm
        parse_spec({"type": "flow_diagram", "alt": "Kreis im Ablauf", "nodes": [{"id": "a", "label": "A"},
                    {"id": "b", "label": "B"}], "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]})
    with pytest.raises(ValueError):        # kein Code in Funktionstermen
        compile_expression("__import__('os').system('ls')")
    assert compile_expression("x^2 - 3")(2) == 1


# ---------------- Klassenauswahl & Visuals in der Datenbank ----------------
@pytest.fixture(scope="module")
def env():
    os.environ["KARO_SPEC_PATH"] = "/nonexistent"
    cfg = load_config()
    db = DB(URL)
    db.query("DROP SCHEMA IF EXISTS karo CASCADE; DROP SCHEMA IF EXISTS curriculum CASCADE; DROP SCHEMA IF EXISTS learner CASCADE;")
    db.migrate()
    return cfg, db


def _run(cfg, db, grades):
    provider = make_provider("mock", cfg)
    run_id = db.start_run("Mathematik", grades, "mock")
    pipe = Pipeline(cfg=cfg, provider=provider, db=db, run_id=run_id, log=lambda *_: None)
    stats = pipe.run("Mathematik", grades)
    db.finish_run(run_id, "finished", stats)
    return stats


def _status(db) -> dict[str, str]:
    return {r["id"]: r["status"] for r in db.query("SELECT id, status FROM curriculum.concepts")}


@needs_db
def test_single_grade_pulls_prerequisites_but_not_higher_grades(env):
    cfg, db = env
    _run(cfg, db, (6, 6))
    st = _status(db)
    assert st["MA.BRUECHE.ADD_UNGL"] == "approved"
    assert st["MA.ZAHLEN.ZR100"] == "approved"                # Voraussetzung aus Klasse 1–2 nachgezogen
    assert not any(k.startswith("MA.ALGEBRA") for k in st)    # Klasse 7+ nicht angefasst
    # Themenlandkarte deckt trotzdem die ganze Schulzeit ab (stabile IDs)
    assert db.one("SELECT grade_min, grade_max FROM curriculum.subjects WHERE code='MA'") == {"grade_min": 1, "grade_max": 13}   # Fachprofil Mathematik: 1–13


@needs_db
def test_visuals_are_stored_prerendered(env):
    _cfg, db = env
    expl = db.query("SELECT * FROM karo.visual_explanations WHERE concept_id='MA.BRUECHE.ADD_UNGL'")
    assert expl and expl[0]["misconception_id"] == "MA.BRUECHE.ADD_UNGL.F1"
    assert all(s["svg"].startswith("<svg") and s["caption"] for s in expl[0]["steps"])
    items = db.query("SELECT * FROM karo.items WHERE concept_id='MA.BRUECHE.ADD_UNGL' AND is_visual")
    assert items and items[0]["kind"] == "exit" and items[0]["visual_svg"].startswith("<svg")
    kgv = db.one("SELECT karo.concept_bundle('MA.TEILBARKEIT.KGV') AS b")["b"]
    assert kgv["visual_need"] == "none" and kgv["visual_explanations"] == []
    b = db.one("SELECT karo.concept_bundle('MA.BRUECHE.BEGRIFF') AS b")["b"]
    assert b["visual_need"] == "essential" and b["visual_explanations"][0]["steps"]


@needs_db
def test_range_run_extends_upwards(env):
    cfg, db = env
    _run(cfg, db, (7, 7))
    st = _status(db)
    assert st["MA.ALGEBRA.TERME"] == "approved"               # Ziel Klasse 7
    assert st["MA.ALGEBRA.GLEICHUNGEN"] == "structured"       # Ziel Klasse 8 – bleibt für später


@needs_db
def test_backfill_visuals_keeps_concept_visible(env):
    cfg, db = env
    db.query("DELETE FROM curriculum.visual_explanations WHERE concept_id='MA.ZAHLEN.ZR100'")
    db.query("UPDATE curriculum.concepts SET visuals=NULL, visual_need=NULL WHERE id='MA.ZAHLEN.ZR100'")
    stats = _run(cfg, db, (1, 2))
    assert stats.get("visualized") == 1
    c = db.concept("MA.ZAHLEN.ZR100")
    assert c["status"] == "approved" and c["visuals"] is not None
    assert db.query("SELECT 1 FROM karo.visual_explanations WHERE concept_id='MA.ZAHLEN.ZR100'")


# ---------------------------------------------------------------- Lesbarkeit (Beschriftungen passen)
_LESBAR = {
    "blatt": {"type": "flow_diagram", "title": "Licht trifft ein Blatt", "alt": "Licht wird zu Wärme und Energie",
              "direction": "horizontal",
              "nodes": [{"id": "a", "label": "Licht trifft grünes Blatt"}, {"id": "b", "label": "Wärme"},
                        {"id": "c", "label": "chemische Energie im Traubenzucker"}, {"id": "d", "label": "Wachstum"}],
              "edges": [{"source": "a", "target": "b", "label": "ein Teil"},
                        {"source": "a", "target": "c", "label": "wird"},
                        {"source": "c", "target": "d", "label": "später genutzt"}]},
    "alle": {"type": "flow_diagram", "title": "Alle Lebewesen atmen", "alt": "Menschen, Tiere und Pflanzen atmen",
             "direction": "horizontal",
             "nodes": [{"id": "o", "label": "Sauerstoff (O2) aus der Luft"}, {"id": "m", "label": "Mensch"},
                       {"id": "t", "label": "Tier"}, {"id": "p", "label": "Pflanze"},
                       {"id": "c", "label": "Kohlenstoffdioxid (CO2) in die Luft"}],
             "edges": [{"source": "o", "target": x, "label": "nimmt auf"} for x in "mtp"]
             + [{"source": x, "target": "c", "label": "gibt ab"} for x in "mtp"]},
    "tabelle": {"type": "table", "title": "Wer ist ein Lebewesen?", "alt": "Vergleich Hund, Pflanze, Stein",
                "headers": ["Merkmal", "Hund", "Pflanze", "Stein"],
                "rows": [["wächst", "ja", "ja", "nein"],
                         ["nimmt etwas auf – womit?", "Futter, mit dem Maul", "Wasser, mit den Wurzeln", "nichts"],
                         ["bekommt Nachkommen", "ja", "ja (Samen)", "nein"]],
                "highlight_cells": [[1, 2], [2, 2]]},
    "zyklus": {"type": "cycle", "title": "Der Wasserkreislauf", "alt": "Kreislauf des Wassers",
               "nodes": ["Wasser verdunstet über dem Meer", "Wolken entstehen", "Regen und Schnee fallen",
                         "Wasser fließt zurück ins Meer"],
               "arrow_labels": ["steigt auf", "kühlt ab", "versickert", "Sonne wärmt"],
               "center_label": "Sonne liefert Energie"},
}


@pytest.mark.parametrize("name", list(_LESBAR))
def test_labels_fit_and_do_not_overlap(name):
    import re
    from kcteam.visuals.render import layout_problems, render
    svg = render(_LESBAR[name])
    assert layout_problems(svg) == []
    # kein Kasten ragt über den Rand
    vw = float(re.search(r'viewBox="0 0 ([\d.]+)', svg)[1])
    for x, w in re.findall(r'<rect x="([-\d.]+)" y="[-\d.]+" width="([\d.]+)"', svg):
        assert float(x) >= -0.5 and float(x) + float(w) <= vw + 0.5
    # kein Text wird abgeschnitten: jedes Wort der Vorlage steht im Bild
    spec = _LESBAR[name]
    shown = [spec["title"], spec.get("center_label") or ""] + spec.get("arrow_labels", []) + spec.get("headers", [])
    shown += [n["label"] for n in spec.get("nodes", []) if isinstance(n, dict)] + \
        [n for n in spec.get("nodes", []) if isinstance(n, str)]
    shown += [e.get("label") or "" for e in spec.get("edges", [])] + [c for r in spec.get("rows", []) for c in r]
    text = "".join(re.findall(r">([^<]*)</text>", svg)).replace("-", "").replace(" ", "")
    for w in re.findall(r"\w+", " ".join(shown)):
        assert w in text, (w, text)


def test_audit_reports_overlaps():
    from kcteam.visuals.render import layout_problems
    bad = ('<svg viewBox="0 0 100 50"><text x="10" y="20" font-size="14" font-weight="normal" fill="#000" '
           'text-anchor="start" dominant-baseline="auto">Sehr langer Text läuft hinaus</text>'
           '<text x="12" y="22" font-size="14" font-weight="normal" fill="#000" text-anchor="start" '
           'dominant-baseline="auto">drüber</text></svg>')
    probs = layout_problems(bad)
    assert any("Bildrand" in p for p in probs) and any("überdecken" in p for p in probs)
