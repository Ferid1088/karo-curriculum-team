"""Der Dienst prueft mit der echten Pruefung des Abnehmers, nicht mit einem Nachbau.

Der Nachbau war anders als das Original: der Dienst gab eine Lektion frei,
Karos Import lehnte sie ab, und nach zwei Ablehnungen gab der Dienst das Thema
nicht mehr heraus. Hier steht, dass es jetzt dieselbe Pruefung ist — und dass
der Dienst fuer andere Abnehmer trotzdem neutral bleibt.
"""
from __future__ import annotations

import pytest

from kcteam import consumers, lessons

KONZEPT = {"id": "MA.BRUECHE.ADD_UNGL", "version": 1, "first_contact_grade": 5,
           "target_grade": 7, "subject_code": "MA"}


def test_karos_pruefung_ist_eingetragen():
    assert "karo-adaptiv-v1" in consumers.registriert()


def test_fremdes_format_bekommt_keine_zusatzpruefung():
    """Der Dienst ist fuer alle Abnehmer da, nicht nur fuer Karo."""
    spec = {"id": "irgendein-anderer-abnehmer-v3"}
    assert consumers.registriert() and "irgendein-anderer-abnehmer-v3" not in consumers.registriert()
    assert lessons.consumer_findings({"beliebig": "unsinn"}, KONZEPT, spec) == []
    assert lessons.classification_errors({"beliebig": "unsinn"}, KONZEPT, spec) == []


def test_karos_befunde_kommen_aus_karos_paket():
    karo_contract = pytest.importorskip("karo_contract")
    spec = {"id": karo_contract.FORMAT_ID}
    befunde = lessons.consumer_findings({"konzept": {"klasse_von": 5, "klasse_bis": 7}},
                                        KONZEPT, spec, topic="Brüche addieren", subject="Mathematik")
    assert befunde, "Eine offensichtlich unvollstaendige Lektion muss auffallen"
    # dieselbe Meldung, die auch Karo geben wuerde – nicht eine eigene Formulierung
    assert befunde == karo_contract.befunde(
        {"format": spec["id"], "concept_id": KONZEPT["id"], "concept_version": 1,
         "classification": {"source": "approved_curriculum", "first_contact_grade": 5,
                            "target_grade": 7}, "subject": "Mathematik",
         "lesson": {"konzept": {"klasse_von": 5, "klasse_bis": 7}}},
        thema="Brüche addieren", fach="Mathematik")


def test_ausliefern_prueft_nur_die_einordnung():
    """Beim Ausliefern darf eine freigegebene Lektion nicht am Thema scheitern."""
    pytest.importorskip("karo_contract")
    spec = {"id": "karo-adaptiv-v1"}
    passend = {"konzept": {"klasse_von": 5, "klasse_bis": 7}}
    assert lessons.classification_errors(passend, KONZEPT, spec) == []
    falsch = {"konzept": {"klasse_von": 1, "klasse_bis": 1}}
    assert lessons.classification_errors(falsch, KONZEPT, spec)


def test_kaputte_abnehmerpruefung_blockiert_keine_lektion(monkeypatch):
    """Sonst taeuscht ein Fehler im Paket einen Inhaltsfehler vor."""
    def explodiert(lesson, umschlag):
        raise RuntimeError("kaputt")
    monkeypatch.setitem(consumers._REGISTER, "karo-adaptiv-v1",
                        consumers.Abnehmer(befunde=explodiert, einordnung=explodiert))
    monkeypatch.setattr(consumers, "_gemeldet", set())
    assert lessons.consumer_findings({"x": 1}, KONZEPT, {"id": "karo-adaptiv-v1"}) == []
