"""Kuratierte vertikale Slices: Säen, Auflösen, Lernpfad, Export, Lückenbericht.

Echte PostgreSQL-Testdatenbank, echter HTTP-Testclient, Mock-Provider —
kein externer Dienst, keine nachgebauten DB-Zugriffe.
"""
import json
import os
from pathlib import Path

import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")
KARO = json.loads((Path(__file__).parent / "fixtures" / "karo_format.json").read_text(encoding="utf-8"))

SLICES_FAECHER = {"mathematik", "deutsch", "englisch", "biologie", "physik", "chemie"}


@pytest.fixture(scope="module")
def env():
    from fastapi.testclient import TestClient

    from kcteam.api import add_client, create_app
    from kcteam.config import load_config
    from kcteam.db import DB
    from kcteam.ondemand import CurriculumAgent
    from kcteam.providers import make_provider
    from kcteam.slices import seed_slices
    os.environ["KARO_SPEC_PATH"] = "/nonexistent"
    cfg = load_config()
    cfg.pipeline["parallel_concepts"] = 3
    db = DB(URL)
    from tools.reset_test_db import reset_schemas
    reset_schemas(db, mit_backup=False)
    gezaehlt = seed_slices(db)
    prov = make_provider("mock", cfg)
    _, key = add_client(db, "karo-test", "familie")
    agent = CurriculumAgent(cfg=cfg, provider=prov, db=db, log=lambda *_: None)
    api = TestClient(create_app(db, webhooks=False))
    return {"db": db, "api": api, "agent": agent, "key": key, "gezaehlt": gezaehlt}


def h(key):
    return {"Authorization": f"Bearer {key}"}


def test_sechs_faecher_alle_konzepte_freigegeben(env):
    from karo_contract import slices
    db = env["db"]
    gezaehlt = env["gezaehlt"]
    assert set(gezaehlt) == SLICES_FAECHER
    assert sum(len(v) for v in gezaehlt.values()) == len(slices.konzepte())
    rows = db.query("""SELECT subject_code, status, count(*) AS n
                         FROM curriculum.concepts GROUP BY 1, 2""")
    for code in ("MA", "DE", "EN", "BI", "PH", "CH"):
        anteile = {r["status"]: r["n"] for r in rows if r["subject_code"] == code}
        assert anteile.get("approved") == sum(anteile.values()), \
            f"{code}: nicht alle Konzepte freigegeben: {anteile}"
    ohne_draft = db.query("SELECT id FROM curriculum.concepts "
                          "WHERE status='approved' AND lesson_draft IS NULL")
    assert ohne_draft == []


def test_saat_ist_idempotent(env):
    from kcteam.slices import seed_slices
    db = env["db"]
    vorher = db.one("SELECT count(*) AS n FROM curriculum.concepts")["n"]
    kanten = db.one("SELECT count(*) AS n FROM curriculum.concept_prerequisites")["n"]
    seed_slices(db)
    assert db.one("SELECT count(*) AS n FROM curriculum.concepts")["n"] == vorher
    assert db.one("SELECT count(*) AS n FROM curriculum.concept_prerequisites")["n"] == kanten


@pytest.mark.parametrize("fach,thema,konzept,klasse", [
    ("Mathematik", "Volumen eines Quaders", "MA.GEO.QUADERVOLUMEN", 5),
    ("Deutsch", "Subjekt und Prädikat bestimmen", "DE.SATZGLIEDER.SUBJEKT_PRAEDIKAT", 4),
    ("Englisch", "Simple Past", "EN.GRAMMAR.SIMPLE_PAST", 6),
    ("Biologie", "Fotosynthese", "BI.PFLANZEN.FOTOSYNTHESE", 6),
    ("Physik", "Dichte", "PH.GROESSEN.DICHTE", 6),
    ("Chemie", "chemische Reaktion", "CH.REAKTION.BEGRIFF", 7),
])
def test_resolve_findet_slice_thema(env, fach, thema, konzept, klasse):
    r = env["api"].post("/v1/resolve", json={
        "subject": fach, "grade": klasse, "topic": thema}, headers=h(env["key"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "found", r.json()
    assert r.json()["concepts"][0]["concept_id"] == konzept


def test_lernpfad_reicht_bis_level_0(env):
    """Der Pfad zum Zielkonzept endet an einem freigegebenen Fuß — überall."""
    from karo_contract import slices
    api, key = env["api"], env["key"]
    for konzept in slices.konzepte():
        r = api.post("/v1/learning-path", json={
            "targets": [konzept["id"]], "mastered": []}, headers=h(key))
        assert r.status_code == 200, r.text
        pfad = r.json()["path"]
        assert pfad and pfad[-1]["concept_id"] == konzept["id"]
        assert all(e["available"] for e in pfad), \
            f"{konzept['id']}: Pfad enthält nicht freigegebene Konzepte"
        # Der erste Eintrag (tiefste Voraussetzung) ist ein Fuß ohne
        # eigene Voraussetzungen — sonst gäbe es tieferes Material.
        fuss = pfad[0]["concept_id"]
        if fuss == konzept["id"]:
            continue                          # Ziel ist selbst Level 0
        tiefer = api.post("/v1/learning-path", json={
            "targets": [fuss], "mastered": []}, headers=h(key)).json()["path"]
        assert len(tiefer) == 1, f"{fuss} ist kein Fuß — der Pfad geht tiefer"


def test_export_liefert_die_verfasste_lektion(env):
    """Kuratierte Lektion über denselben Exportweg wie generiertes Material."""
    api, key, agent = env["api"], env["key"], env["agent"]
    r = api.post("/v1/lessons", json={
        "subject": "Biologie", "grade": 5, "format": KARO,
        "concept_id": "BI.PFLANZEN.FOTOSYNTHESE", "topic": "Fotosynthese"},
        headers=h(key))
    assert r.status_code == 202, r.text
    eid = r.json()["export_id"]
    assert agent.process_export()
    r = api.get(f"/v1/lessons/{eid}", headers=h(key))
    assert r.status_code == 200 and r.json()["status"] == "ready", r.json()
    lektion = r.json()["lesson"]
    # Karos eigene Prüfung nimmt sie an — und die Begriffs-Rubrik ist da.
    import karo_contract
    geprueft = karo_contract.pruefe_lektion(lektion)
    rubriken = [a["rubrik"] for f in geprueft["fehlertypen"]
                for a in f["aufgaben"].values() if a.get("rubrik")]
    assert rubriken, "keine Rubrik in der gelieferten Lektion"
    assert any(a.get("antwort_art") == "begriffe"
               for f in geprueft["fehlertypen"] for a in f["aufgaben"].values())


def test_gap_report_ist_sauber(env):
    """Nach der Saat meldet der Lückenbericht nichts — sonst fehlt Material."""
    from kcteam.gap_report import curriculum_gap_report
    bericht = curriculum_gap_report(env["db"])
    assert bericht["summary"]["concepts"] >= 25
    assert bericht["graph_gaps"] == [], bericht["graph_gaps"]
    assert bericht["concept_gaps"] == [], \
        [(k["concept_id"], k["gaps"]) for k in bericht["concept_gaps"]]
    assert bericht["summary"]["ok"]


def test_gap_report_findet_eine_gelegte_luecke(env):
    """Der Bericht darf kein Feuermelder sein, der nie piept."""
    from kcteam.gap_report import curriculum_gap_report
    db = env["db"]
    db.query("""INSERT INTO curriculum.concepts(id, block_id, subject_code, title,
                    description, first_contact_grade, target_grade, status)
                VALUES ('BI.PFLANZEN.WURZEL', 'BI.PFLANZEN', 'BI', 'Wurzeln',
                        'test', 3, 4, 'structured')""")
    db.query("""INSERT INTO curriculum.concept_prerequisites VALUES
                    ('BI.PFLANZEN.WURZEL', 'BI.PFLANZEN.GIBTS_NICHT'),
                    ('BI.PFLANZEN.FOTOSYNTHESE', 'BI.PFLANZEN.WURZEL')
                ON CONFLICT DO NOTHING""")
    bericht = curriculum_gap_report(db)
    arten = {g["type"] for g in bericht["graph_gaps"]}
    assert "missing_prerequisite" in arten
    assert "unapproved_prerequisite" in arten
    wurzel = next(k for k in bericht["concept_gaps"]
                  if k["concept_id"] == "BI.PFLANZEN.WURZEL")
    assert any("nicht freigegeben" in l for l in wurzel["gaps"])
    db.query("DELETE FROM curriculum.concept_prerequisites "
             "WHERE concept_id IN ('BI.PFLANZEN.WURZEL','BI.PFLANZEN.FOTOSYNTHESE') "
             "AND prerequisite_id IN ('BI.PFLANZEN.GIBTS_NICHT','BI.PFLANZEN.WURZEL')")
    db.query("DELETE FROM curriculum.concepts WHERE id='BI.PFLANZEN.WURZEL'")
    assert curriculum_gap_report(db)["summary"]["ok"]
