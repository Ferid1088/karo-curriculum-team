"""Die eigenen Pruefungen der Abnehmer, nach `format.id`.

`lessons.check_lesson` prueft, was jeder Abnehmer mitschickt: sein JSON-Schema,
sein Register, kein Markup. Darueber hinaus kann ein Abnehmer eigene Regeln
haben. Karo hat sie — und sie waren hier nachgebaut. Der Nachbau war anders
als das Original: der Dienst gab eine Lektion frei, Karos Import lehnte sie
ab, und nach zwei Ablehnungen gab der Dienst das Thema nicht mehr heraus.
Zwei Pruefungen, die dasselbe zu pruefen glauben, sind eine Fehlerquelle.

Deshalb ruft der Dienst jetzt die echte Pruefung des Abnehmers auf, aus
dessen eigenem Paket, bevor eine Lektion „fertig" wird. Was dabei auffaellt,
geht als Befund an den Lektionsautor — nicht spaeter als Ablehnung zurueck.

Der Dienst bleibt dabei fuer alle Abnehmer da: hier steht nichts ueber Karo
ausser der Zeile, die Karos Paket eintraegt. Ein Format ohne Eintrag wird
weiterhin nur gegen sein eigenes Schema geprueft.

Fehlt das Paket eines eingetragenen Abnehmers oder hat es die falsche
Fassung, wird fuer dieses Format **nichts** ausgeliefert: der Export bleibt
`unavailable` mit `consumer_check_missing`, und ein Mensch bekommt es auf den
Tisch. Ungeprueft zu liefern waere das Schlimmste von beidem — es sieht aus
wie Betrieb, und der Abnehmer verwirft dann jede Lieferung. Formate ohne
Eintrag sind davon nicht betroffen und laufen weiter wie bisher.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Abnehmer:
    """Die zwei Pruefungen eines Abnehmers, zu verschiedenen Zeitpunkten.

    `befunde` prueft die ganze Lieferung, waehrend die Lektion geschrieben
    wird: was sie findet, geht an den Autor zurueck und wird neu geschrieben.

    `einordnung` prueft beim Ausliefern nur noch, was sich seit der Freigabe
    geaendert haben kann. Dort die ganze Lieferung zu pruefen hiesse, eine
    freigegebene Lektion an einem unscharfen Stichwortabgleich scheitern zu
    lassen — und einer Familie ohne Grund nichts zu geben.
    """

    befunde: Callable[[Any, dict], list[str]]
    einordnung: Callable[[Any, dict], list[str]]
    #: Gibt None zurueck, wenn geprueft werden kann — sonst den Grund,
    #: warum nicht. Solange ein Grund da ist, wird fuer dieses Format nichts
    #: fertig: lieber nichts liefern als ungeprueft liefern.
    bereit: Callable[[], str | None] = lambda: None


_REGISTER: dict[str, Abnehmer] = {}
#: Formate, deren Pruefung einmal ausgefallen ist. Nur damit die Meldung
#: einmal kommt und nicht bei jeder Lektion.
_gemeldet: set[str] = set()


def register(format_id: str, abnehmer: Abnehmer) -> None:
    _REGISTER[format_id] = abnehmer


def registriert() -> tuple[str, ...]:
    return tuple(sorted(_REGISTER))


def nicht_pruefbar(spec: dict) -> str | None:
    """Warum die Pruefung dieses Abnehmers gerade nicht laufen kann.

    None heisst: sie kann laufen (oder es gibt fuer dieses Format keine).
    """
    abnehmer = _REGISTER.get((spec or {}).get("id") or "")
    if abnehmer is None:
        return None
    try:
        return abnehmer.bereit()
    except Exception as exc:                        # noqa: BLE001
        return f"Die Pruefung des Abnehmers laesst sich nicht laden: {exc}"


def _ruf(was: str, lesson: Any, umschlag: dict, spec: dict) -> list[str]:
    format_id = (spec or {}).get("id") or ""
    abnehmer = _REGISTER.get(format_id)
    if abnehmer is None:
        return []
    try:
        return [str(b)[:300] for b in getattr(abnehmer, was)(lesson, umschlag)][:10]
    except Exception as exc:                        # noqa: BLE001
        # Eine kaputte Abnehmerpruefung darf keine Lektion blockieren; sie
        # taeuscht sonst einen Inhaltsfehler vor, den niemand findet.
        if format_id not in _gemeldet:
            _gemeldet.add(format_id)
            log.warning("Pruefung des Abnehmers %s ist ausgefallen: %s", format_id, exc)
        return []


def befunde(lesson: Any, umschlag: dict, spec: dict) -> list[str]:
    """Die ganze Lieferung, so wie der Abnehmer sie pruefen wird."""
    return _ruf("befunde", lesson, umschlag, spec)


def einordnung(lesson: Any, umschlag: dict, spec: dict) -> list[str]:
    """Nur die Klasseneinordnung – beim Ausliefern."""
    return _ruf("einordnung", lesson, umschlag, spec)


def _karo_bereit() -> str | None:
    """Ist Karos Paket da, und in der Fassung, die dieser Dienst erwartet?"""
    from .lessons import CONTRACT_VERSION
    try:
        import karo_contract
    except ImportError as exc:
        return ('karo_contract ist nicht installiert (%s). Ohne das Paket kann der '
                'Dienst nicht pruefen, was Karo annehmen wuerde. Installieren: '
                'pip install "karo-contract @ git+https://github.com/Ferid1088/karo.git'
                '@contract-%s"' % (exc, CONTRACT_VERSION.removeprefix("karo-adaptiv-")))
    fremd = getattr(karo_contract, "CONTRACT_VERSION", None)
    if fremd != CONTRACT_VERSION:
        return (f"karo_contract spricht {fremd or '(keine Angabe)'}, dieser Dienst "
                f"{CONTRACT_VERSION}. Eine Pruefung aus der falschen Fassung ist keine.")
    return None


def _karo_befunde(lesson: Any, umschlag: dict) -> list[str]:
    import karo_contract
    return karo_contract.befunde({**umschlag, "lesson": lesson},
                                 thema=umschlag.get("topic"),
                                 fach=umschlag.get("subject"))


def _karo_einordnung(lesson: Any, umschlag: dict) -> list[str]:
    from karo_contract import InhaltUngueltig, huelle
    konzept = lesson.get("konzept") if isinstance(lesson, dict) else None
    einstufung = umschlag["classification"]
    try:
        huelle.pruefe_konzeptklasse(konzept or {}, einstufung["first_contact_grade"],
                                    einstufung["target_grade"])
    except InhaltUngueltig as fehler:
        return [str(fehler)]
    return []


#: Karos Format steht hier unabhaengig davon, ob sein Paket gerade da ist.
#: Genau darum geht es: ein eingetragener Abnehmer ohne Pruefung bekommt
#: nichts, statt still ungeprueftes Material zu bekommen.
register("karo-adaptiv-v1", Abnehmer(befunde=_karo_befunde, einordnung=_karo_einordnung,
                                     bereit=_karo_bereit))

_grund = nicht_pruefbar({"id": "karo-adaptiv-v1"})
if _grund:                                          # pragma: no cover
    log.warning("Fuer karo-adaptiv-v1 wird nichts ausgeliefert: %s", _grund)
