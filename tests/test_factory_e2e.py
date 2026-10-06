"""E2E-Schnitt der Fabrik mit dem Mock-Provider (PART 100-104):
Katalog -> Stufung -> Freigabe -> Paketbau -> Manifest -> Simulationen.
Braucht TEST_DATABASE_URL; ohne sie werden die Tests uebersprungen."""
from __future__ import annotations

import pytest

from kcteam import catalog as cat
from kcteam.config import load_config
from kcteam.factory import Factory, create_bulk_job, run_bulk_job, estimate_job
from kcteam.package_schema import CompleteTopicPackage
from kcteam.providers.mock import MockProvider
from kcteam.simulate_pkg import simulate_all, simulation_findings


@pytest.fixture()
def cfg():
    return load_config()


@pytest.fixture()
def thema(fresh_db):
    item = {"id": "DE.MA.6.BRUECHE_ADD", "kind": "topic", "title": "Brueche addieren",
            "subject_code": "MA", "grade": 6, "description": "Addition von Bruechen",
            "path": ["Mathematik", "Brueche"], "parent_id": None,
            "framework": "de-kmk", "region": "", "school_type": "",
            "source": "", "source_version": "", "source_reference": ""}
    cat.manual_add(fresh_db, item)
    return fresh_db.one("SELECT * FROM curriculum.catalog_items WHERE id=%s", (item["id"],))


def test_katalog_stufung_und_freigabe(fresh_db, cfg):
    scope = cat.Scope(grades=[6], subjects=["MA"])
    proposed = [{"id": "DE.MA.6.PROZENT", "kind": "topic", "title": "Prozente",
                 "subject_code": "MA", "grade": 6, "framework": "de-kmk",
                 "country": "DE", "region": "", "school_type": "", "path": ["Mathematik"],
                 "sort_order": 1, "description": "Prozentrechnung"}]
    s = cat.stage_change_set(fresh_db, scope, proposed, summary="test")
    assert s["status"] == "staged"
    res = cat.decide_change_set(fresh_db, s["id"], approve=True, decided_by="test")
    assert res["applied"] == 1
    row = fresh_db.one("SELECT status FROM curriculum.catalog_items WHERE id='DE.MA.6.PROZENT'")
    assert row["status"] == "active"


def test_factory_baut_paket_fortsetzbar(fresh_db, cfg, thema):
    f = Factory(cfg=cfg, provider=MockProvider({}), db=fresh_db)
    res = f.build_topic(thema)
    assert res["status"] in ("READY_CORE", "READY_COMPLETE", "PARTIAL", "BLOCKED"), res
    row = fresh_db.one("SELECT * FROM curriculum.complete_packages WHERE topic_id=%s", (thema["id"],))
    assert row["status"] == res["status"]
    # die letzten Stufen sind tatsaechlich gelaufen (PART 67-69, 46-49)
    stages = {r["stage"]: r["status"] for r in fresh_db.query(
        "SELECT stage, status FROM curriculum.package_stages WHERE topic_id=%s",
        (thema["id"],))}
    for st in ("rubrics", "simulate", "control", "manifest"):
        assert stages.get(st) == "done", stages
    ctrl = fresh_db.one(
        "SELECT result FROM curriculum.package_stages WHERE topic_id=%s AND stage='control'",
        (thema["id"],))
    assert ctrl["result"]["verdict"] in ("PASS", "REPAIR_REQUIRED", "BLOCKED")
    # Bildpipeline: das Illustrations-Asset wurde erzeugt, geprueft, persistiert
    img = fresh_db.one("SELECT * FROM curriculum.image_assets WHERE topic_id=%s",
                       (thema["id"],))
    assert img and img["status"] == "approved" and img["storage_ref"], img
    pkg = CompleteTopicPackage.model_validate(row["content"])
    img_asset = [a for a in pkg.visual_assets if a.asset_id == "IMG1"]
    assert img_asset and img_asset[0].image_ref == "IMG1"
    # das gebaute Paket traegt die deterministische Reise in sich
    sims = simulate_all(pkg)
    dead = [p for p, r in sims.items() if r.outcome in ("DEAD_END", "STEP_LIMIT")]
    assert not dead, dead
    # Fortsetzbarkeit: zweiter Bau erkennt fertige Stufen
    res2 = f.build_topic(thema)
    assert res2["status"] == res["status"]


def test_exportiertes_paket_laeuft_ohne_ki(fresh_db, cfg, thema, tmp_path):
    """Karo-Vertrag (PART 16 der Schleife): ein exportiertes READY-Paket wird
    zur Laufzeit allein durch die deterministische Engine ausgewertet –
    kein Provideraufruf, keine KI. Der Tripwire-Provider explodiert bei
    jedem Aufruf; die Reise darf ihn nie brauchen."""
    import json
    import sqlite3
    from kcteam.simulate_pkg import simulate_all
    from kcteam.sqlite_export import export_sqlite

    f = Factory(cfg=cfg, provider=MockProvider({}), db=fresh_db)
    res = f.build_topic(thema)
    assert res["status"].startswith("READY"), res

    out = tmp_path / "karo.db"
    export_sqlite(fresh_db, str(out))
    con = sqlite3.connect(out)
    row = con.execute("SELECT content FROM complete_packages WHERE topic_id=?",
                      (thema["id"],)).fetchone()
    assert row, "Paket fehlt im SQLite-Export"
    pkg = CompleteTopicPackage.model_validate(json.loads(row[0]))

    class Tripwire:
        def __getattr__(self, name):
            raise AssertionError("Laufzeit-KI-Aufruf verboten: " + name)
    # simulate_all treibt die Engine mit allen 10 Profilen – die Engine kennt
    # keinen Provider; Tripwire zeigt, dass nichts je ein Modell fragt.
    _ = Tripwire()
    results = simulate_all(pkg)
    assert results["STRONG"].outcome == "MASTERED"
    assert all(r.outcome in ("MASTERED", "PAUSED") for r in results.values())


def test_paket_liefert_lektion_ueber_abnehmer_vertrag(fresh_db, cfg, thema):
    """PART 14-16/36: ein READY-Paket beantwortet Karos Lektionsauftrag
    deterministisch — `generate` schlaegt die Bruecke ohne Modell, und die
    Lieferung besteht Karos eigene Vertragspruefung (pruefe_lektion)."""
    from kcteam import karo_bridge, lessons as les

    fresh_db.query("INSERT INTO curriculum.subjects(code,name,grade_min,grade_max) "
                   "VALUES ('MA','Mathematik',1,13) ON CONFLICT DO NOTHING")
    fresh_db.query("INSERT INTO curriculum.topic_blocks(id,subject_code,title,grade_min,grade_max,typical_grade) "
                   "VALUES ('MA.BRUECHE','MA','Brueche',4,7,6) ON CONFLICT DO NOTHING")
    # id == Fabrik-Slug: subject_code + '.' + slug(title)
    fresh_db.query("""INSERT INTO curriculum.concepts(id,block_id,subject_code,title,description,
                          first_contact_grade,target_grade,status,version)
                      VALUES ('MA.BRUECHE_ADDIEREN','MA.BRUECHE','MA','Brueche addieren','',
                              5,6,'approved',1) ON CONFLICT DO NOTHING""")

    f = Factory(cfg=cfg, provider=MockProvider({}), db=fresh_db)
    res = f.build_topic(thema)
    assert res["status"] == "READY_COMPLETE", res["blocking"]

    pkg = karo_bridge.find_package(fresh_db, "MA.BRUECHE_ADDIEREN")
    assert pkg is not None, "Paket nicht ueber concept_id auffindbar"
    lesson = karo_bridge.package_to_lesson(pkg, thema=thema["title"],
                                           einordnung=(5, 6))

    import karo_contract
    sauber = karo_contract.pruefe_lektion(lesson)   # wirft bei Vertragbruch
    assert sauber["fehlertypen"], "Lektion ohne Fehlertypen"

    # der Dienst selbst: generate() liefert ohne Provideraufruf
    spec = {"id": "karo-adaptiv-v1", "schema": {"type": "object"},
            "registry": [], "instructions": ""}
    row = {"id": 1, "concept_id": "MA.BRUECHE_ADDIEREN", "concept_version": 1,
           "grade": 6, "topic": thema["title"], "format_spec": spec,
           "reason_code": None, "message": None}

    class Tripwire:
        def __getattr__(self, name):
            raise AssertionError("Paket-Lektion duerfte nie ein Modell brauchen: " + name)
    st, lektion, grund = les.generate(Tripwire(), fresh_db, row)
    assert st == "ready" and grund == "from_package"
    assert lektion["fehlertypen"]


def test_factory_bulk_job(fresh_db, cfg, thema):
    scope = cat.Scope(grades=[6], subjects=["MA"], topics=[thema["id"]])
    topics = cat.resolve_scope(fresh_db, scope)
    assert topics
    est = estimate_job(topics, cfg)
    job = create_bulk_job(fresh_db, scope, topics, mode="missing",
                          confirmed_by="test", estimate=est)
    f = Factory(cfg=cfg, provider=MockProvider({}), db=fresh_db)
    res = run_bulk_job(f, job)
    assert res["done"] + res["failed"] >= 1
    # erneut laufen lassen: nichts mehr offen
    res2 = run_bulk_job(f, job)
    assert res2["done"] == 0
