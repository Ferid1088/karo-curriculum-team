"""Datenbank-Browser: Passwort, nur lesen, Geheimnisse ausgeblendet, beide Datenbanken durchsuchbar."""
from __future__ import annotations

import os
import sqlite3

import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")

KARO_DDL = """
CREATE TABLE lern_konzept (id INTEGER PRIMARY KEY, fach TEXT, thema_key TEXT, konzept_key TEXT, label TEXT,
  klasse_von INT, klasse_bis INT, stichworte TEXT DEFAULT '[]', quelle TEXT, geprueft_am TEXT, aktiv INT DEFAULT 1,
  created_at TEXT);
CREATE TABLE lern_fehlertyp (id INTEGER PRIMARY KEY, konzept_id INT, fehler_key TEXT, label TEXT, beschreibung TEXT);
CREATE TABLE lern_fehler_alias (id INTEGER PRIMARY KEY, fehlertyp_id INT, muster TEXT);
CREATE TABLE lern_erklaerung (id INTEGER PRIMARY KEY, fehlertyp_id INT, klasse INT, version INT, visualisierung TEXT,
  ausgeliefert INT DEFAULT 0);
CREATE TABLE lern_aufgabe (id INTEGER PRIMARY KEY, fehlertyp_id INT, rolle TEXT, position INT, frage TEXT,
  loesung TEXT, typischer_fehler TEXT, geprueft_am TEXT, aktiv INT DEFAULT 1);
CREATE TABLE lern_hilfe (id INTEGER PRIMARY KEY, konzept_id INT, art TEXT, schluessel TEXT, text TEXT, sortierung INT);
CREATE TABLE lern_erstkontakt (id INTEGER PRIMARY KEY, konzept_id INT, anker TEXT, erste_aufgabe TEXT);
CREATE TABLE "zugang x" (id INTEGER PRIMARY KEY, name TEXT, api_key TEXT);
INSERT INTO lern_konzept VALUES (1,'mathematik','brueche','brueche-addieren','Brüche addieren',5,6,'["brüche"]',
  'curriculum','2026-09-01',1,'2026-09-01');
INSERT INTO lern_fehlertyp VALUES (1,1,'nenner-addiert','Nenner addiert','zählt Nenner zusammen');
INSERT INTO lern_fehler_alias VALUES (1,1,'2/5');
INSERT INTO lern_aufgabe VALUES (1,1,'gefuehrt',0,'1/2 + 1/3 = ?','5/6','2/5','2026-09-01',1);
INSERT INTO "zugang x" VALUES (1,'eltern','GEHEIM123');
"""


@pytest.fixture(scope="module")
def ui(tmp_path_factory):
    from fastapi.testclient import TestClient

    from kcteam.admin import ReadOnlyDB, create_app
    from kcteam.db import DB
    DB(URL).migrate()   # Schema vorhanden (andere Tests füllen es)
    path = tmp_path_factory.mktemp("karo") / "karo.db"
    con = sqlite3.connect(path)
    con.executescript(KARO_DDL)
    con.commit()
    con.close()
    os.environ["ADMIN_PASSWORD"] = "pw-test"
    db = ReadOnlyDB(URL)
    return TestClient(create_app(db, str(path))), db, path


def test_refuses_without_password(monkeypatch):
    from kcteam.admin import create_app
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        create_app()


def test_login_required(ui):
    client, _, _ = ui
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("admin", "falsch")).status_code == 401
    r = client.get("/", auth=("admin", "pw-test"))
    assert r.status_code == 200 and "Übersicht" in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_postgres_is_read_only(ui):
    _, db, _ = ui
    with pytest.raises(Exception) as exc:
        db.query("CREATE TABLE curriculum.darf_nicht (x int)")
    assert "read-only" in str(exc.value)


def test_curriculum_pages(ui):
    client, db, _ = ui
    a = ("admin", "pw-test")
    for path in ["/curriculum/requests", "/curriculum/queue", "/curriculum/demand", "/curriculum/exports",
                 "/curriculum/clients", "/tables/curriculum"]:
        assert client.get(path, auth=a).status_code == 200, path
    subj = db.one("SELECT code FROM curriculum.subjects LIMIT 1")
    if subj:
        r = client.get(f"/curriculum/subjects/{subj['code']}?q=Br", auth=a)
        assert r.status_code == 200
        c = db.one("SELECT id FROM curriculum.concepts WHERE status='approved' LIMIT 1")
        r = client.get(f"/curriculum/concepts/{c['id']}", auth=a)
        assert r.status_code == 200 and "Aufgaben" in r.text
        assert "<svg" not in r.text          # Bilder nur als data:-URI in <img>
    r = client.get("/tables/curriculum/curriculum.api_clients", auth=a)
    assert r.status_code == 200 and "key_hash" in r.text and "Ausgeblendet" in r.text
    assert client.get("/tables/curriculum/pg_catalog.pg_authid", auth=a).status_code == 404
    # Suche findet nichts über ausgeblendete Spalten (kein Erraten von Geheimnissen)
    h = db.one("SELECT key_hash FROM curriculum.api_clients LIMIT 1")
    if h:
        r = client.get(f"/tables/curriculum/curriculum.api_clients?q={h['key_hash'][:12]}", auth=a)
        assert "0 Zeilen" in r.text
    e = db.one("SELECT id FROM curriculum.lesson_exports LIMIT 1")
    if e:
        assert client.get(f"/curriculum/exports/{e['id']}", auth=a).status_code == 200


def test_karo_pages_and_secret_columns(ui):
    client, _, path = ui
    a = ("admin", "pw-test")
    r = client.get("/karo?q=Brüche", auth=a)
    assert r.status_code == 200 and "Brüche addieren" in r.text and "curriculum" in r.text
    r = client.get("/karo/konzept/1", auth=a)
    assert r.status_code == 200 and "Nenner addiert" in r.text and "2/5" in r.text
    r = client.get("/tables/karo", auth=a)
    assert r.status_code == 200 and "lern_aufgabe" in r.text
    r = client.get('/tables/karo/zugang x', auth=a)
    assert r.status_code == 200 and "eltern" in r.text and "GEHEIM123" not in r.text
    assert "0 Zeilen" in client.get("/tables/karo/zugang x?q=GEHEIM", auth=a).text
    r = client.get("/tables/karo/lern_aufgabe?q=5/6", auth=a)
    assert r.status_code == 200 and "1/2 + 1/3" in r.text
    assert client.get("/tables/karo/gibtsnicht", auth=a).status_code == 404
    # Datei wurde nicht verändert
    before = path.stat().st_mtime_ns
    client.get("/tables/karo/lern_konzept", auth=a)
    assert path.stat().st_mtime_ns == before
