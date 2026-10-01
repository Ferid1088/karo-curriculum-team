"""Wenn das Kontingent erschoepft ist: anhalten, nicht weiterklopfen.

Der Anlass steht im Protokoll: 262 Aufrufe fuer ein einziges Thema, 261 davon
fehlgeschlagen, alle mit „You've hit your session limit". An einem Tag gingen
176 von 176 Aufrufen ins Leere. Jeder einzelne hat Zeit gekostet, nichts
geliefert, und beim Anbieter gezaehlt.

Ein erschoepftes Kontingent ist kein voruebergehender Fehler, bei dem
Wiederholen hilft. Es ist eine Tuer, die bis zu einer bestimmten Uhrzeit zu
ist. Also wird sie gemerkt — in der Datenbank, nicht im Prozess, damit alle
Prozesse (API, Agent, Browser) und auch ein Neustart davon wissen.

Danach: kein einziger Modellaufruf bis zum Ende der Pause. Auftraege werden
zurueckgestellt, ohne einen Versuch zu verbrauchen — das Kontingent ist nicht
die Schuld des Auftrags. Nach der Pause genau EIN Probeaufruf: klappt er,
laeuft die Schlange weiter; klappt er nicht, beginnt die naechste Pause,
doppelt so lang.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ZEITZONE = ZoneInfo("Europe/Berlin")

#: Ohne Reset-Angabe: so lange warten, dann jeweils doppelt.
ERSTE_PAUSE_MINUTEN = 60
#: Nie laenger als bis zum naechsten Morgen — ein Abo fuellt sich spaetestens
#: ueber Nacht wieder, und eine Pause bis uebermorgen waere ein Ausfall.
SPAETESTENS_UHR = 6


class KontingentErschoepft(RuntimeError):
    """Das Kontingent des Anbieters ist aufgebraucht.

    Eigene Klasse, weil die Folge eine andere ist als bei jedem anderen
    Fehler: nicht wiederholen, sondern aufhoeren — und zwar fuer alle.
    """

    def __init__(self, meldung: str, bis: dt.datetime | None = None,
                 provider: str = ""):
        super().__init__(meldung)
        self.bis = bis
        self.provider = provider


#: Woran ein erschoepftes Kontingent zu erkennen ist. Bewusst eng: ein
#: „rate limit" ohne Reset-Angabe ist oft nur eine kurze Drosselung, und die
#: gehoert in die normale Wiederholung, nicht in eine stundenlange Pause.
_SICHER = re.compile(
    r"(session limit"
    r"|usage limit"
    r"|insufficient_quota"
    r"|quota exceeded"
    r"|exceeded your current quota"
    r"|credit balance is too low"
    r"|monthly limit"
    r"|daily limit reached)", re.I)

#: „rate limit" oder 429 zaehlt nur mit Reset-Angabe als Kontingent.
_VIELLEICHT = re.compile(r"(rate.?limit|\b429\b|too many requests)", re.I)

#: „resets 2:40pm (UTC)", „resets at 14:40 UTC", „resets 1:10am"
_RESET = re.compile(
    r"resets?(?:\s+at)?\s+"
    r"(\d{1,2})(?::(\d{2}))?\s*"
    r"(am|pm)?"
    r"(?:\s*\(?\s*(UTC|GMT|Z|[A-Za-z]+/[A-Za-z_]+)\s*\)?)?", re.I)


def reset_zeit(meldung: str, jetzt: dt.datetime | None = None) -> dt.datetime | None:
    """Liest „resets 2:40pm (UTC)" aus der Meldung des Anbieters.

    Gibt einen Zeitpunkt in UTC zurueck, oder None. Liegt die genannte
    Uhrzeit heute schon hinter uns, ist der naechste Tag gemeint — so steht
    es in den Meldungen der CLI auch.
    """
    treffer = _RESET.search(meldung or "")
    if not treffer:
        return None
    stunde = int(treffer.group(1))
    minute = int(treffer.group(2) or 0)
    haelfte = (treffer.group(3) or "").lower()
    zone_name = treffer.group(4) or "UTC"
    if not 0 <= stunde <= 23 or not 0 <= minute <= 59:
        return None
    if haelfte == "pm" and stunde < 12:
        stunde += 12
    elif haelfte == "am" and stunde == 12:
        stunde = 0

    try:
        zone = dt.timezone.utc if zone_name.upper() in ("UTC", "GMT", "Z") else ZoneInfo(zone_name)
    except Exception:                       # noqa: BLE001 - unbekannte Zone
        zone = dt.timezone.utc

    jetzt = jetzt or dt.datetime.now(dt.timezone.utc)
    lokal = jetzt.astimezone(zone)
    ziel = lokal.replace(hour=stunde, minute=minute, second=0, microsecond=0)
    if ziel <= lokal:
        ziel += dt.timedelta(days=1)
    return ziel.astimezone(dt.timezone.utc)


def erkennen(meldung: str, jetzt: dt.datetime | None = None
             ) -> tuple[bool, dt.datetime | None]:
    """Ist das ein erschoepftes Kontingent, und bis wann?

    Gibt (erkannt, reset_zeit_oder_None) zurueck.
    """
    text = meldung or ""
    bis = reset_zeit(text, jetzt)
    if _SICHER.search(text):
        return True, bis
    # Drosselung ohne Reset-Angabe ist kein Kontingent, sondern ein kurzer
    # Stau — den loest die normale Wiederholung besser als eine Pause.
    if _VIELLEICHT.search(text) and bis is not None:
        return True, bis
    return False, None


def deckel(jetzt: dt.datetime | None = None) -> dt.datetime:
    """Spaetestens bis morgen frueh — laenger ist ein Ausfall, keine Pause."""
    jetzt = jetzt or dt.datetime.now(dt.timezone.utc)
    lokal = jetzt.astimezone(ZEITZONE)
    ziel = lokal.replace(hour=SPAETESTENS_UHR, minute=0, second=0, microsecond=0)
    if ziel <= lokal:
        ziel += dt.timedelta(days=1)
    return ziel.astimezone(dt.timezone.utc)


def naechste_pause(vorherige_minuten: int | None, jetzt: dt.datetime | None = None
                   ) -> tuple[dt.datetime, int]:
    """Ohne Reset-Angabe: 60 Minuten, danach jeweils doppelt, bis zum Deckel."""
    jetzt = jetzt or dt.datetime.now(dt.timezone.utc)
    minuten = ERSTE_PAUSE_MINUTEN if not vorherige_minuten else vorherige_minuten * 2
    bis = jetzt + dt.timedelta(minutes=minuten)
    grenze = deckel(jetzt)
    if bis > grenze:
        bis = grenze
        minuten = max(1, int((grenze - jetzt).total_seconds() // 60))
    return bis, minuten
