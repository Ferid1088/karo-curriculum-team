"""Die Pause lesen und setzen — fuer alle Prozesse dieselbe Quelle.

Getrennt von `kontingent.py` (das rechnet und erkennt, ohne Datenbank), damit
die Erkennung ohne Postgres testbar bleibt.
"""
from __future__ import annotations

import datetime as dt
import logging

from . import kontingent

log = logging.getLogger(__name__)


def aktiv(db, provider: str) -> dict | None:
    """Die laufende Pause dieses Anbieters — oder None.

    „Laufend" heisst: das Ende liegt in der Zukunft. Eine abgelaufene Pause
    bleibt stehen, bis ein Probeaufruf sie aufhebt: daran erkennt der naechste
    Aufruf, dass er der Probeaufruf ist.
    """
    row = db.one("""SELECT *, bis > now() AS laeuft,
                           greatest(0, extract(epoch FROM bis - now()))::int AS rest_s
                      FROM curriculum.provider_pause WHERE provider=%s""", (provider,))
    return row if row and row["laeuft"] else None


def stand(db, provider: str) -> dict | None:
    """Der Eintrag, auch wenn die Pause schon abgelaufen ist."""
    return db.one("""SELECT *, bis > now() AS laeuft,
                            greatest(0, extract(epoch FROM bis - now()))::int AS rest_s
                       FROM curriculum.provider_pause WHERE provider=%s""", (provider,))


def setzen(db, provider: str, grund: str, bis: dt.datetime | None = None) -> dict:
    """Pause beginnen oder verlaengern.

    Ohne `bis` wird gerechnet: 60 Minuten, danach jeweils doppelt, hoechstens
    bis zum naechsten Morgen. Nennt der Anbieter eine Reset-Zeit, gilt die —
    er weiss es besser als jede Verdopplung.
    """
    vorher = stand(db, provider)
    minuten = None
    if bis is None:
        bis, minuten = kontingent.naechste_pause(
            (vorher or {}).get("minuten") if vorher else None)
    db.query("""INSERT INTO curriculum.provider_pause
                  (provider, bis, grund, erkannt_am, minuten, probe_seit, probe_am,
                   aufrufe_verhindert)
                VALUES (%s,%s,%s, now(), %s, NULL, NULL, 0)
                ON CONFLICT (provider) DO UPDATE
                  SET bis=EXCLUDED.bis, grund=EXCLUDED.grund, erkannt_am=now(),
                      minuten=EXCLUDED.minuten, probe_seit=NULL, probe_am=NULL""",
             (provider, bis, grund[:500], minuten))
    log.warning("Kontingent %s erschoepft, Pause bis %s: %s", provider, bis, grund[:200])
    return stand(db, provider)


def verhindert_zaehlen(db, provider: str) -> None:
    """Mitzaehlen, wie viele Aufrufe die Pause verhindert hat.

    Die Zahl ist der Beleg dafuer, dass die Pause etwas tut — ohne sie waeren
    das alles Fehlaufrufe gewesen.
    """
    db.query("""UPDATE curriculum.provider_pause
                   SET aufrufe_verhindert = aufrufe_verhindert + 1
                 WHERE provider=%s""", (provider,))


def probe_beanspruchen(db, provider: str) -> bool:
    """Darf dieser Prozess den einen Probeaufruf machen?

    Genau einer: `probe_seit` wird in derselben Anweisung gesetzt, mit der
    geprueft wird. Zwei Worker gleichzeitig bekommen nicht beide ein Ja.
    Ein haengender Probeaufruf blockiert nicht ewig — nach 10 Minuten darf
    der naechste es versuchen.
    """
    rows = db.query("""UPDATE curriculum.provider_pause
                          SET probe_seit = now()
                        WHERE provider=%s AND bis <= now()
                          AND (probe_seit IS NULL OR probe_seit < now() - interval '10 minutes')
                        RETURNING provider""", (provider,))
    return bool(rows)


def probe_geglueckt(db, provider: str) -> None:
    """Der Probeaufruf kam durch: die Pause ist vorbei."""
    db.query("DELETE FROM curriculum.provider_pause WHERE provider=%s", (provider,))
    log.info("Kontingent %s wieder da — Warteschlange laeuft weiter", provider)


def aufheben(db, provider: str) -> None:
    """Von Hand freigeben (Mensch im Browser oder auf der Kommandozeile)."""
    db.query("DELETE FROM curriculum.provider_pause WHERE provider=%s", (provider,))
