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

log = logging.getLogger(__name__)

#: Beim Bauen ins Image geschrieben (siehe Dockerfile und build.sh).
GIT_SHA = os.environ.get("KCTEAM_GIT_SHA", "unbekannt")

#: Wie lange ein Eintrag als „laeuft gerade" gilt. Wer sich laenger nicht
#: gemeldet hat, ist gestorben oder abgeschaltet — und dann sagt ein
#: Vergleich seines Standes nichts mehr.
FRISCH_SEKUNDEN = 300


def melden(db, dienst: str) -> None:
    """Lebenszeichen mit Stand. Ein Fehler hier darf nichts aufhalten."""
    from . import lessons
    try:
        db.query("""INSERT INTO curriculum.service_heartbeat
                      (service, git_sha, contract_version, pid, started_at, last_seen)
                    VALUES (%s,%s,%s,%s, now(), now())
                    ON CONFLICT (service) DO UPDATE
                      SET git_sha=EXCLUDED.git_sha,
                          contract_version=EXCLUDED.contract_version,
                          pid=EXCLUDED.pid,
                          started_at=CASE WHEN curriculum.service_heartbeat.git_sha
                                               IS DISTINCT FROM EXCLUDED.git_sha
                                          OR curriculum.service_heartbeat.pid
                                               IS DISTINCT FROM EXCLUDED.pid
                                     THEN now() ELSE curriculum.service_heartbeat.started_at END,
                          last_seen=now()""",
                 (dienst, GIT_SHA, lessons.CONTRACT_VERSION, os.getpid()))
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
    return (f"kcteam {dienst}: Stand {GIT_SHA[:12]}, Vertrag {lessons.CONTRACT_VERSION}"
            + (" (KCTEAM_GIT_SHA ist nicht gesetzt — Image ohne Stand gebaut?)"
               if GIT_SHA == "unbekannt" else ""))


def laufende(db) -> list[dict]:
    """Wer sich zuletzt gemeldet hat, mit Alter in Sekunden."""
    return db.query("""SELECT service, git_sha, contract_version, pid, started_at, last_seen,
                              extract(epoch FROM now() - last_seen)::int AS alter_s
                       FROM curriculum.service_heartbeat ORDER BY service""")


def befund(db, erwartet: tuple[str, ...] = ("api", "agent", "admin")) -> tuple[bool, list[str]]:
    """Laufen alle erwarteten Dienste, und alle aus demselben Bild?

    Gibt (in Ordnung, Zeilen) zurueck. Ein unbekannter Stand zaehlt als
    Abweichung: „unbekannt" heisst, dass das Image ohne SHA gebaut wurde,
    und dann ist der Vergleich wertlos.
    """
    zeilen, ok = [], True
    rows = {r["service"]: r for r in laufende(db)}
    frisch = {name: r for name, r in rows.items() if r["alter_s"] <= FRISCH_SEKUNDEN}
    for name in erwartet:
        r = rows.get(name)
        if not r:
            zeilen.append(f"✗ {name:6} hat sich noch nie gemeldet")
            ok = False
        elif name not in frisch:
            zeilen.append(f"✗ {name:6} zuletzt vor {r['alter_s']}s gesehen (laeuft nicht)")
            ok = False
        else:
            zeilen.append(f"✓ {name:6} {r['git_sha'][:12]}  Vertrag {r['contract_version']}")
    staende = {r["git_sha"] for name, r in frisch.items() if name in erwartet}
    if len(staende) > 1:
        zeilen.append("✗ Verschiedene Staende: " + ", ".join(sorted(s[:12] for s in staende)))
        ok = False
    elif staende == {"unbekannt"}:
        zeilen.append("✗ Stand unbekannt — die Images wurden ohne KCTEAM_GIT_SHA gebaut "
                      "(bauen mit ./build.sh)")
        ok = False
    vertraege = {r["contract_version"] for name, r in frisch.items() if name in erwartet}
    if len(vertraege) > 1:
        zeilen.append("✗ Verschiedene Vertragsfassungen: " + ", ".join(sorted(map(str, vertraege))))
        ok = False
    return ok, zeilen
