"""Beweist, dass eine normale Datenbank nicht mehr zerstört werden kann.

Der Schaden, gegen den das hier steht: `TEST_DATABASE_URL` zeigte auf die
laufende Entwicklungsdatenbank, die Fixtures löschten dort `DROP SCHEMA …
CASCADE`. Jede der vier Schichten hätte das allein verhindert.
"""
from __future__ import annotations

import os

import pytest

from kcteam import dbsafety
from kcteam.dbsafety import DestructiveOperationRefused

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")


class FakeDB:
    """Antwortet wie die echte DB, ohne eine anzufassen."""

    def __init__(self, name="karo", identity=None, has_identity=True):
        self.name, self.identity, self.has_identity = name, identity, has_identity
        self.ausgefuehrt: list[str] = []

    def query(self, sql, params=None):
        self.ausgefuehrt.append(sql)
        if "current_database()" in sql:
            return [{"name": self.name}]
        if "to_regclass" in sql:
            return [{"da": self.has_identity}]
        if "FROM public.system_identity" in sql:
            return [self.identity] if self.identity else []
        return []


@pytest.fixture
def test_umgebung(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv(dbsafety.KILL_SWITCH, dbsafety.KILL_SWITCH_VALUE)


TEST_IDENTITY = {"environment": "test", "database_role": "curriculum",
                 "stamped_at": None, "note": None}


# ---------------- Schicht 1: die URL, vor jeder Verbindung ----------------
@pytest.mark.parametrize("url", [
    "postgresql://karo:karo@localhost:5432/karo",              # Entwicklung
    "postgresql://karo:karo@db.example.com:5432/karo_prod",    # produktionsartig
    "postgresql://karo:karo@localhost:5432/curriculum",        # falscher Name
    "postgresql://karo:karo@localhost:5432/testkaro",          # "test" nur vorn
    "",
])
def test_pytest_darf_keine_nicht_test_datenbank_benutzen(url):
    with pytest.raises(DestructiveOperationRefused):
        dbsafety.guard_pytest_url(url)


def test_eine_echte_test_url_kommt_durch():
    url = "postgresql://karo:karo@127.0.0.1:5434/karo_curriculum_test"
    assert dbsafety.guard_pytest_url(url) == url
    # Auch mit Parametern hinter dem Namen.
    assert dbsafety.database_name_from_url(url + "?sslmode=require") == "karo_curriculum_test"


# ---------------- Schicht 2 bis 4: die offene Verbindung ----------------
def test_reset_auf_dev_datenbank_wird_abgelehnt(test_umgebung):
    db = FakeDB(name="karo", identity=TEST_IDENTITY)
    with pytest.raises(DestructiveOperationRefused, match="endet nicht"):
        dbsafety.assert_destructive_allowed(db)


def test_reset_auf_produktionsartiger_datenbank_wird_abgelehnt(test_umgebung):
    db = FakeDB(name="karo_curriculum_prod",
                identity={**TEST_IDENTITY, "environment": "production"})
    with pytest.raises(DestructiveOperationRefused):
        dbsafety.assert_destructive_allowed(db)


def test_reset_ohne_kill_switch_wird_abgelehnt(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv(dbsafety.KILL_SWITCH, raising=False)
    db = FakeDB(name="karo_curriculum_test", identity=TEST_IDENTITY)
    with pytest.raises(DestructiveOperationRefused, match=dbsafety.KILL_SWITCH):
        dbsafety.assert_destructive_allowed(db)
    assert db.ausgefuehrt == []          # nichts hat die Datenbank angefasst


def test_reset_ohne_app_env_test_wird_abgelehnt(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv(dbsafety.KILL_SWITCH, dbsafety.KILL_SWITCH_VALUE)
    db = FakeDB(name="karo_curriculum_test", identity=TEST_IDENTITY)
    with pytest.raises(DestructiveOperationRefused, match="APP_ENV"):
        dbsafety.assert_destructive_allowed(db)
    assert db.ausgefuehrt == []


def test_reset_mit_falscher_identitaet_wird_abgelehnt(test_umgebung):
    db = FakeDB(name="karo_curriculum_test",
                identity={**TEST_IDENTITY, "environment": "dev"})
    with pytest.raises(DestructiveOperationRefused, match="environment"):
        dbsafety.assert_destructive_allowed(db)


def test_reset_ohne_identitaet_wird_abgelehnt(test_umgebung):
    """Ein richtiger Name allein genügt nicht: die Datenbank muss es selbst sagen."""
    db = FakeDB(name="karo_curriculum_test", has_identity=False)
    with pytest.raises(DestructiveOperationRefused, match="system_identity"):
        dbsafety.assert_destructive_allowed(db)


def test_reset_auf_korrekt_gestempelter_test_datenbank_ist_erlaubt(test_umgebung):
    db = FakeDB(name="karo_curriculum_test", identity=TEST_IDENTITY)
    assert dbsafety.assert_destructive_allowed(db) == "karo_curriculum_test"


# ---------------- am echten Server ----------------
@needs_db
def test_die_laufende_testdatenbank_traegt_ihre_identitaet():
    from kcteam.db import DB
    db = DB(URL)
    assert dbsafety.current_database(db).endswith(dbsafety.TEST_SUFFIX)
    identitaet = dbsafety.read_identity(db)
    assert identitaet and identitaet["environment"] == "test"
    assert identitaet["database_role"] == "curriculum"


@needs_db
def test_der_sichere_reset_laeuft_und_haelt_den_stempel():
    from kcteam.db import DB
    from tools.reset_test_db import reset_schemas
    db = DB(URL)
    name = reset_schemas(db, mit_backup=False)
    assert name.endswith(dbsafety.TEST_SUFFIX)
    # Nach dem Leeren stehen Schemata und Stempel wieder.
    assert dbsafety.read_identity(db)["environment"] == "test"
    vorhanden = db.query("SELECT count(*) AS n FROM information_schema.schemata "
                         "WHERE schema_name = ANY(%s)", (["karo", "curriculum", "learner"],))
    assert vorhanden[0]["n"] == 3


def test_kein_code_setzt_den_kill_switch_selbst():
    """Ein Schalter, den der Prozess bei Bedarf selbst umlegt, ist keiner.

    Weder conftest noch Anwendungscode dürfen APP_ENV oder den Kill-Switch
    setzen — nur lesen. Die Testdateien hier dürfen es über `monkeypatch`,
    weil das den Prozess nicht überdauert.
    """
    import pathlib
    import re
    wurzel = pathlib.Path(__file__).parents[1]
    schreibend = re.compile(
        r"""(?:os\.environ(?:\.setdefault\(|\[)\s*["'](?:APP_ENV|ALLOW_DESTRUCTIVE_DB_RESET)"""
        r"""|putenv\(\s*["'](?:APP_ENV|ALLOW_DESTRUCTIVE_DB_RESET))""")
    treffer = []
    for pfad in (list(wurzel.glob("kcteam/**/*.py")) + list(wurzel.glob("tools/*.py"))
                 + [wurzel / "tests" / "conftest.py"]):
        if schreibend.search(pfad.read_text(encoding="utf-8")):
            treffer.append(pfad.relative_to(wurzel).as_posix())
    assert treffer == [], treffer


def test_env_beispiel_enthaelt_keinen_gefaehrlichen_standard():
    """`.env.example` darf den Schalter nie scharf vorgeben."""
    import pathlib
    beispiel = (pathlib.Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")
    for zeile in beispiel.splitlines():
        nackt = zeile.strip()
        if nackt.startswith("#") or "=" not in nackt:
            continue
        name, _, wert = nackt.partition("=")
        assert name.strip() != dbsafety.KILL_SWITCH, f"scharf vorgegeben: {zeile}"
        assert not (name.strip() == "APP_ENV" and wert.strip() == "test"), zeile


def test_im_projekt_steht_nur_noch_ein_einziges_drop_schema():
    """Zerstörung gibt es genau an einer Stelle — hinter allen Prüfungen."""
    import pathlib
    wurzel = pathlib.Path(__file__).parents[1]
    treffer = []
    for pfad in list(wurzel.glob("kcteam/**/*.py")) + list(wurzel.glob("tests/*.py")) \
            + list(wurzel.glob("tools/*.py")):
        text = pfad.read_text(encoding="utf-8")
        if "DROP SCHEMA" in text and "dbsafety" not in pfad.name:
            treffer.append(pfad.relative_to(wurzel).as_posix())
    assert treffer == ["tools/reset_test_db.py"], treffer
