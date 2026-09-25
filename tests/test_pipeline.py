"""Ende-zu-Ende-Tests mit dem Mock-Provider gegen eine echte PostgreSQL.

    TEST_DATABASE_URL=postgresql://karo@localhost:5433/karo pytest -q
Die Tests löschen die Schemas curriculum und karo in dieser Datenbank!
"""
from __future__ import annotations

import json
import os

import pytest

from kcteam import review
from kcteam.agents import extract_json
from kcteam.config import load_config
from kcteam.db import DB
from kcteam.integrator import check_calibration, find_cycle
from kcteam.pipeline import Pipeline
from kcteam.providers import make_provider
from kcteam.schemas import Calibration

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")


# ---------------- ohne Datenbank ----------------
def test_extract_json_tolerates_fences_and_text():
    assert extract_json('Hier:\n```json\n{"a": {"b": 1}}\n```\nfertig') == {"a": {"b": 1}}
    assert extract_json('x {kaputt} {"ok": true}') == {"ok": True}


def test_find_cycle():
    assert find_cycle([("A", "B"), ("B", "C")]) is None
    cyc = find_cycle([("A", "B"), ("B", "C"), ("C", "A")])
    assert cyc and cyc[0] == cyc[-1]


def test_calibration_check_catches_level_errors():
    item = lambda lvl, g: {"prompt": "p", "solution": "s", "level": lvl, "grade": g}
    cal = Calibration.model_validate({
        "concept_id": "X", "levels": {"below": "a", "target": "b", "above": "c"},
        "can_do": {"below": ["a"], "target": ["b"], "above": ["c"]},
        "difficulty_parameters": {"max_nenner": 12},
        "anchor_items": [item("target", 6), item("target", 7)],           # 2. falsch
        "boundary_items": {"below": [item("below", 5)], "within": [item("target", 6)],
                           "above": [item("above", 6)]},                    # above muss > 6
    })
    errors = check_calibration({"id": "MA.B.C", "target_grade": 6}, cal)
    assert any("anchor_items[1]" in e for e in errors)
    assert any("boundary_items.above[0]" in e for e in errors)
    assert cal.concept_id == "MA.B.C"


# ---------------- mit Datenbank ----------------
@pytest.fixture(scope="module")
def env():
    os.environ["KARO_SPEC_PATH"] = "/nonexistent"
    cfg = load_config()
    cfg.pipeline["parallel_concepts"] = 3
    db = DB(URL)
    db.query("DROP SCHEMA IF EXISTS karo CASCADE; DROP SCHEMA IF EXISTS curriculum CASCADE; DROP SCHEMA IF EXISTS learner CASCADE;")
    db.migrate()
    return cfg, db


def _run(cfg, db, **kw):
    provider = make_provider("mock", cfg)
    run_id = db.start_run("Mathematik", (1, 10), "mock")
    pipe = Pipeline(cfg=cfg, provider=provider, db=db, run_id=run_id, log=lambda *_: None)
    stats = pipe.run("Mathematik", (1, 10), **kw)
    db.finish_run(run_id, "finished", stats)
    return stats


@needs_db
def test_full_run(env):
    cfg, db = env
    stats = _run(cfg, db)
    status = {r["id"]: r["status"] for r in db.query("SELECT id, status FROM curriculum.concepts")}
    assert status["MA.BRUECHE.ERWEITERN"] == "blocked"          # Wette -> 3x Veto
    assert status["MA.BRUECHE.ADD_UNGL"] == "approved"          # Marke nach Rückmeldung entfernt
    assert sum(s == "approved" for s in status.values()) == 9
    assert stats["blocked_for_human"] == 1
    assert stats["critic_issues"] == 1
    # Die Marke darf nirgends in Karos Sicht auftauchen
    assert not db.query("SELECT 1 FROM karo.items WHERE prompt ILIKE '%nutella%'")
    # Inspektor-Protokoll vollständig
    rej = db.query("""SELECT count(*) AS n FROM curriculum.reviews
                      WHERE reviewer='kinderrechts_inspektor' AND decision='rejected'""")[0]["n"]
    assert rej == stats["inspector_rejected"]


@needs_db
def test_karo_interface(env):
    _cfg, db = env
    path = db.query("SELECT * FROM karo.learning_path(ARRAY['MA.BRUECHE.ADD_UNGL'])")
    ids = [r["concept_id"] for r in path]
    assert ids[-1] == "MA.BRUECHE.ADD_UNGL"                        # Ziel zuletzt
    assert ids.index("MA.ZAHLEN.ZR100") < ids.index("MA.BRUECHE.BEGRIFF")   # Grundlagen zuerst
    assert {r["concept_id"]: r["available"] for r in path}["MA.BRUECHE.ERWEITERN"] is False
    # was das Kind schon kann, wird samt Unterbau übersprungen
    short = db.query("SELECT concept_id FROM karo.learning_path(ARRAY['MA.BRUECHE.ADD_UNGL'], "
                     "ARRAY['MA.BRUECHE.BEGRIFF','MA.ZAHLEN.EINMALEINS'])")
    assert "MA.ZAHLEN.ZR100" not in [r["concept_id"] for r in short]
    bundle = db.one("SELECT karo.concept_bundle('MA.BRUECHE.ADD_UNGL') AS b")["b"]
    assert bundle["target_grade"] == 6 and bundle["misconceptions"]
    assert all(i["level"] == "target" and i["grade"] == 6 for i in bundle["items"] if i["kind"] == "exit")
    assert db.one("SELECT karo.concept_bundle('MA.BRUECHE.ERWEITERN') AS b")["b"] is None   # gesperrt = unsichtbar
    found = db.query("SELECT * FROM karo.find_concepts('MA', 6, 'Brüche')")
    assert "MA.BRUECHE.ADD_UNGL" in [r["concept_id"] for r in found]


@needs_db
def test_rerun_is_idempotent(env):
    cfg, db = env
    before = db.one("SELECT count(*) AS n FROM curriculum.agent_calls")["n"]
    stats = _run(cfg, db)
    after = db.one("SELECT count(*) AS n FROM curriculum.agent_calls")["n"]
    assert after == before, f"zweiter Lauf hat {after - before} Aufrufe gemacht: {stats}"


@needs_db
def test_human_overrides_inspector(env):
    cfg, db = env
    q = [r for r in review.list_open(db) if r["entity_id"] == "MA.BRUECHE.ERWEITERN"][0]
    review.approve(db, q["id"], "Wettkontext im Unterricht besprochen – ok", "Fachkraft")
    assert db.concept("MA.BRUECHE.ERWEITERN")["status"] == "diagnosed"
    _run(cfg, db)   # Schlussprüfung respektiert die menschliche Freigabe
    assert db.concept("MA.BRUECHE.ERWEITERN")["status"] == "approved"
    path = db.query("SELECT available FROM karo.learning_path(ARRAY['MA.BRUECHE.ADD_UNGL'])")
    assert all(r["available"] for r in path)
    assert db.query("SELECT 1 FROM curriculum.reviews WHERE reviewer='human' AND decision='override_approve'")


@needs_db
def test_retry_with_human_note(env):
    cfg, db = env
    db.enqueue_human("concept", "MA.ALGEBRA.TERME", "final", "Testeintrag")
    q = [r for r in review.list_open(db) if r["entity_id"] == "MA.ALGEBRA.TERME"][0]
    review.retry(db, q["id"], "Bitte Beschreibung kindgerechter formulieren")
    c = db.concept("MA.ALGEBRA.TERME")
    assert c["status"] == "approved"          # bleibt für Karo sichtbar, während nachgearbeitet wird
    fbd = json.loads(c["pending_feedback"])
    assert fbd["rework"] and fbd["all"].startswith("Hinweis einer pädagogischen Fachkraft")
    stats = _run(cfg, db)
    c = db.concept("MA.ALGEBRA.TERME")
    assert c["status"] == "approved" and c["pending_feedback"] is None
    assert stats.get("reworked", 0) >= 1
    calls = db.query("SELECT role FROM curriculum.agent_calls WHERE entity_id='MA.ALGEBRA.TERME' "
                     "AND run_id=(SELECT id FROM curriculum.runs ORDER BY started_at DESC LIMIT 1)")
    assert {"niveau_kalibrierer", "diagnostiker"} <= {r["role"] for r in calls}


@needs_db
def test_export(env, tmp_path):
    _cfg, db = env
    files = review.export_subject(db, "MA", str(tmp_path))
    assert len(files) == 4
    data = json.loads(open([f for f in files if f.endswith("MA.BRUECHE.json")][0]).read())
    assert {c["id"] for c in data["concepts"]} >= {"MA.BRUECHE.ADD_UNGL", "MA.BRUECHE.ERWEITERN"}


@needs_db
def test_reader_role_cannot_see_drafts(env):
    _cfg, db = env
    with db.tx() as cur:
        cur.execute("SET LOCAL ROLE karo_reader")
        cur.execute("SELECT count(*) AS n FROM karo.concepts")
        assert cur.fetchone()["n"] > 0
        cur.execute("SELECT count(*) AS n FROM karo.learning_path(ARRAY['MA.BRUECHE.ADD_UNGL'])")
        assert cur.fetchone()["n"] > 0
    with pytest.raises(Exception, match="permission denied"):
        with db.tx() as cur:
            cur.execute("SET LOCAL ROLE karo_reader")
            cur.execute("SELECT count(*) FROM curriculum.concepts")
