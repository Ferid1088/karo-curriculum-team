"""Alle Dienste auf demselben Stand — und ein Befehl, der es sagt.

Die API lief auf dem neuen Image, Agent und Browser noch tagelang auf dem
alten. Von aussen war das nicht zu sehen: die API meldete ihren Stand unter
/v1/meta, die anderen gar nichts. Hinterher liess sich nicht sagen, welcher
Code eine Lektion geschrieben hatte.
"""
from __future__ import annotations

import os

import pytest

from kcteam import version

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")


@pytest.fixture
def db():
    from kcteam.db import DB
    d = DB(URL)
    d.migrate()
    d.query("DELETE FROM curriculum.service_heartbeat")
    return d


def _melden(db, dienst, sha, alter_s=0):
    db.query("""INSERT INTO curriculum.service_heartbeat(service, git_sha, contract_version, pid, last_seen)
                VALUES (%s,%s,%s,1, now() - make_interval(secs => %s))
                ON CONFLICT (service) DO UPDATE SET git_sha=EXCLUDED.git_sha, last_seen=EXCLUDED.last_seen""",
             (dienst, sha, "karo-adaptiv-v1.1", alter_s))


def test_gleicher_stand_ist_in_ordnung(db):
    for dienst in ("api", "agent", "admin"):
        _melden(db, dienst, "abc123def456")
    ok, zeilen = version.befund(db)
    assert ok, zeilen
    assert all(z.startswith("✓") for z in zeilen)


def test_verschiedene_staende_fallen_auf(db):
    """Genau der Fall, der tagelang unbemerkt lief."""
    _melden(db, "api", "neu0000neu00")
    _melden(db, "agent", "alt0000alt00")
    _melden(db, "admin", "alt0000alt00")
    ok, zeilen = version.befund(db)
    assert not ok
    assert any("Verschiedene Staende" in z for z in zeilen)


def test_ein_stiller_dienst_gilt_nicht_als_laufend(db):
    """Ein alter Eintrag sagt nichts ueber das, was gerade laeuft."""
    _melden(db, "api", "abc123def456")
    _melden(db, "agent", "abc123def456")
    _melden(db, "admin", "abc123def456", alter_s=version.FRISCH_SEKUNDEN + 60)
    ok, zeilen = version.befund(db)
    assert not ok
    assert any("laeuft nicht" in z for z in zeilen)


def test_unbekannter_stand_zaehlt_als_abweichung(db):
    """„unbekannt" heisst: ohne KCTEAM_GIT_SHA gebaut. Dann ist der Vergleich wertlos."""
    for dienst in ("api", "agent", "admin"):
        _melden(db, dienst, "unbekannt")
    ok, zeilen = version.befund(db)
    assert not ok
    assert any("ohne KCTEAM_GIT_SHA" in z for z in zeilen)


def test_verschiedene_vertragsfassungen_fallen_auf(db):
    for dienst in ("api", "agent", "admin"):
        _melden(db, dienst, "abc123def456")
    db.query("UPDATE curriculum.service_heartbeat SET contract_version='karo-adaptiv-v0.9' "
             "WHERE service='agent'")
    ok, zeilen = version.befund(db)
    assert not ok and any("Vertragsfassungen" in z for z in zeilen)


def test_melden_schreibt_den_eigenen_stand(db):
    version.melden(db, "cli")
    row = db.one("SELECT * FROM curriculum.service_heartbeat WHERE service='cli'")
    assert row["git_sha"] == version.GIT_SHA and row["pid"] == os.getpid()


def test_doctor_bricht_ab_wenn_die_staende_auseinanderlaufen(db, monkeypatch, capsys):
    from kcteam import cli
    _melden(db, "api", "neu0000neu00")
    _melden(db, "agent", "alt0000alt00")
    _melden(db, "admin", "alt0000alt00")
    monkeypatch.setenv("DATABASE_URL", URL)
    assert cli.main(["doctor"]) == 1
    assert "nicht auf demselben Stand" in capsys.readouterr().out
    for dienst in ("api", "agent", "admin"):
        _melden(db, dienst, "gleich000000")
    assert cli.main(["doctor"]) == 0


def test_kosten_rechnet_mit_median_statt_mittelwert(db, monkeypatch, capsys):
    """Ein einziges dauernd scheiterndes Thema verdreifacht sonst die Zahl.

    Genau so sah es im Betrieb aus: 103 Aufrufe fuer ein Thema, davon 95
    fehlgeschlagen — neben zehn fuer ein gesundes.
    """
    from kcteam import cli
    db.query("DELETE FROM curriculum.agent_calls")
    for thema, aufrufe, fehler in (("gesund a", 10, 0), ("gesund b", 8, 1), ("kaputt", 103, 95)):
        for i in range(aufrufe):
            db.query("""INSERT INTO curriculum.agent_calls(role, provider, topic, ok,
                          input_tokens, output_tokens)
                        VALUES ('lektionsautor','mock',%s,%s,100,50)""", (thema, i >= fehler))
    monkeypatch.setenv("DATABASE_URL", URL)
    assert cli.main(["kosten", "--themen", "20"]) == 0
    aus = capsys.readouterr().out
    assert "Median      10.0" in aus
    assert "nach Median    200" in aus
    assert "davon 95 fehlgeschlagen" in aus
    # Der Mittelwert steht daneben, aber als das, was er ist.
    assert "hochgezogen" in aus
    db.query("DELETE FROM curriculum.agent_calls")


def test_kosten_ohne_daten_sagt_das_auch(db, monkeypatch, capsys):
    from kcteam import cli
    db.query("DELETE FROM curriculum.agent_calls")
    monkeypatch.setenv("DATABASE_URL", URL)
    assert cli.main(["kosten"]) == 0
    assert "Noch keine Modellaufrufe" in capsys.readouterr().out
