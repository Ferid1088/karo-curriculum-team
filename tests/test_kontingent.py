"""Ein erschoepftes Kontingent haelt den Dienst an, statt weiterzuklopfen.

Der Anlass steht im Protokoll: 262 Aufrufe fuer ein einziges Thema, 261 davon
fehlgeschlagen, alle mit „You've hit your session limit". An einem Tag gingen
176 von 176 Aufrufen ins Leere — jeder hat beim Anbieter gezaehlt und nichts
geliefert.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

from kcteam import kontingent

URL = os.environ.get("TEST_DATABASE_URL")
JETZT = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)


# ------------------------------------------------------------ Erkennen

@pytest.mark.parametrize("meldung", [
    "claude CLI: You've hit your session limit · resets 2:40pm (UTC)",
    "claude CLI: You've hit your usage limit",
    "API-Fehler: insufficient_quota",
    "Error code: 429 - quota exceeded for this month",
    "Your credit balance is too low to access the API",
])
def test_erschoepftes_kontingent_wird_erkannt(meldung):
    erkannt, _ = kontingent.erkennen(meldung, JETZT)
    assert erkannt, meldung


@pytest.mark.parametrize("meldung", [
    "claude CLI Timeout nach 600s",
    "503 service unavailable",
    "rate limit exceeded, please retry",      # ohne Reset: kurzer Stau, kein Kontingent
    "ungültiges JSON: Die Lektion passt nicht zum Format",
])
def test_andere_fehler_loesen_keine_pause_aus(meldung):
    """Eine Drosselung von Sekunden darf keine Pause von Stunden ausloesen."""
    erkannt, _ = kontingent.erkennen(meldung, JETZT)
    assert not erkannt, meldung


def test_rate_limit_mit_reset_zaehlt_doch():
    erkannt, bis = kontingent.erkennen("429 rate limit, resets at 15:30 UTC", JETZT)
    assert erkannt and bis == dt.datetime(2026, 10, 1, 15, 30, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("meldung,erwartet", [
    ("resets 2:40pm (UTC)", (14, 40)),
    ("resets 1:10am (UTC)", (1, 10)),        # heute schon vorbei -> morgen
    ("resets at 23:00 UTC", (23, 0)),
    ("resets 12:00am", (0, 0)),              # Mitternacht, nicht Mittag
    ("resets 12:30pm", (12, 30)),
])
def test_die_reset_zeit_wird_gelesen(meldung, erwartet):
    bis = kontingent.reset_zeit(meldung, JETZT)
    assert bis is not None, meldung
    assert (bis.hour, bis.minute) == erwartet
    assert bis > JETZT, "eine Reset-Zeit liegt immer in der Zukunft"


def test_ohne_reset_zeit_wird_verdoppelt_aber_nicht_endlos():
    """60 Minuten, dann 120, 240 … und spaetestens morgen frueh um 6."""
    erste, m1 = kontingent.naechste_pause(None, JETZT)
    assert m1 == 60 and erste == JETZT + dt.timedelta(minutes=60)
    zweite, m2 = kontingent.naechste_pause(m1, JETZT)
    assert m2 == 120
    dritte, m3 = kontingent.naechste_pause(m2, JETZT)
    assert m3 == 240
    # Der Deckel: nie laenger als bis zum naechsten Morgen (Europe/Berlin).
    lang, _ = kontingent.naechste_pause(100_000, JETZT)
    assert lang == kontingent.deckel(JETZT)
    assert lang - JETZT < dt.timedelta(days=1)


# ------------------------------------------------------------ Die Pause

pytestmark_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")


@pytest.fixture
def db():
    if not URL:
        pytest.skip("TEST_DATABASE_URL nicht gesetzt")
    from kcteam.db import DB
    d = DB(URL)
    d.migrate()
    d.query("DELETE FROM curriculum.provider_pause")
    return d


def test_die_pause_gilt_fuer_alle_prozesse(db):
    """Sie liegt in der Datenbank, nicht in einer Prozessvariablen.

    Genau daran lag es: der Agent startete neu und klopfte wieder, die API
    wusste ohnehin nie davon.
    """
    from kcteam import pause_store

    assert pause_store.aktiv(db, "claude_token") is None
    bis = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    pause_store.setzen(db, "claude_token", "session limit", bis)

    # Eine zweite Verbindung — stellvertretend fuer API, Agent und Browser.
    from kcteam.db import DB
    andere = DB(URL)
    laufend = pause_store.aktiv(andere, "claude_token")
    assert laufend and laufend["grund"] == "session limit"
    assert laufend["rest_s"] > 3000
    # Ein anderer Anbieter ist davon nicht betroffen.
    assert pause_store.aktiv(db, "anthropic_api") is None


def test_abgelaufene_pause_erlaubt_genau_einen_probeaufruf(db):
    from kcteam import pause_store

    vorbei = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    pause_store.setzen(db, "claude_token", "session limit", vorbei)
    assert pause_store.aktiv(db, "claude_token") is None      # laeuft nicht mehr

    assert pause_store.probe_beanspruchen(db, "claude_token") is True
    # Der zweite Worker bekommt kein Ja — sonst waeren es wieder viele Aufrufe.
    assert pause_store.probe_beanspruchen(db, "claude_token") is False

    pause_store.probe_geglueckt(db, "claude_token")
    assert pause_store.stand(db, "claude_token") is None


def test_eine_laufende_pause_laesst_niemanden_proben(db):
    from kcteam import pause_store

    pause_store.setzen(db, "claude_token", "session limit",
                       dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    assert pause_store.probe_beanspruchen(db, "claude_token") is False


def test_die_naechste_pause_ist_doppelt_so_lang(db):
    from kcteam import pause_store

    erste = pause_store.setzen(db, "claude_token", "usage limit")
    assert erste["minuten"] == 60
    zweite = pause_store.setzen(db, "claude_token", "usage limit")
    # 120 Minuten — es sei denn, der Deckel (morgen frueh) liegt naeher:
    # dann gilt die Restzeit bis dorthin, keine volle Verdopplung.
    _, erwartet = kontingent.naechste_pause(60)
    assert abs(zweite["minuten"] - erwartet) <= 1
    # Mit Reset-Angabe gilt die Angabe des Anbieters, nicht die Verdopplung.
    bis = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
    dritte = pause_store.setzen(db, "claude_token", "session limit", bis)
    assert dritte["minuten"] is None
    assert abs((dritte["bis"] - bis).total_seconds()) < 2


# ---------------------------------------------- Kein einziger Aufruf mehr

class _Zaehlend:
    """Ein Anbieter, der mitzaehlt und immer dasselbe Limit meldet."""

    name = "claude_token"
    settings: dict = {}

    def __init__(self, meldung: str):
        self.meldung = meldung
        self.aufrufe = 0

    @property
    def web_search_enabled(self):
        return False

    def complete(self, **kw):
        from kcteam.providers.base import ProviderError
        self.aufrufe += 1
        raise ProviderError(self.meldung)


def _runner(db, provider):
    from kcteam.agents import AgentRunner
    from kcteam.config import load_config
    os.environ["KARO_SPEC_PATH"] = "/nonexistent"
    return AgentRunner(cfg=load_config(), provider=provider, db=db, run_id=None)


def test_nach_dem_limit_kommt_kein_einziger_aufruf_mehr(db):
    """Die Fertig-Bedingung dieses Schritts, als Test.

    Vorher: 262 Aufrufe fuer ein Thema. Jetzt: einer, der das Limit meldet —
    und danach keiner mehr, bis die Pause vorbei ist.
    """
    from kcteam.kontingent import KontingentErschoepft

    anbieter = _Zaehlend("claude CLI: You've hit your session limit · resets 2:40pm (UTC)")
    läufer = _runner(db, anbieter)

    with pytest.raises(KontingentErschoepft):
        läufer._complete(role="lektionsautor", system="s", prompt="p", model="m",
                         web_search=False, meta=None, entity_id="EXP-1")
    assert anbieter.aufrufe == 1, "der erste Aufruf darf laufen, er findet das Limit"

    # Zehn weitere Versuche — kein einziger erreicht den Anbieter.
    for _ in range(10):
        with pytest.raises(KontingentErschoepft):
            läufer._complete(role="lektionsautor", system="s", prompt="p", model="m",
                             web_search=False, meta=None, entity_id="EXP-1")
    assert anbieter.aufrufe == 1

    from kcteam import pause_store
    stand = pause_store.stand(db, "claude_token")
    assert stand["aufrufe_verhindert"] == 10
    assert stand["bis"].astimezone(dt.timezone.utc).hour == 14      # die Reset-Zeit


def test_ohne_limit_wird_normal_wiederholt(db):
    """Ein Timeout bleibt ein Timeout: dafuer gibt es die Wiederholung."""
    from kcteam.providers.base import ProviderError

    anbieter = _Zaehlend("claude CLI Timeout nach 600s")
    läufer = _runner(db, anbieter)
    läufer.cfg.pipeline["provider_retries"] = 1
    läufer.cfg.pipeline["max_retry_wait"] = 0
    with pytest.raises(ProviderError):
        läufer._complete(role="lektionsautor", system="s", prompt="p", model="m",
                         web_search=False, meta=None, entity_id="EXP-1")
    assert anbieter.aufrufe == 2          # Erstversuch plus eine Wiederholung
    assert pytest.importorskip("kcteam.pause_store").stand(db, "claude_token") is None


def test_der_probeaufruf_oeffnet_die_schlange_wieder(db):
    from kcteam import pause_store

    class _Gelingt(_Zaehlend):
        def complete(self, **kw):
            from kcteam.providers.base import Completion
            self.aufrufe += 1
            return Completion(text="{}", model="m")

    vorbei = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    pause_store.setzen(db, "claude_token", "session limit", vorbei)

    anbieter = _Gelingt("")
    läufer = _runner(db, anbieter)
    ergebnis, _ = läufer._complete(role="lektionsautor", system="s", prompt="p",
                                   model="m", web_search=False, meta=None, entity_id="EXP-1")
    assert ergebnis.text == "{}" and anbieter.aufrufe == 1
    assert pause_store.stand(db, "claude_token") is None, "die Pause ist aufgehoben"
