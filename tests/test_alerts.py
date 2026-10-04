"""Betriebsgrenzen: alerts.pruefen wertet config-Schwellen aus, wirft nie."""
from __future__ import annotations

import pytest

from kcteam import alerts
from kcteam.config import load_config


class FakeDB:
    """Liefert je nach SQL-Auszug vorbereitete Zeilen — wie db.query."""

    def __init__(self, **kuellen):
        self.kuellen = kuellen  # schluessel: teil der SQL -> zeilen

    def query(self, sql, params=None):
        for teil, zeilen in self.kuellen.items():
            if teil in sql:
                return zeilen
        return []


def _cfg(**werte):
    cfg = load_config()
    cfg.alerts = werte
    return cfg


def test_keine_funde_bei_gesundem_stand():
    db = FakeDB()
    assert alerts.pruefen(db, _cfg()) == []


def test_stuck_session_schlaegt_an():
    db = FakeDB(**{
        "WHERE status='working'": [
            {"call_key": "k", "session_id": "devin-x", "role": "autor",
             "entity_id": "K1", "minuten": 400.0}],
        "FROM curriculum.lesson_exports": [{"status": "ready"}] * 5,
        "missing_remote FROM": [{"missing_remote": 0}] * 5,
    })
    funde = alerts.pruefen(db, _cfg(stuck_session_minutes=120))
    assert [f["art"] for f in funde] == ["stuck_session"]
    assert "devin-x" in funde[0]["text"]
    assert "400" in funde[0]["text"]


def test_stuck_session_unter_schwelle_kein_fund():
    db = FakeDB()
    funde = alerts.pruefen(db, _cfg(stuck_session_minutes=120))
    assert not any(f["art"] == "stuck_session" for f in funde)


def test_pending_exports_ueber_schwelle():
    db = FakeDB(**{
        "status IN ('waiting','queued','running')": [{"n": 3}],
    })
    funde = alerts.pruefen(db, _cfg(pending_export_minutes=60))
    assert any(f["art"] == "pending_exports" and "3" in f["text"] for f in funde)


def test_validation_reject_rate():
    zeilen = [{"status": "failed"}] * 4 + [{"status": "ready"}] * 6
    db = FakeDB(**{"FROM curriculum.lesson_exports": zeilen})
    funde = alerts.pruefen(db, _cfg(validation_reject_rate=0.3))
    assert any(f["art"] == "validation_rejects" for f in funde)
    # Schwelle hoch genug -> kein Fund
    funde = alerts.pruefen(db, _cfg(validation_reject_rate=0.9))
    assert not any(f["art"] == "validation_rejects" for f in funde)


def test_validation_rate_braucht_stichprobe():
    # Weniger als 10 Exporte -> keine Quote, kein Fehlalarm
    db = FakeDB(**{"FROM curriculum.lesson_exports": [{"status": "failed"}] * 5})
    assert alerts.pruefen(db, _cfg(validation_reject_rate=0.0)) == []


def test_missing_remote_rate():
    zeilen = [{"missing_remote": 1}] * 3 + [{"missing_remote": 0}] * 7
    db = FakeDB(**{"missing_remote FROM": zeilen})
    funde = alerts.pruefen(db, _cfg(missing_remote_rate=0.2))
    assert any(f["art"] == "missing_remote" for f in funde)


def test_pruefen_wirft_nie_bei_kaputtem_db():
    class KaputtesDB:
        def query(self, *a, **k):
            raise RuntimeError("verbindung weg")
    assert alerts.pruefen(KaputtesDB(), _cfg()) == []


def test_grenzen_defaults_und_kaputte_werte():
    g = alerts.grenzen(_cfg())
    assert g["stuck_session_minutes"] == 150
    g = alerts.grenzen(_cfg(stuck_session_minutes="kaputt", missing_remote_rate=0.5))
    assert g["stuck_session_minutes"] == 150
    assert g["missing_remote_rate"] == 0.5


def test_config_laedt_alerts_aus_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("alerts:\n  stuck_session_minutes: 42\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.alerts == {"stuck_session_minutes": 42}


def test_fehlerklasse_trennt_betrieb_von_inhalt():
    from kcteam.alerts import _fehlerklasse, CONTENT_KLASSEN
    assert _fehlerklasse("claude CLI: You've hit your weekly limit") == "provider_quota"
    assert _fehlerklasse("Devin API POST /sessions: 401") == "provider_auth"
    assert _fehlerklasse("Auftrag hat 29708 Zeichen — über der Grenze") == "prompt_too_large"
    assert _fehlerklasse("connection timed out") == "provider_transport"
    assert _fehlerklasse("Die Lektion passt nicht zum Format: ...") == "contract_invalid"
    assert _fehlerklasse(None) == "content"          # Unbekannt: vorsichtig fachlich
    assert {"provider_quota", "provider_auth", "provider_transport",
            "prompt_too_large"}.isdisjoint(CONTENT_KLASSEN)


def test_reject_rate_zaehlt_nur_fachliche_verluste():
    """Quota-Fehlschläge blasen die Material-Quote nicht auf."""
    zeilen = ([{"status": "failed", "message": "weekly limit resets"}] * 8
              + [{"status": "failed", "message": "passt nicht zum Format"}] * 2
              + [{"status": "ready", "message": None}] * 10)
    db = FakeDB(**{"FROM curriculum.lesson_exports": zeilen})
    funde = alerts.pruefen(db, _cfg(validation_reject_rate=0.30))
    # 2/20 = 10 % fachlich — kein Alarm trotz 50 % Gesamtverlust
    assert not any(f["art"] == "validation_rejects" for f in funde)
    funde = alerts.pruefen(db, _cfg(validation_reject_rate=0.05))
    assert any(f["art"] == "validation_rejects" and "2/20" in f["text"]
               for f in funde)
