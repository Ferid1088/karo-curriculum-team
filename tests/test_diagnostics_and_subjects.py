"""Fachprofile (alle Fächer 1–13) und Diagnostik für jedes Ergebnis und jedes Niveau."""
from __future__ import annotations

import json
import os

import pytest
from psycopg.types.json import Jsonb

from kcteam import review
from kcteam.agents import ROLES
from kcteam.config import load_config
from kcteam.db import DB
from kcteam.pipeline import Pipeline
from kcteam.providers import make_provider
from kcteam.simulate import run_child
from kcteam.subjects import SubjectTeam, load_all, resolve

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")


# ============================================================ Fachprofile
def test_all_subjects_have_complete_teams():
    subjects, families = load_all()
    assert len(subjects) >= 30
    codes = [s.code for s in subjects.values()]
    assert len(codes) == len(set(codes)), "Fachkürzel müssen eindeutig sein"
    for fam in families.values():
        assert set(ROLES) <= set(fam.roles), f"{fam.id}: nicht alle Rollen haben Anweisungen"
        if fam.id != "allgemein":
            assert fam.example.strip(), f"{fam.id}: Beispiel fehlt"
            assert fam.answer_types and fam.visual_types
    for s in subjects.values():
        assert s.family in families
        assert 1 <= s.grades[0] <= s.grades[1] <= 13
        team = SubjectTeam.for_subject(s.name)
        for role in ROLES:
            assert s.name in team.section(role)


def test_aliases_are_unambiguous():
    subjects, _ = load_all()
    seen = {}
    for s in subjects.values():
        for label in [s.name, s.id, *s.aliases]:
            hit = resolve(label)
            assert hit is not None and hit.id == s.id, f"'{label}' zeigt auf {hit and hit.id} statt {s.id}"
            seen[label] = s.id


@pytest.mark.parametrize("given,expected", [
    ("Bio", "Biologie"), ("erdkunde", "Erdkunde"), ("Geografie", "Erdkunde"), ("Gemeinschaftskunde", "Politik"),
    ("HSU", "Sachunterricht"), ("Werte und Normen", "Ethik"), ("kath. Religion", "Katholische Religion"),
    ("Französisch", "Französisch"), ("NaWi", "Naturwissenschaften"), ("Mathe", "Mathematik"),
])
def test_resolve_common_names(given, expected):
    assert resolve(given).name == expected


def test_team_specifics():
    es = SubjectTeam.for_subject("Spanisch")
    assert es.family.level_axis == "learning_year" and "Lernjahr" in es.section("fachdidaktiker")
    assert SubjectTeam.for_subject("Sachunterricht").check_grades((7, 7)) is not None
    assert SubjectTeam.for_subject("Physik").check_grades((6, 8)) is None
    bio = SubjectTeam.for_subject("Biologie")
    assert "Sexualerziehung" in bio.section("kinderrechts_inspektor")
    assert "Beutelsbacher" in SubjectTeam.for_subject("Politik").section("kinderrechts_inspektor")
    unknown = SubjectTeam.for_subject("Klingonisch")
    assert unknown.generic and unknown.family.id == "allgemein"


# ============================================================ Datenbank vorbereiten
@pytest.fixture(scope="module")
def env():
    os.environ["KARO_SPEC_PATH"] = "/nonexistent"
    cfg = load_config()
    db = DB(URL)
    # Einziger erlaubter Weg zum Leeren: er prüft Umgebung, tatsächlichen
    # Datenbanknamen, die Selbstauskunft der Datenbank und den Kill-Switch.
    from tools.reset_test_db import reset_schemas
    reset_schemas(db, mit_backup=False)

    def run(subject, grades):
        run_id = db.start_run(subject, grades, "mock")
        pipe = Pipeline(cfg=cfg, provider=make_provider("mock", cfg), db=db, run_id=run_id, log=lambda *_: None)
        return pipe.run(subject, grades)

    run("Mathematik", (1, 10))
    q = [r for r in review.list_open(db) if r["entity_id"] == "MA.BRUECHE.ERWEITERN"][0]
    review.approve(db, q["id"], "geprüft", "test")          # Kette vollständig machen
    run("Mathematik", (1, 10))
    return cfg, db, run


def _child(db, profile, targets=("MA.BRUECHE.ADD_UNGL",), learner=None, known=None):
    return run_child(db, learner=learner or f"t-{profile}-{os.urandom(4).hex()}", subject_code="MA", grade=6,
                     targets=list(targets), profile=profile, known=known)


def _check(db, item, answer):
    return db.one("SELECT * FROM karo.check_answer(%s, %s)", (item, Jsonb(answer)))


@needs_db
def test_data_is_diagnostic_ready(env):
    _cfg, db, _ = env
    rows = db.query("SELECT * FROM karo.diagnostic_readiness WHERE subject_code='MA'")
    assert rows and all(r["ready"] for r in rows), [r["concept_id"] for r in rows if not r["ready"]]


@needs_db
def test_check_answer_all_outcomes(env):
    _cfg, db, _ = env
    it = "MA.BRUECHE.ADD_UNGL.F1.DIAG"                       # 1/2 + 1/3 = 5/6, Distraktor 2/5 -> F1
    assert _check(db, it, "5/6")["outcome"] == "correct"
    assert _check(db, it, "10/12")["outcome"] == "correct"    # gleichwertiger Bruch
    assert _check(db, it, {"value": " 5 / 6 "})["outcome"] == "correct"
    assert _check(db, it, "0,8333")["outcome"] == "incorrect" # Dezimal nicht erlaubt
    mis = _check(db, it, "2/5")
    assert mis["outcome"] == "misconception" and mis["misconception_id"] == "MA.BRUECHE.ADD_UNGL.F1"
    assert _check(db, it, "4/10")["outcome"] == "misconception"   # gleichwertig zum Distraktor
    assert _check(db, it, "")["outcome"] == "skipped"
    assert _check(db, it, None)["outcome"] == "skipped"
    assert _check(db, it, "7/9")["outcome"] == "incorrect"
    num = "MA.BRUECHE.ADD_UNGL.DIAG_2"                        # 3 · 4 = 12
    assert _check(db, num, "12")["outcome"] == "correct"
    assert _check(db, num, "12 Stück")["outcome"] == "correct"
    choice = "MA.BRUECHE.ADD_UNGL.DIAG_1"
    assert _check(db, choice, 0)["outcome"] == "correct"
    assert _check(db, choice, "ein Viertel")["outcome"] == "correct"   # auch als Optionstext
    assert _check(db, choice, 1)["outcome"] == "misconception"


@needs_db
def test_child_view_hides_solutions(env):
    _cfg, db, _ = env
    item = db.one("SELECT karo.item_for_child('MA.BRUECHE.ADD_UNGL.DIAG_1') AS i")["i"]
    text = json.dumps(item)
    assert item["choices"] and "correct" not in text and "misconception" not in text and "solution" not in text


@needs_db
def test_strong_child_is_done_quickly_and_gets_challenge(env):
    _cfg, db, _ = env
    out = _child(db, "strong")
    res = out["result"]
    assert res["targets_mastered"] and res["plan"] == []
    assert len(out["asked"]) == 2                              # zwei richtige Ziel-Aufgaben genügen
    assert res["challenge_items"], "starke Kinder bekommen Aufgaben über dem Ziel"


@needs_db
def test_weak_child_goes_down_to_the_very_beginning(env):
    _cfg, db, _ = env
    res = _child(db, "weak")["result"]
    ids = [p["concept_id"] for p in res["plan"]]
    assert not res["targets_mastered"]
    assert ids[0] == "MA.ZAHLEN.ZR100" and ids[-1] == "MA.BRUECHE.ADD_UNGL"   # Lernreihenfolge
    zr = res["plan"][0]
    assert zr["entry_point"] and zr["below_curriculum_floor"]
    assert "MA.ZAHLEN.ZR100" in res["entry_points"]


@needs_db
def test_misconception_is_detected_and_explained(env):
    _cfg, db, _ = env
    out = _child(db, "misconception")
    plan = out["result"]["plan"]
    assert len(out["asked"]) == 1 and out["asked"][0]["outcome"] == "misconception"
    target = [p for p in plan if p["concept_id"] == "MA.BRUECHE.ADD_UNGL"][0]
    assert target["state"] == "misconception"
    assert target["misconceptions"] == ["MA.BRUECHE.ADD_UNGL.F1"]
    assert target["explanations"] == ["MA.BRUECHE.ADD_UNGL.V1"]   # passende Bildfolge zur Fehlvorstellung


@needs_db
def test_specific_gap_is_found(env):
    _cfg, db, _ = env
    res = _child(db, "gap:MA.TEILBARKEIT.KGV")["result"]
    ids = [p["concept_id"] for p in res["plan"]]
    assert ids == ["MA.TEILBARKEIT.KGV", "MA.BRUECHE.ADD_UNGL"]
    assert res["entry_points"] == ["MA.TEILBARKEIT.KGV"]
    assert "MA.BRUECHE.GLEICHN_ADD" in res["mastered"]


@needs_db
def test_skipping_child_is_handled(env):
    _cfg, db, _ = env
    res = _child(db, "skipper")["result"]
    assert not res["targets_mastered"] and res["plan"][0]["concept_id"] == "MA.ZAHLEN.ZR100"


@needs_db
def test_previous_mastery_is_reused(env):
    _cfg, db, _ = env
    _child(db, "strong", learner="kind-wiederkehr")
    out = _child(db, "weak", learner="kind-wiederkehr")         # zweite Sitzung: schon beherrscht
    assert out["asked"] == [] and out["result"]["targets_mastered"]


@needs_db
def test_free_text_is_asked_last_and_review_completes(env):
    _cfg, db, _ = env
    it = "MA.BRUECHE.GLEICHN_ADD.DIAG_2"
    db.query("""UPDATE curriculum.items SET auto_checkable=false,
                answer='{"type":"free_text","rubric":[{"criterion":"Rechnung","points":2}],"pass_points":1,
                         "sample_answer":"12"}' WHERE id=%s""", (it,))
    out = _child(db, "strong", targets=("MA.BRUECHE.GLEICHN_ADD",))
    assert out["asked"][-1]["item"] == it and out["asked"][-1]["outcome"] == "needs_review"   # automatisch prüfbare zuerst
    res = out["result"]
    target = [p for p in res["plan"] if p["concept_id"] == "MA.BRUECHE.GLEICHN_ADD"][0]
    assert target["state"] == "pending"                          # offener Freitext zählt nicht als Scheitern
    assert not any(p["concept_id"] == "MA.BRUECHE.BEGRIFF" for p in res["plan"])   # kein Abstieg wegen Freitext
    after = db.one("SELECT karo.review_response(%s, 'correct', 1) AS r", (res["needs_review"][0],))["r"]
    assert "action" not in after and "ask" not in json.dumps(after)   # nie eine Kinderaufgabe
    assert after["result"]["targets_mastered"] and after["needs_followup"] is False
    # skip auf Freitext = übersprungen, nicht needs_review
    assert _check(db, it, None)["outcome"] == "skipped"
    db.query("""UPDATE curriculum.items SET auto_checkable=true, answer='{"type":"number","value":12}' WHERE id=%s""",
             (it,))


@needs_db
def test_duplicate_and_foreign_answers_are_rejected(env):
    _cfg, db, _ = env
    sid = db.one("SELECT karo.start_diagnosis('dup-kind','MA',6,ARRAY['MA.BRUECHE.ADD_UNGL']) AS s")["s"]
    step = db.one("SELECT karo.next_step(%s) AS s", (sid,))["s"]
    item = step["item"]["id"]
    again = db.one("SELECT karo.next_step(%s) AS s", (sid,))["s"]
    assert again["item"]["id"] == item                            # Neuladen liefert dieselbe Aufgabe
    first = db.one("SELECT karo.record_response(%s,%s,'\"12\"') AS r", (sid, item))["r"]
    dup = db.one("SELECT karo.record_response(%s,%s,'\"12\"') AS r", (sid, item))["r"]
    assert dup["duplicate"] and dup["outcome"] == first["outcome"]
    assert db.one("SELECT count(*) AS n FROM learner.responses WHERE session_id=%s", (sid,))["n"] == 1
    with pytest.raises(Exception, match="nicht gestellt"):
        db.query("SELECT karo.record_response(%s,'MA.ZAHLEN.ZR100.DIAG_1','\"x\"')", (sid,))


@needs_db
def test_bad_inputs_are_handled(env):
    _cfg, db, _ = env
    with pytest.raises(Exception, match="Zielkonzept"):
        db.query("SELECT karo.start_diagnosis('x','MA',6,ARRAY[]::text[])")
    with pytest.raises(Exception, match="anderen Fach"):
        db.query("SELECT karo.start_diagnosis('x','DE',6,ARRAY['MA.BRUECHE.ADD_UNGL'])")
    res = run_child(db, learner="null-known", subject_code="MA", grade=6, targets=["MA.BRUECHE.ADD_UNGL"],
                    profile="strong", known=[None])
    assert res["result"]["targets_mastered"] and len(res["asked"]) == 2   # NULL im Vorwissen stört nicht
    num = "MA.BRUECHE.ADD_UNGL.DIAG_2"                                    # 3 · 4 = 12
    for bad in ["12 oder 7", "12-5", "12:3", "1.5e3"]:
        assert _check(db, num, bad)["outcome"] == "incorrect", bad
    for good in ["12", " +12 ", "12,0", "12 Stück"]:
        assert _check(db, num, good)["outcome"] == "correct", good
    assert _check(db, "MA.BRUECHE.ADD_UNGL.DIAG_1", ["ein Viertel"])["outcome"] == "correct"   # kein Absturz
    assert _check(db, "MA.BRUECHE.ADD_UNGL.DIAG_1", {"index": 0})["outcome"] == "correct"


@needs_db
def test_target_that_is_also_a_prerequisite_is_tested(env):
    _cfg, db, _ = env
    out = _child(db, "strong", targets=("MA.BRUECHE.ADD_UNGL", "MA.BRUECHE.GLEICHN_ADD"))
    res = out["result"]
    assert res["targets_mastered"]
    # GLEICHN_ADD ist Voraussetzung von ADD_UNGL und damit schon impliziert – es darf nicht als „nicht geprüft“ fehlen
    assert "MA.BRUECHE.GLEICHN_ADD" not in res["not_assessed"]


@needs_db
def test_unavailable_prerequisite_does_not_drill_strong_child(env):
    _cfg, db, _ = env
    db.query("UPDATE curriculum.concepts SET status='blocked' WHERE id='MA.BRUECHE.ERWEITERN'")
    try:
        out = _child(db, "strong")
        assert len(out["asked"]) == 2 and out["result"]["targets_mastered"]
        assert out["result"]["plan"] == [] and out["result"]["not_assessed"] == []
        path = db.query("SELECT * FROM karo.learning_path(ARRAY['MA.BRUECHE.ADD_UNGL'])")
        blocked = [p for p in path if p["concept_id"] == "MA.BRUECHE.ERWEITERN"][0]
        assert blocked["title"] is None and not blocked["available"]      # kein Titel nicht freigegebener Inhalte
        assert db.one("SELECT karo.item_for_child('MA.BRUECHE.ERWEITERN.DIAG_1') AS i")["i"] is None
    finally:
        db.query("UPDATE curriculum.concepts SET status='approved' WHERE id='MA.BRUECHE.ERWEITERN'")


@needs_db
def test_mastery_evidence_does_not_grow_on_repeated_calls(env):
    _cfg, db, _ = env
    out = _child(db, "strong", learner="evidence-kind")
    sid = out["session"]
    ev = lambda: db.one("""SELECT sum(evidence) AS n FROM learner.mastery m JOIN learner.learners l ON l.id=m.learner_id
                           WHERE l.external_ref='evidence-kind'""")["n"]
    before = ev()
    for _ in range(3):
        db.one("SELECT karo.next_step(%s) AS s", (sid,))
    assert ev() == before


@needs_db
def test_roles_and_erasure(env):
    _cfg, db, _ = env
    with db.tx() as cur:                      # App darf diagnostizieren ...
        cur.execute("SET LOCAL ROLE karo_app")
        cur.execute("SELECT karo.start_diagnosis('rolle-kind','MA',6,ARRAY['MA.BRUECHE.ADD_UNGL']) AS s")
        sid = cur.fetchone()["s"]
        cur.execute("SELECT karo.next_step(%s) AS s", (sid,))
    for role, sql in [("karo_app", "SELECT solution FROM karo.items LIMIT 1"),          # ... aber keine Lösungen sehen
                      ("karo_app", "SELECT * FROM karo.check_answer('MA.BRUECHE.ADD_UNGL.DIAG_2','\"12\"')"),
                      ("karo_app", "SELECT karo.review_response(1,'correct')"),
                      ("karo_reader", "SELECT karo.record_response('%s','x','\"1\"')" % sid),
                      ("karo_reader", "SELECT * FROM learner.responses")]:
        with pytest.raises(Exception, match="permission denied"):
            with db.tx() as cur:
                cur.execute(f"SET LOCAL ROLE {role}")
                cur.execute(sql)
    with db.tx() as cur:
        cur.execute("SET LOCAL ROLE karo_app")
        cur.execute("SELECT karo.forget_learner('rolle-kind') AS n")
        assert cur.fetchone()["n"] == 1
    assert not db.query("SELECT 1 FROM learner.sessions WHERE id=%s", (sid,))


@needs_db
def test_learning_path_scales(env):
    import time as _t
    _cfg, db, _ = env
    with db.tx() as cur:   # 60 Konzepte, 4 breit, 15 tief, dicht verbunden – wird am Ende zurückgerollt
        cur.execute("INSERT INTO curriculum.subjects(code,name,grade_min,grade_max) VALUES ('TT','Testfach',1,13)")
        cur.execute("INSERT INTO curriculum.topic_blocks(id,subject_code,title,grade_min,grade_max,typical_grade) "
                    "VALUES ('TT.B','TT','B',1,13,5)")
        ids = [[f"TT.B.L{d}_{w}" for w in range(4)] for d in range(15)]
        for d, layer in enumerate(ids):
            for cid in layer:
                cur.execute("INSERT INTO curriculum.concepts(id,block_id,subject_code,title,first_contact_grade,"
                            "target_grade,status) VALUES (%s,'TT.B','TT',%s,1,1,'approved')", (cid, cid))
                if d:
                    for pre in ids[d - 1]:
                        cur.execute("INSERT INTO curriculum.concept_prerequisites VALUES (%s,%s)", (cid, pre))
        t0 = _t.time()
        cur.execute("SELECT count(*) AS n FROM karo.learning_path(ARRAY[%s])", (ids[-1][0],))
        n = cur.fetchone()["n"]
        elapsed = _t.time() - t0
        cur.connection.rollback()
    assert n == 57 and elapsed < 2, (n, elapsed)


@needs_db
def test_other_subject_uses_its_own_team(env):
    _cfg, db, run = env
    run("Bio", (5, 6))
    subj = db.one("SELECT * FROM curriculum.subjects WHERE code='BI'")
    assert subj["name"] == "Biologie" and subj["profile"] == "biologie"
    calls = db.query("SELECT DISTINCT role FROM curriculum.agent_calls")
    assert {"visual_didaktiker", "kinderrechts_inspektor"} <= {c["role"] for c in calls}
    assert db.query("SELECT 1 FROM curriculum.concepts WHERE subject_code='BI' AND status='approved'")
