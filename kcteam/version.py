"""Welcher Stand laeuft hier — und sagt es auch.

Die API meldete ihren Git-SHA unter `/v1/meta`, Agent und Browser meldeten
gar nichts. Damit liefen sie tagelang aus einem aelteren Image weiter, ohne
dass es jemandem auffiel: die API war neu, der Agent schrieb Lektionen mit
altem Code, und hinterher liess sich nicht sagen, welcher Stand welche
Lektion verfasst hatte.

Jeder Prozess traegt sich hier ein. `kcteam doctor` vergleicht.
"""
from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)

#: Beim Bauen ins Image geschrieben (siehe Dockerfile und build.sh).
GIT_SHA = os.environ.get("KCTEAM_GIT_SHA", "unbekannt")

#: Identitaet dieser Instanz: Rechner und Prozess zusammen halten zwei
#: Worker auseinander. Der Schaden vom 4.10. war ein zweiter Agent mit
#: altem Code, dessen Herzschlag den des Docker-Agents ueberschrieb.
HOSTNAME = socket.gethostname()
INSTANCE = f"{HOSTNAME}:{os.getpid()}"

#: Umgebung, in der dieser Prozess schreibt. Wer nichts setzt, meldet
#: „entwicklung" — ein versehentlich gestarteter lokaler Agent faellt
#: dann sofort als Fremdkoerper neben der Produktion auf, statt sich
#: als zweiter Produktivschreiber zu tarnen (KCTEAM_ENV im Image).
ENVIRONMENT = os.environ.get("KCTEAM_ENV", "entwicklung")

#: Wie lange ein Eintrag als „laeuft gerade" gilt. Wer sich laenger nicht
#: gemeldet hat, ist gestorben oder abgeschaltet — und dann sagt ein
#: Vergleich seines Standes nichts mehr.
FRISCH_SEKUNDEN = 300


def melden(db, dienst: str) -> None:
    """Lebenszeichen mit Stand und Instanz. Ein Fehler hier darf nichts aufhalten."""
    from . import lessons
    try:
        db.query("""INSERT INTO curriculum.service_heartbeat
                      (service, instance, git_sha, contract_version, pid,
                       hostname, environment, started_at, last_seen)
                    VALUES (%s,%s,%s,%s,%s,%s,%s, now(), now())
                    ON CONFLICT (service, instance) DO UPDATE
                      SET git_sha=EXCLUDED.git_sha,
                          contract_version=EXCLUDED.contract_version,
                          pid=EXCLUDED.pid,
                          hostname=EXCLUDED.hostname,
                          environment=EXCLUDED.environment,
                          started_at=CASE WHEN curriculum.service_heartbeat.git_sha
                                               IS DISTINCT FROM EXCLUDED.git_sha
                                          OR curriculum.service_heartbeat.pid
                                               IS DISTINCT FROM EXCLUDED.pid
                                     THEN now() ELSE curriculum.service_heartbeat.started_at END,
                          last_seen=now()""",
                 (dienst, INSTANCE, GIT_SHA, lessons.CONTRACT_VERSION,
                  os.getpid(), HOSTNAME, ENVIRONMENT))
    except Exception as exc:                      # noqa: BLE001
        log.warning("Lebenszeichen von %s nicht geschrieben: %s", dienst, exc)


def puls(db_holen, dienst: str, takt: int = 60):
    """Meldet sich sofort und dann alle `takt` Sekunden, im Hintergrund.

    Als Faden, weil die drei Prozesse verschieden aufgebaut sind (uvicorn mit
    mehreren Arbeitern, eine Schleife, ein Browser) und keiner von ihnen eine
    Stelle hat, an der ohnehin regelmaessig etwas passiert.
    """
    import threading

    def schlagen():
        while True:
            try:
                melden(db_holen(), dienst)
            except Exception as exc:              # noqa: BLE001
                log.debug("Lebenszeichen %s: %s", dienst, exc)
            if halt.wait(takt):
                return

    halt = threading.Event()
    faden = threading.Thread(target=schlagen, daemon=True, name=f"puls-{dienst}")
    faden.start()
    return halt


def startmeldung(dienst: str) -> str:
    from . import lessons
    return (f"kcteam {dienst}: Stand {GIT_SHA[:12]}, Vertrag {lessons.CONTRACT_VERSION}, "
            f"Instanz {INSTANCE}, Umgebung {ENVIRONMENT}"
            + (" (KCTEAM_GIT_SHA ist nicht gesetzt — Image ohne Stand gebaut?)"
               if GIT_SHA == "unbekannt" else ""))


def laufende(db) -> list[dict]:
    """Wer sich zuletzt gemeldet hat — eine Zeile je Instanz, mit Alter."""
    return db.query("""SELECT service, instance, git_sha, contract_version, pid,
                              hostname, environment, started_at, last_seen,
                              extract(epoch FROM now() - last_seen)::int AS alter_s
                       FROM curriculum.service_heartbeat
                       ORDER BY service, instance""")


def befund(db, erwartet: tuple[str, ...] = ("api", "agent", "admin")) -> tuple[bool, list[str]]:
    """Laufen alle erwarteten Dienste, jeder genau einmal, aus demselben Bild?

    Gibt (in Ordnung, Zeilen) zurueck. Zwei lebende Instanzen desselben
    Dienstes in derselben Umgebung sind kein Feature: der Ghost-Worker vom
    4.10. war genau das — ein vergessener zweiter Agent, der gleichzeitig
    mit dem echten dieselbe Queue leer arbeitete.
    """
    zeilen, ok = [], True
    alle = laufende(db)
    for name in erwartet:
        eintraege = [r for r in alle if r["service"] == name]
        frisch = [r for r in eintraege if r["alter_s"] <= FRISCH_SEKUNDEN]
        if not eintraege:
            zeilen.append(f"✗ {name:6} hat sich noch nie gemeldet")
            ok = False
            continue
        if not frisch:
            aeltester = min(eintraege, key=lambda r: r["alter_s"])
            zeilen.append(f"✗ {name:6} zuletzt vor {aeltester['alter_s']}s gesehen (laeuft nicht)")
            ok = False
            continue
        for r in frisch:
            zeilen.append(f"✓ {name:6} {r['git_sha'][:12]}  Vertrag {r['contract_version']}"
                          f"  {r['instance']}  [{r['environment']}]")
        # Pro Umgebung zaehlt nur der eigentliche Schreiber — ein zweiter
        # aktiver Agent dort ist der Ghost, der sich nie melden sollte.
        umgebungen: dict[str | None, list] = {}
        for r in frisch:
            umgebungen.setdefault(r["environment"], []).append(r)
        for umgebung, instanzen in umgebungen.items():
            if len(instanzen) > 1:
                ok = False
                wer = ", ".join(f"{i['instance']} ({i['git_sha'][:12]})" for i in instanzen)
                zeilen.append(f"✗ {name:6} {len(instanzen)}× aktiv in „{umgebung}“: {wer}"
                              " — nur ein Schreiber je Umgebung")
    frisch_alle = [r for r in alle if r["alter_s"] <= FRISCH_SEKUNDEN
                   and r["service"] in erwartet]
    staende = {r["git_sha"] for r in frisch_alle}
    if len(staende) > 1:
        zeilen.append("✗ Verschiedene Staende: " + ", ".join(sorted(s[:12] for s in staende)))
        ok = False
    elif staende == {"unbekannt"}:
        zeilen.append("✗ Stand unbekannt — die Images wurden ohne KCTEAM_GIT_SHA gebaut "
                      "(bauen mit ./build.sh)")
        ok = False
    vertraege = {r["contract_version"] for r in frisch_alle}
    if len(vertraege) > 1:
        zeilen.append("✗ Verschiedene Vertragsfassungen: " + ", ".join(sorted(map(str, vertraege))))
        ok = False
    return ok, zeilen
