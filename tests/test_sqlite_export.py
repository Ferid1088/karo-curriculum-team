"""Stufe 1: SQLite-Kopie für das Karo-MVP. Stufe 2: Verbindungshinweise für Supabase/Neon."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from kcteam.config import load_config
from kcteam.db import DB
from kcteam.pipeline import Pipeline
from kcteam.providers import make_provider
from kcteam.sqlite_export import auto_export, export_sqlite

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")


def test_connection_warnings_for_poolers():
    assert DB.connection_warnings("postgresql://karo:karo@postgres:5432/karo") == []
    w = DB.connection_warnings("postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:6543/postgres")
    assert any("6543" in x for x in w) and any("sslmode" in x for x in w)
    assert DB.connection_warnings("postgresql://u:p@ep-x-pooler.eu-central-1.aws.neon.tech/db?sslmode=require")
    assert DB.connection_warnings(
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require") == []


@pytest.fixture(scope="module")
def env():
    os.environ["KARO_SPEC_PATH"] = "/nonexistent"
    cfg = load_config()
    cfg.pipeline["parallel_concepts"] = 3
    db = DB(URL)
    db.query("DROP SCHEMA IF EXISTS karo CASCADE; DROP SCHEMA IF EXISTS curriculum CASCADE; "
             "DROP SCHEMA IF EXISTS learner CASCADE;")
    db.migrate()
    run_id = db.start_run("Mathematik", (1, 10), "mock")
    Pipeline(cfg=cfg, provider=make_provider("mock", cfg), db=db, run_id=run_id,
             log=lambda *_: None).run("Mathematik", (1, 10))
    return cfg, db


@needs_db
def test_export_contains_only_approved_content_and_works_like_karo_needs(env, tmp_path):
    _cfg, db = env
    out = tmp_path / "karo-data" / "curriculum.db"
    counts = export_sqlite(db, out)
    approved = {r["id"] for r in db.query("SELECT id FROM curriculum.concepts WHERE status='approved'")}
    assert counts["concepts"] == len(approved) > 0

    # so wie Karo sie einbindet
    karo = sqlite3.connect(":memory:")
    karo.execute(f"ATTACH DATABASE '{out}' AS cur")
    got = {r[0] for r in karo.execute("SELECT id FROM cur.concepts")}
    assert got == approved
    assert "MA.BRUECHE.ERWEITERN" not in got                       # gesperrt -> nicht in Karo
    # Kanten nur zwischen freigegebenen Konzepten, fehlende werden gezählt
    assert not karo.execute("""SELECT 1 FROM cur.concept_prerequisites p
                               LEFT JOIN cur.concepts c ON c.id = p.prerequisite_id WHERE c.id IS NULL""").fetchall()
    assert karo.execute("SELECT missing_prerequisites FROM cur.concepts WHERE id='MA.BRUECHE.ADD_UNGL'").fetchone()[0] >= 1
    # auswertbare Antworten und Fehlvorstellungen
    ans = karo.execute("""SELECT answer FROM cur.items WHERE concept_id='MA.BRUECHE.ADD_UNGL' AND kind='exit'
                          AND auto_checkable=1""").fetchall()
    assert ans and all(json.loads(a[0])["type"] for a in ans)
    assert karo.execute("SELECT count(*) FROM cur.misconceptions").fetchone()[0] > 0
    # Bild-Erklärungen mit fertigem SVG
    steps = json.loads(karo.execute("SELECT steps FROM cur.visual_explanations LIMIT 1").fetchone()[0])
    assert "<svg" in steps[0]["svg"]
    # Kind-Ansicht ohne Lösung
    cols = [d[1] for d in karo.execute("PRAGMA cur.table_info(items_for_child)")]
    assert "solution" not in cols and "answer" not in cols
    # Volltextsuche: Umlaute egal ("Bruche" findet "Brüche")
    hits = {r[0] for r in karo.execute("SELECT concept_id FROM cur.concept_search WHERE concept_search MATCH 'bruche'")}
    assert "MA.BRUECHE.ADD_UNGL" in hits
    # Lernpfad-Abfrage liegt in meta bereit
    sql = karo.execute("SELECT value FROM cur.meta WHERE key='learning_path_sql'").fetchone()[0]
    con = sqlite3.connect(out)
    path = con.execute(sql, {"targets": json.dumps(["MA.BRUECHE.ADD_UNGL"]),
                             "mastered": json.dumps([])}).fetchall()
    assert path[-1][0] == "MA.BRUECHE.ADD_UNGL" and len(path) >= 3
    # keine Kinderdaten, keine Aufträge, kein Prüfprotokoll
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {"learners", "sessions", "responses", "mastery", "topic_requests", "reviews", "human_queue"}


@needs_db
def test_export_replaces_atomically_and_leaves_no_temp_files(env, tmp_path):
    _cfg, db = env
    out = tmp_path / "curriculum.db"
    out.write_text("alter Stand")
    reader = open(out, "rb")                  # Karo hält die alte Datei offen
    export_sqlite(db, out, ["MA"])
    assert reader.read() == b"alter Stand"    # alter Stand bleibt für offene Leser lesbar
    reader.close()
    assert sqlite3.connect(out).execute("SELECT value FROM meta WHERE key='subjects'").fetchone()[0] == "MA"
    assert [p.name for p in tmp_path.iterdir()] == ["curriculum.db"]


@needs_db
def test_auto_export_only_when_enabled(env, tmp_path):
    cfg, db = env
    out = tmp_path / "auto.db"
    cfg.raw["sqlite_export"] = {"enabled": "false", "path": str(out)}   # so kommt es aus ${VAR:-false}
    auto_export(cfg, db, log=lambda *_: None)
    assert not out.exists()
    cfg.raw["sqlite_export"] = {"enabled": "true", "path": str(out)}
    auto_export(cfg, db, log=lambda *_: None)
    assert out.exists()
